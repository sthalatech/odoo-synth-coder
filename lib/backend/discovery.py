"""Provenance discovery orchestration.

Launches the *discovery* runner workspace for a profile: it inspects the live source
DB + addons repo and uploads a discovery.json to S3. On success we fold the
result back into the profile (odoo_series, installed_modules, python_deps,
apt_deps, discovery_yaml_uri) and advance image_status to ``discovered``.

Streams live to the run-log via :mod:`pipeline` (Coder runner workspace).
"""
from __future__ import annotations

import json
import time
import urllib.request
import uuid
from typing import Callable, Optional

import boto3

from . import config, store, profiles, pipeline

LogSink = Callable[[str], None]


def _region() -> str:
    return config.require("AWS_REGION")


def _presign_discovery(profile_id: str) -> tuple[str, str, str]:
    bucket = config.dump_s3_bucket()
    if not bucket:
        raise RuntimeError("no S3 bucket configured (set the dump_s3_bucket)")
    prefix = config.dump_s3_prefix().rstrip("/").rsplit("/", 1)[0] + "/discovery"
    key = f"{prefix}/{profile_id}/{uuid.uuid4().hex[:12]}/discovery.json"
    s3 = boto3.client("s3", region_name=_region())
    put_url = s3.generate_presigned_url(
        "put_object",
        Params={"Bucket": bucket, "Key": key, "ContentType": "application/json"},
        ExpiresIn=6 * 3600)
    get_url = s3.generate_presigned_url(
        "get_object", Params={"Bucket": bucket, "Key": key}, ExpiresIn=7 * 24 * 3600)
    return put_url, get_url, f"s3://{bucket}/{key}"


def _discovery_env_pairs(profile: dict, put_url: str) -> list[tuple[str, str]]:
    """Build the discovery container's environment as (KEY, VAL) pairs. Shared by
    the Coder runner path (env -> S3 env-file)."""
    conn = profile.get("source_conn") or {}
    password = profiles._get_secret(profile.get("source_password_secret"))
    env = [
        ("PROFILE_ID", profile["id"]),
        ("SOURCE_DB_HOST", conn.get("host", "")),
        ("SOURCE_DB_PORT", conn.get("port", 5432)),
        ("SOURCE_DB_NAME", conn.get("dbname", "")),
        ("SOURCE_DB_USER", conn.get("user", "")),
        ("SOURCE_DB_PASSWORD", password),
        ("ODOO_GIT_REF", profile.get("odoo_git_ref") or ""),
        ("ADDONS_GIT_URL", profile.get("addons_git_url") or ""),
        ("ADDONS_GIT_REF", profile.get("addons_git_ref") or ""),
        # The git token lives in a Coder user secret injected into the runner
        # workspace as $GIT_TOKEN_<UPPER_ID>. Forward the *name* (not the value --
        # Coder secrets are write-only) so the runner startup re-exports it as
        # GIT_TOKEN for the discovery container.
        ("GIT_TOKEN_ENV", profiles.git_token_env_name(profile["id"])
         if profile.get("git_token_secret") else ""),
        ("DISCOVERY_PUT_URL", put_url),
    ]
    # dump-slimming knobs (saved per-profile in mask_inputs) -> read by
    # gen_masking.py inside the discovery container when it builds the profile.
    # NOTE: subset_days is a MASK-time knob (the masker prunes rows after
    # restore), so it is NOT passed here -- only exclude_table_data affects the
    # generated greenmask profile.
    mi = profile.get("mask_inputs") or {}
    etd = mi.get("exclude_table_data")
    if etd not in (None, ""):
        env.append(("GM_EXCLUDE_TABLE_DATA", etd))
    if conn.get("ssh_enabled") and conn.get("ssh_bastion"):
        b = pipeline.parse_bastion(conn["ssh_bastion"])
        env += [
            ("SSH_ENABLED", "true"),
            ("SSH_BASTION_HOST", b["host"]),
            ("SSH_BASTION_USER", b["user"]),
            ("SSH_BASTION_PORT", b["port"]),
            ("SSH_PRIVATE_KEY", profiles._get_secret(profile.get("ssh_key_secret"))),
        ]
    return env


def run_discovery(profile_id: str, emit: LogSink, run_id: str | None = None) -> dict:
    """Blocking: launch the discovery task, stream logs, fold results into the
    profile. Returns a small result dict (exit_code, discovery_uri, hash)."""
    profile = store.get_profile(profile_id)
    if not profile:
        raise KeyError(profile_id)
    conn = profile.get("source_conn") or {}
    if not conn.get("host"):
        raise ValueError("profile has no source connection; add a source DB URL first")

    pipeline._require_coder()
    put_url, get_url, s3_uri = _presign_discovery(profile_id)
    emit(f"[panel] discovery output -> {s3_uri}")

    store.update_profile(profile_id, image_status="discovering", error=None)

    # ---- run the discovery container as a Coder runner workspace ----
    # Env vars written to S3 as an env-file. The discovery container writes its
    # OWN discovery.json to DISCOVERY_PUT_URL (the presigned URL above); the
    # runner workspace additionally writes a runner-result.json marker
    # (exit_code) which run_runner polls. We fetch discovery.json via get_url
    # after the runner exits 0.
    env_pairs = _discovery_env_pairs(profile, put_url)
    emit("[panel] launching Coder runner workspace (discovery) ...")
    rr = pipeline.run_runner("discovery", env_pairs, "discover", emit, run_id=run_id)
    exit_code = rr.get("exit_code", 1)
    emit(f"[panel] discovery runner exited with code {exit_code}")

    if exit_code != 0:
        store.update_profile(profile_id, image_status="failed",
                             error=f"discovery task exited {exit_code}")
        return {"exit_code": exit_code, "error": f"discovery task exited {exit_code}"}

    # fetch the discovery.json we just produced and fold it into the profile
    data = _fetch_discovery(get_url)
    req_keys = data.get("required_config_keys") or []
    fields: dict = {
        "discovery_yaml_uri": s3_uri,
        "installed_modules": data.get("installed_modules") or [],
        "python_deps": data.get("python_deps") or [],
        "apt_deps": data.get("apt_deps") or [],
        "required_config_keys": req_keys,
        "discovery_hash": data.get("discovery_hash"),
        "image_status": "discovered",
        "error": None,
    }
    if data.get("odoo_series") and not profile.get("odoo_series"):
        fields["odoo_series"] = data["odoo_series"]
    # Seed odoo.conf extras from the discovered config keys so custom addons that
    # read source-specific keys (e.g. an SSO addon's config['sso_api_secret'])
    # don't KeyError->500 in the masked dev replica. Existing lines are kept and
    # user-set values are never overwritten; only missing keys are added empty.
    merged_conf = _merge_conf_extra(profile.get("odoo_conf_extra") or "", req_keys)
    if merged_conf != (profile.get("odoo_conf_extra") or ""):
        fields["odoo_conf_extra"] = merged_conf
    # Editable per-source masking plan: seed it from discovery only if the
    # profile has none yet, so a hand-edited plan survives re-discovery. The
    # freshly discovered plan always stays retrievable from discovery.json in S3
    # (discovery_yaml_uri), which powers the "reset to discovered" action.
    discovered_plan = data.get("masking_plan") or ""
    if discovered_plan and not (profile.get("masking_rules") or "").strip():
        fields["masking_rules"] = discovered_plan
    store.update_profile(profile_id, **fields)

    if discovered_plan:
        emit(f"[panel] masking plan generated ({discovered_plan.count(chr(10))} "
             f"lines){' (seeded)' if 'masking_rules' in fields else ' (kept existing edits)'}")

    if req_keys:
        emit(f"[panel] required odoo.conf keys discovered: {', '.join(req_keys)}")

    undeclared = data.get("python_deps_undeclared") or []
    if undeclared:
        emit(f"[panel] NOTE undeclared python deps discovered: {', '.join(undeclared)}")
    emit(f"[panel] discovery folded into profile: "
         f"{len(fields['installed_modules'])} modules, "
         f"{len(fields['python_deps'])} python deps, "
         f"hash={data.get('discovery_hash')}")
    return {"exit_code": 0, "discovery_uri": s3_uri,
            "discovery_hash": data.get("discovery_hash")}


def _fetch_discovery(get_url: str) -> dict:
    with urllib.request.urlopen(get_url, timeout=60) as r:  # noqa: S310 — presigned
        return json.loads(r.read().decode())


def _merge_conf_extra(existing: str, required_keys: list) -> str:
    """Append `key =` lines for any required odoo.conf key not already present
    in `existing`. Never overwrites a key the user already set (with a value),
    so a hand-edited secret survives re-discovery. Returns the merged text."""
    present: set = set()
    for ln in existing.splitlines():
        s = ln.strip()
        if not s or s.startswith(("#", ";")) or "=" not in s:
            continue
        present.add(s.split("=", 1)[0].strip())
    additions = [f"{k} =" for k in required_keys if k not in present]
    if not additions:
        return existing
    body = existing.rstrip("\n")
    prefix = (body + "\n") if body else ""
    return prefix + "\n".join(additions) + "\n"


# ---------------------------------------------------------------------------
# masking-plan helpers (used by the API to view/edit/reset the per-source plan)
# ---------------------------------------------------------------------------

def validate_masking_rules(text: str) -> tuple[bool, str]:
    """Best-effort validation of an edited greenmask masking profile. Empty is
    allowed (the masker falls back to its baked profile). If PyYAML is available
    we parse it and sanity-check that it looks like a greenmask profile with a
    dump.transformation list."""
    if not text.strip():
        return True, ""
    try:
        import yaml
    except Exception:  # noqa: BLE001
        return True, ""  # cannot validate here; masker/greenmask validates strictly
    try:
        doc = yaml.safe_load(text)
    except Exception as exc:  # noqa: BLE001
        return False, f"YAML parse error: {exc}"
    if not isinstance(doc, dict):
        return False, "profile must be a YAML mapping"
    dump = doc.get("dump")
    if not isinstance(dump, dict) or "transformation" not in dump:
        return False, "greenmask profile must define dump.transformation"
    if not isinstance(dump["transformation"], list):
        return False, "dump.transformation must be a list"
    return True, ""


def discovered_masking_plan(profile: dict) -> str:
    """Fetch the freshly-discovered masking plan from the profile's last
    discovery.json in S3 (discovery_yaml_uri), for the 'reset to discovered'
    action. Returns '' if unavailable."""
    uri = profile.get("discovery_yaml_uri") or ""
    if not uri.startswith("s3://"):
        return ""
    try:
        _, _, rest = uri.partition("s3://")
        bucket, _, key = rest.partition("/")
        s3 = boto3.client("s3", region_name=_region())
        body = s3.get_object(Bucket=bucket, Key=key)["Body"].read()
        return (json.loads(body).get("masking_plan") or "")
    except Exception:  # noqa: BLE001
        return ""

