"""Mask orchestration: run the masker as a Coder runner workspace.

Single operation: **mask**.
  * SOURCE      = a live Postgres DB the user points at (a connection URL/DSN).
                  greenmask dumps + masks it directly.
  * DESTINATION = the masked DB. RDS-free: the masker restores into a throwaway
                  local postgres on the runner workspace (see odoo-synth-runner
                  main.tf). Always dropped + recreated.
  * OUTPUT      = optionally a downloadable pg_dump of the masked DB (uploaded to
                  S3 via a presigned PUT; a presigned GET is returned to the UI).

The masker runs as a Coder runner workspace; the panel tails `coder logs -f`
live into the run log and polls S3 for the runner-result.json marker.
"""
from __future__ import annotations
import json
import threading
import time
import uuid
from typing import Callable, Optional
from urllib.parse import urlparse, unquote

import boto3

from . import config

LogSink = Callable[[str], None]

# Option E, Phase 3: the mask + discovery single-container workloads run as
# Coder workspaces from the odoo-synth-runner template. The
# panel keeps orchestration: it builds the same env-var dict, writes it to S3 as
# an env-file, presigns a result PUT URL, launches the workspace, tails its
# logs live, and polls S3 for the result marker -- exactly like the build.
RUNNER_TEMPLATE = "odoo-synth-runner"


def _require_coder() -> None:
    """Preflight: the `coder` CLI must be on PATH and the Coder control plane
    configured. discover/build/run mask all launch a Coder runner workspace via
    `subprocess.run(["coder", ...])`; if the binary is missing that raises a
    raw FileNotFoundError. Fail fast with an actionable message instead."""
    import shutil
    if not shutil.which("coder"):
        raise RuntimeError(
            "the `coder` CLI was not found on PATH. It is required to launch "
            "runner workspaces (discover / build / run mask). Install it via "
            "`bash deploy/00_install_prereqs.sh` or from https://coder.com/docs/install, "
            "then `coder login <CODER_URL>`.")


def _coder_env() -> dict:
    s = config.environments_settings()
    env = {
        "CODER_URL": s.get("coder_url") or "",
        "CODER_SESSION_TOKEN": s.get("coder_session_token") or "",
    }
    for k in ("AWS_REGION", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY",
              "AWS_SESSION_TOKEN", "AWS_DEFAULT_REGION"):
        v = config.get(k)
        if v:
            env[k] = v
    return env


def _region() -> str:
    return config.require("AWS_REGION")


def _ecr() -> str:
    acct = boto3.client("sts", region_name=_region()).get_caller_identity()["Account"]
    return f"{acct}.dkr.ecr.{_region()}.amazonaws.com"


def parse_dsn(dsn: str) -> dict:
    """Parse a postgresql:// URL into host/port/user/password/dbname parts."""
    u = urlparse(dsn)
    if u.scheme not in ("postgres", "postgresql"):
        raise ValueError("source URL must start with postgresql://")
    if not u.hostname:
        raise ValueError("source URL is missing a host")
    dbname = (u.path or "/").lstrip("/")
    if not dbname:
        raise ValueError("source URL is missing a database name (…/dbname)")
    return {
        "host": u.hostname,
        "port": str(u.port or 5432),
        "user": unquote(u.username) if u.username else "postgres",
        "password": unquote(u.password) if u.password else "",
        "dbname": dbname,
    }


def parse_bastion(bastion: str) -> dict:
    """Parse 'user@host[:port]' into user/host/port."""
    b = bastion.strip()
    if "@" not in b:
        raise ValueError("bastion must be 'user@host[:port]'")
    user, _, hostpart = b.partition("@")
    user = user.strip()
    host, _, port = hostpart.partition(":")
    host = host.strip()
    if not user or not host:
        raise ValueError("bastion must be 'user@host[:port]'")
    return {"user": user, "host": host, "port": (port.strip() or "22")}


# ---------------------------------------------------------------------------
# masked-dump download staging (presigned PUT for upload, GET for download)
# ---------------------------------------------------------------------------

def _presign_masked_dump() -> tuple[str, str, str]:
    """Return (put_url, get_url, s3_uri) for a fresh masked-dump object, or raise."""
    bucket = config.dump_s3_bucket()
    if not bucket:
        raise RuntimeError("no S3 bucket configured for masked dumps (set DUMP_S3_BUCKET)")
    prefix = config.dump_s3_prefix().rstrip("/")
    key = f"{prefix}/{uuid.uuid4().hex[:12]}/masked.dump"
    s3 = boto3.client("s3", region_name=_region())
    put_url = s3.generate_presigned_url(
        "put_object", Params={"Bucket": bucket, "Key": key}, ExpiresIn=12 * 3600
    )
    get_url = s3.generate_presigned_url(
        "get_object", Params={"Bucket": bucket, "Key": key}, ExpiresIn=7 * 24 * 3600
    )
    return put_url, get_url, f"s3://{bucket}/{key}"


def _upload_mask_rules(text: str) -> str:
    """Upload an edited greenmask profile to S3 and return a presigned GET URL
    the masker can download. Returns '' if there's nothing to upload."""
    if not text.strip():
        return ""
    bucket = config.dump_s3_bucket()
    if not bucket:
        raise RuntimeError("no S3 bucket configured (set DUMP_S3_BUCKET)")
    prefix = config.dump_s3_prefix().rstrip("/").rsplit("/", 1)[0] + "/mask-rules"
    key = f"{prefix}/{uuid.uuid4().hex[:12]}/greenmask.yml"
    s3 = boto3.client("s3", region_name=_region())
    s3.put_object(Bucket=bucket, Key=key, Body=text.encode("utf-8"),
                  ContentType="text/yaml")
    return s3.generate_presigned_url(
        "get_object", Params={"Bucket": bucket, "Key": key}, ExpiresIn=12 * 3600)


# ---------------------------------------------------------------------------
# Coder runner workspace (Option E, Phase 3: mask + discovery as workspaces)
# ---------------------------------------------------------------------------

def _upload_env_file(env_pairs: list[tuple[str, object]]) -> tuple[str, list[str]]:
    """Write a shell env-setup script to S3 and return (presigned GET URL, list
    of KEY names). The runner sources the script (which `export`s each var with
    single-quote escaping -- this handles multi-line values like SSH private
    keys, which docker's line-based --env-file CANNOT), then passes each KEY to
    `docker run -e KEY` so the host env (incl. newlines) flows into the
    container unchanged. Values are stringified. The object is short-lived
    (matched to the run); the presigned GET lasts 12h."""
    keys: list[str] = []
    lines = ["#!/usr/bin/env sh", "# auto-generated runner env (single-quote escaped)"]
    for k, v in env_pairs:
        keys.append(k)
        # escape embedded single-quotes: ' -> '\'' (close, escaped quote, reopen)
        sv = str(v).replace("'", "'\\''")
        lines.append(f"export {k}='{sv}'")
    body = "\n".join(lines).encode("utf-8")
    bucket = config.dump_s3_bucket()
    if not bucket:
        raise RuntimeError("no S3 bucket configured (set the dump_s3_bucket)")
    prefix = config.dump_s3_prefix().rstrip("/").rsplit("/", 1)[0] + "/runner-env"
    key = f"{prefix}/{uuid.uuid4().hex[:12]}/container.env"
    s3 = boto3.client("s3", region_name=_region())
    s3.put_object(Bucket=bucket, Key=key, Body=body, ContentType="text/plain")
    url = s3.generate_presigned_url(
        "get_object", Params={"Bucket": bucket, "Key": key}, ExpiresIn=12 * 3600)
    return url, keys


def _presign_runner_result(phase: str, run_id: str | None = None) -> tuple[str, str, str]:
    """Presign a PUT (runner writes result.json) + GET (panel reads it) and
    return (put_url, get_url, s3_uri).

    When ``run_id`` is given, the result lands in the run's own S3 prefix
    (``runs/<run_id>/runner-result.json``) so run_store can reconcile a run
    whose CLI finalizer died -- the runner's result is authoritative on S3.
    Otherwise (legacy) a random runner-results key is used."""
    bucket = config.dump_s3_bucket()
    if not bucket:
        raise RuntimeError("no S3 bucket configured (set the dump_s3_bucket)")
    if run_id:
        prefix = (config.dump_s3_prefix() or "masked-dumps").rstrip("/") + "/runs"
        key = f"{prefix}/{run_id}/runner-result.json"
    else:
        prefix = config.dump_s3_prefix().rstrip("/").rsplit("/", 1)[0] + "/runner-results"
        key = f"{prefix}/{phase}/{uuid.uuid4().hex[:12]}/result.json"
    s3 = boto3.client("s3", region_name=_region())
    put_url = s3.generate_presigned_url(
        "put_object", Params={"Bucket": bucket, "Key": key,
                               "ContentType": "application/json"}, ExpiresIn=6 * 3600)
    get_url = s3.generate_presigned_url(
        "get_object", Params={"Bucket": bucket, "Key": key}, ExpiresIn=7 * 24 * 3600)
    return put_url, get_url, f"s3://{bucket}/{key}"


def _runner_image(name: str) -> str:
    """Full ECR URI for a runner image (masker / discovery)."""
    return f"{_ecr()}/{config.require('PROJECT')}/{name}:latest"


def _launch_runner(image_uri: str, env_file_get_url: str, env_keys: list[str],
                    result_put_url: str, phase: str) -> str:
    """Launch a Coder runner workspace and return its name. The workspace pulls
    the image, downloads the env-setup script, sources it, `docker run -e KEY`s
    each var through (handles multi-line values like SSH keys), PUTs a
    result.json to S3, and powers off."""
    import os
    import subprocess

    _require_coder()
    s = config.environments_settings()
    # The runner template coalesces an empty ami_id back to its golden default,
    # but the SG + instance profile + subnet have no safe default for a fresh
    # account -- an empty one yields a vague AWS error (e.g. MissingParameter:
    # ImageId, or InvalidGroup.NotFound). Fail fast with an actionable message
    # so the user knows to populate deploy/state.env (ENV_SG_ID / ENV_SUBNET_ID
    # / ENV_INSTANCE_PROFILE) from the deploy scripts.
    missing = [k for k in ("security_group_id", "instance_profile", "subnet_id")
               if not s.get(k)]
    if missing:
        raise RuntimeError(
            f"runner launch settings missing from config: {', '.join(missing)}. "
            "These come from deploy/state.env (written by deploy/11_coder_server.sh "
            "and 09_dev_env.sh): ENV_SG_ID, ENV_SUBNET_ID, ENV_INSTANCE_PROFILE. "
            "Source deploy/state.env before running, or run the deploy pipeline "
            "first.")
    params = [
        ("ami_id", s.get("ami_id") or ""),
        ("instance_profile", s.get("instance_profile") or ""),
        ("subnet_id", s.get("subnet_id") or ""),
        ("security_group_id", s.get("security_group_id") or ""),
        ("region", _region()),
        ("instance_type", s.get("runner_instance_type")
         or s.get("instance_type") or "m5.large"),
        ("image_uri", image_uri),
        ("env_file_get_url", env_file_get_url),
        ("env_keys", ";".join(env_keys)),
        ("result_put_url", result_put_url),
        ("phase", phase),
    ]
    ws_name = f"{phase}-{uuid.uuid4().hex[:8]}"
    args = ["create", "-t", RUNNER_TEMPLATE, "-y", "--no-wait", ws_name]
    for k, v in params:
        args += ["--parameter", f"{k}={v}"]
    subprocess.run(["coder", *args], env={**os.environ, **_coder_env()},
                   check=True, capture_output=True, text=True, timeout=120)
    return ws_name


def _delete_runner(ws_name: str, emit: Optional[LogSink] = None) -> None:
    """Delete a runner workspace once its result has been collected. The
    workspace powers itself off on completion; this tears down the (now
    stopped) EC2 instance + Coder record so we don't accumulate idle VMs.
    Best-effort: a failure here is logged, not raised."""
    import os
    import subprocess

    try:
        subprocess.run(["coder", "delete", ws_name, "-y"],
                       env={**os.environ, **_coder_env()},
                       check=True, capture_output=True, text=True, timeout=120)
        if emit is not None:
            emit(f"[panel] runner workspace {ws_name} deleted")
    except Exception as exc:  # noqa: BLE001
        if emit is not None:
            emit(f"[panel] runner workspace {ws_name} cleanup failed: {exc}")


def _tail_runner(ws_name: str, emit: LogSink, timeout: float = 7200.0,
                stop: Optional[threading.Event] = None) -> None:
    """Stream the runner workspace's Coder logs into the panel's run-log SSE.

    Uses `coder logs -f` via a long-lived Popen so lines stream incrementally.
    `coder logs -f` doesn't always follow the agent's startup_script stdout
    reliably (it can stall after the provisioner phase), so this is best-effort
    UX -- the authoritative completion signal comes from _poll_runner_result.
    Stops when `stop` is set, the timeout elapses, or the stream EOFs."""
    import os
    import subprocess

    deadline = time.time() + timeout
    try:
        p = subprocess.Popen(
            ["coder", "logs", "-f", ws_name],
            env={**os.environ, **_coder_env()},
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            bufsize=1)
        assert p.stdout is not None
        for line in p.stdout:
            emit(line.rstrip("\n"))
            if time.time() > deadline or (stop is not None and stop.is_set()):
                p.terminate()
                break
        p.wait(timeout=10)
    except Exception as exc:  # noqa: BLE001
        emit(f"[panel] log tail ended: {exc}")
    finally:
        try:
            p.terminate()
        except Exception:  # noqa: BLE001
            pass


def _poll_runner_result(get_url: str, emit: LogSink,
                        timeout: float = 7200.0) -> Optional[dict]:
    """Poll the runner's result.json marker on S3 until it appears or the
    timeout elapses. Returns the parsed dict or None on timeout."""
    import urllib.request

    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(get_url, timeout=30) as r:  # noqa: S310
                return json.loads(r.read().decode())
        except Exception:  # noqa: BLE001
            time.sleep(5)
    emit("[panel] timed out waiting for runner result.json")
    return None


def run_runner(image_name: str, env_pairs: list[tuple[str, object]],
                phase: str, emit: LogSink, run_id: str | None = None) -> dict:
    """Option E runner path: write the env-file, presign the result URL, launch
    the Coder runner workspace, tail its logs live, poll S3 for the result.
    Returns a dict with exit_code (0/1) + error (on failure), matching the
    ECS path's return shape so callers (run_operation / run_discovery) are
    unchanged."""
    env_get, env_keys = _upload_env_file(env_pairs)
    put_url, get_url, _ = _presign_runner_result(phase, run_id=run_id)
    image_uri = _runner_image(image_name)
    emit(f"[panel] launching Coder runner workspace ({image_name}, {phase}) ...")
    ws_name = _launch_runner(image_uri, env_get, env_keys, put_url, phase)
    emit(f"[panel] runner workspace {ws_name} launched; streaming logs ...")
    # Tail the Coder logs in a background thread while the main thread polls
    # S3 for the result.json marker. `coder logs -f` can stall after the
    # provisioner phase (it doesn't reliably follow the agent's startup_script
    # stdout), so the result is authoritative -- the tail is best-effort UX.
    tail_done = threading.Event()
    tail_thread = threading.Thread(
        target=_tail_runner, args=(ws_name, emit, 7200.0, tail_done), daemon=True)
    tail_thread.start()
    result = _poll_runner_result(get_url, emit)
    # Give the tail a moment to flush any trailing lines, then let it end.
    tail_done.set()
    tail_thread.join(timeout=10)
    _delete_runner(ws_name, emit)
    if not result:
        return {"exit_code": 1, "error": "runner result.json not found (timeout)",
                "task_arn": ws_name}
    exit_code = int(result.get("exit_code", 1))
    out: dict = {"exit_code": exit_code, "task_arn": ws_name}
    # `coder logs -f` can stall after the provisioner phase and miss the
    # [container]/[runner] lines, so emit the runner's own log_tail from the
    # result.json as a fallback so the user sees what the container did.
    log_tail = result.get("log_tail") or ""
    if log_tail:
        for line in log_tail.splitlines():
            emit(line)
    if exit_code != 0:
        out["error"] = result.get("error") or f"runner exited {exit_code}"
    return out


# ---------------------------------------------------------------------------
# mask task definition
# ---------------------------------------------------------------------------
# entry
# ---------------------------------------------------------------------------

def run_operation(operation: str, params: dict, emit: LogSink,
                 run_id: str | None = None) -> dict:
    if operation != "mask":
        raise ValueError(f"unknown operation: {operation}")

    _require_coder()

    # SOURCE: a live DB the user pointed at (DSN)
    dsn = params.get("source_dsn")
    if not dsn:
        raise ValueError("source database URL (postgresql://…) is required")
    src = parse_dsn(dsn)

    # DESTINATION: mask restores into a THROWAWAY local postgres on the
    # runner workspace (see odoo-synth-runner main.tf) -- no shared DB, so no
    # two envs share one and re-masking never clobbers another env. The runner
    # overrides TARGET_DB_* to point at its local `runner-db` container. The
    # destination host/user/password/dbname are passed through for the masker's
    # greenmask restore target.
    tgt = config.destination()

    # The masked pg_dump is the PRIMARY artifact: each env hydrates its own
    # local DB from it (see odoo-synth-env main.tf). Always produce it unless
    # the caller explicitly disabled it.
    masked_dump_get_url = None
    masked_dump_put_url = None
    masked_dump_s3_uri = None
    if params.get("produce_dump", True):
        masked_dump_put_url, masked_dump_get_url, masked_dump_s3_uri = _presign_masked_dump()
        emit("[panel] masked dump artifact will be uploaded to S3 (envs hydrate from it)")

    # per-source editable greenmask profile (generated during discovery)
    mask_rules_url = _upload_mask_rules(params.get("mask_rules") or "")
    if mask_rules_url:
        emit("[panel] using edited per-source masking profile (greenmask)")

    via = ""
    if params.get("ssh_enabled") and params.get("ssh_bastion"):
        via = f" via ssh {params['ssh_bastion']}"
    emit(f"[panel] mask source={src['user']}@{src['host']}:{src['port']}/{src['dbname']}{via} "
         f"-> {tgt['dbname']}@{tgt['host']} profile={params.get('mask_profile')}")

    # ---- run the masker as a Coder runner workspace ----
    # Env vars written to S3 as an env-file the workspace downloads. The masker
    # image is unchanged; it PUTs a runner-result.json marker to S3 on
    # completion. The panel tails `coder logs -f` live into the run log.
    env_pairs = _mask_env_pairs(src, tgt, params,
                                masked_dump_put_url, mask_rules_url)
    rr = run_runner("masker", env_pairs, "mask", emit, run_id=run_id)
    exit_code = rr.get("exit_code", 1)
    emit(f"[panel] runner exited with code {exit_code}")
    result: dict = {"task_arn": rr.get("task_arn"), "exit_code": exit_code}
    if exit_code == 0:
        if masked_dump_get_url:
            result["masked_dump_url"] = masked_dump_get_url
        if masked_dump_s3_uri:
            result["masked_dump_s3_uri"] = masked_dump_s3_uri
        # Provenance: the exact Odoo image this masked dataset pairs with, so a
        # developer environment seeded from this run runs identical code. When
        # the run came from a profile, params["odoo_image"] is that profile's
        # immutable build (odoo:<profile>-<hash>); only fall back to :latest for
        # the inline path that has no profile image.
        proj = config.get("PROJECT")
        result["odoo_image"] = (params.get("odoo_image")
                                or f"{_ecr()}/{proj}/odoo:latest")
    elif exit_code == 3:
        result["error"] = ("preflight failed: source or destination DB was "
                            "not reachable from the masker (check the URL, "
                            "credentials, and network/security-group access).")
    elif exit_code == 4:
        result["error"] = ("SSH tunnel failed: could not connect to the "
                            "bastion or forward to the source DB (check "
                            "bastion host/user/port, the SSH key, and that "
                            "the bastion can reach the DB).")
    else:
        result["error"] = rr.get("error") or f"masker exited non-zero ({exit_code})"
    return result
