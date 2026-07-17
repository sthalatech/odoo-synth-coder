"""Provenance image build orchestration (Phase 2, decision 1b).

Bakes a profile's provenance (Odoo core ref + custom addons ref) plus its
*discovered* python deps into an immutable Odoo image on a dedicated, ephemeral
EC2 builder — never on the panel host. The builder downloads the odoo/ build
context, builds, pushes ``odoo:<profile_id>-<discovery_hash>`` to ECR, reports a
result JSON to S3, then self-terminates. We poll S3 for that result and, on
success, advance the profile to ``ready`` with the new immutable image_uri
(keeping prior images in image_history — decision 4: immutable retention).
"""
from __future__ import annotations

import io
import json
import tarfile
import time
import urllib.request
import uuid
from pathlib import Path
from typing import Callable, Optional

import boto3

from . import config, store, profiles

LogSink = Callable[[str], None]

TEMPLATE = (Path(__file__).resolve().parent.parent
            / "environments" / "builder-user-data.sh.tmpl")
BUILD_CONTEXT = config.REPO_ROOT / "odoo"
ENTERPRISE_ZIP = BUILD_CONTEXT / "enterprise.zip"


def _have_enterprise_zip() -> bool:
    """True if a local odoo/enterprise.zip bundle is present to bake in."""
    return ENTERPRISE_ZIP.is_file()

# Option E: the build runs as a Coder workspace from the odoo-synth-builder
# template (same logic as builder-user-data.sh.tmpl, parameterized). The panel
# keeps orchestration: package context, presign URLs, launch the workspace,
# poll S3 for the result. Empty/unset => the legacy ephemeral-EC2 path.
BUILDER_TEMPLATE = "odoo-synth-builder"


def _builder_use_coder() -> bool:
    """True if builds should run as Coder workspaces (the odoo-synth-builder
    template) instead of the legacy hand-rolled ephemeral EC2. Enabled when the
    Coder control plane is configured (CODER_URL + CODER_SESSION_TOKEN)."""
    s = config.environments_settings()
    return bool(s.get("coder_url") and s.get("coder_session_token"))


def _coder_env() -> dict:
    """Env for the coder CLI (server URL + session token + AWS creds). Mirrors
    environments._coder_env so the builder workspace launches with the same
    auth as dev envs."""
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


def _ecr_registry() -> str:
    acct = boto3.client("sts", region_name=_region()).get_caller_identity()["Account"]
    return f"{acct}.dkr.ecr.{_region()}.amazonaws.com"


def _make_context_tarball(include_enterprise: bool = False) -> bytes:
    """Tar.gz the odoo/ build context.

    The unzipped ``enterprise/`` tree is always excluded (it's redundant with
    the compact ``enterprise.zip`` and would bloat the upload). When the
    profile needs enterprise, ``enterprise.zip`` is included so the builder can
    unzip it into ``enterprise/`` before ``docker build`` (mirroring
    ``deploy/02_build_push.sh``); otherwise it's skipped so non-enterprise
    images stay small and enterprise-free.
    """
    buf = io.BytesIO()
    skip = {".git", "enterprise", "custom-addons"}
    skip_files = set()
    if not include_enterprise:
        skip_files.add("enterprise.zip")
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for p in sorted(BUILD_CONTEXT.rglob("*")):
            rel = p.relative_to(BUILD_CONTEXT)
            if rel.parts and rel.parts[0] in skip:
                continue
            if rel.name in skip_files:
                continue
            tar.add(p, arcname=str(rel), recursive=False)
    return buf.getvalue()


def _s3():
    return boto3.client("s3", region_name=_region())


def _bucket() -> str:
    b = config.dump_s3_bucket()
    if not b:
        raise RuntimeError("no S3 bucket configured (set the dump_s3_bucket)")
    return b


def _upload_context(profile_id: str, include_enterprise: bool = False) -> tuple[str, str]:
    """Upload the context tarball; return (get_url, s3_uri)."""
    bucket = _bucket()
    prefix = config.dump_s3_prefix().rstrip("/").rsplit("/", 1)[0] + "/builds"
    key = f"{prefix}/{profile_id}/{uuid.uuid4().hex[:12]}/context.tgz"
    s3 = _s3()
    s3.put_object(Bucket=bucket, Key=key,
                  Body=_make_context_tarball(include_enterprise=include_enterprise))
    get_url = s3.generate_presigned_url(
        "get_object", Params={"Bucket": bucket, "Key": key}, ExpiresIn=6 * 3600)
    return get_url, f"s3://{bucket}/{key}"


def _presign_result(profile_id: str) -> tuple[str, str, str]:
    bucket = _bucket()
    prefix = config.dump_s3_prefix().rstrip("/").rsplit("/", 1)[0] + "/builds"
    key = f"{prefix}/{profile_id}/{uuid.uuid4().hex[:12]}/result.json"
    s3 = _s3()
    put_url = s3.generate_presigned_url(
        "put_object",
        Params={"Bucket": bucket, "Key": key, "ContentType": "application/json"},
        ExpiresIn=12 * 3600)
    get_url = s3.generate_presigned_url(
        "get_object", Params={"Bucket": bucket, "Key": key}, ExpiresIn=12 * 3600)
    return put_url, get_url, key


def _render_user_data(image_uri: str, context_get: str, result_put: str,
                      profile: dict) -> str:
    tmpl = TEMPLATE.read_text()
    e = config.environments_cfg()
    base = e.get("odoo_image_base") or config.get("ODOO_IMAGE") or "odoo:19"
    deps = " ".join(profile.get("python_deps") or [])
    repl = {
        "__AWS_REGION__": _region(),
        "__CONTEXT_GET_URL__": context_get,
        "__RESULT_PUT_URL__": result_put,
        "__IMAGE_URI__": image_uri,
        "__ODOO_IMAGE_BASE__": base,
        "__ODOO_GIT_URL__": profile.get("odoo_git_url") or "https://github.com/odoo/odoo",
        # Never let an empty ref fall back to the upstream default branch
        # (master/latest) — that silently bakes a newer Odoo core than the
        # source data and breaks the registry (e.g. KeyError 'fold_name' when a
        # 19.x core loads a 17.0 dump). Pin to the discovered series branch.
        "__ODOO_GIT_REF__": (profile.get("odoo_git_ref")
                             or profile.get("odoo_series") or ""),
        "__CUSTOM_ADDONS_GIT_URL__": profile.get("addons_git_url") or "",
        "__CUSTOM_ADDONS_GIT_REF__": profile.get("addons_git_ref") or "",
        "__PYTHON_DEPS__": deps,
        "__GIT_TOKEN_SECRET__": profile.get("git_token_secret") or "",
    }
    for k, v in repl.items():
        tmpl = tmpl.replace(k, v)
    return tmpl


def _builder_settings() -> dict:
    """Launch settings for the ephemeral builder. Falls back to the developer-
    environment settings (same AMI/subnet/SG/instance-profile) but allows a
    dedicated `build.*` override and a beefier default instance type."""
    e = config.environments_cfg()
    b = (config.panel().get("build") or {}) if hasattr(config, "panel") else {}
    s = config.environments_settings()
    return {
        "ami_id": b.get("ami_id") or config.get("BUILD_AMI_ID") or s.get("ami_id"),
        "instance_type": b.get("instance_type") or config.get("BUILD_INSTANCE_TYPE") or "m5.xlarge",
        "subnet_id": b.get("subnet_id") or s.get("subnet_id"),
        "security_group_id": b.get("security_group_id") or s.get("security_group_id"),
        "instance_profile": (b.get("instance_profile")
                             or config.get("BUILD_INSTANCE_PROFILE")
                             or s.get("instance_profile")),
        "assign_public_ip": bool(b.get("assign_public_ip", s.get("assign_public_ip", True))),
        "volume_size": int(b.get("volume_size", 40)),
    }


def _launch_builder(user_data: str, image_uri: str, profile_id: str, s: dict) -> str:
    ec2 = boto3.client("ec2", region_name=_region())
    if not s.get("ami_id"):
        raise RuntimeError("no builder AMI configured (set ENV_AMI_ID or build.ami_id)")
    net = {"DeviceIndex": 0, "Groups": [s["security_group_id"]],
           "AssociatePublicIpAddress": s["assign_public_ip"]}
    if s.get("subnet_id"):
        net["SubnetId"] = s["subnet_id"]
    kwargs = {
        "ImageId": s["ami_id"],
        "InstanceType": s["instance_type"],
        "MinCount": 1, "MaxCount": 1,
        "UserData": user_data,
        "NetworkInterfaces": [net],
        "BlockDeviceMappings": [{
            "DeviceName": "/dev/xvda",
            "Ebs": {"VolumeSize": s["volume_size"], "VolumeType": "gp3",
                    "DeleteOnTermination": True},
        }],
        "InstanceInitiatedShutdownBehavior": "terminate",
        "TagSpecifications": [{
            "ResourceType": "instance",
            "Tags": [
                {"Key": "Name", "Value": f"odoo-synth-builder-{profile_id}"},
                {"Key": "odoo-synth:builder", "Value": profile_id},
                {"Key": "odoo-synth:managed", "Value": "true"},
            ],
        }],
    }
    if s.get("instance_profile"):
        kwargs["IamInstanceProfile"] = {"Name": s["instance_profile"]}
    resp = ec2.run_instances(**kwargs)
    return resp["Instances"][0]["InstanceId"]


def _launch_builder_workspace(image_uri: str, context_get: str, result_put: str,
                               profile: dict, s: dict) -> str:
    """Option E: launch the build as a Coder workspace from the
    odoo-synth-builder template. The workspace's startup_script runs the same
    build logic as builder-user-data.sh.tmpl (download context -> docker build
    -> push to ECR -> PUT result JSON to S3 -> poweroff). Returns the
    workspace name (the panel polls S3 for the result, exactly as before)."""
    import os
    import subprocess

    base = s.get("odoo_image_base") or config.get("ODOO_IMAGE") or "odoo:17"
    deps = " ".join(profile.get("python_deps") or [])
    odoo_ref = (profile.get("odoo_git_ref")
                or profile.get("odoo_series") or "")
    params = [
        ("ami_id", s.get("ami_id") or ""),
        ("instance_profile", s.get("instance_profile") or ""),
        ("subnet_id", s.get("subnet_id") or ""),
        ("security_group_id", s.get("security_group_id") or ""),
        ("region", _region()),
        ("instance_type", s.get("instance_type") or "m5.xlarge"),
        ("image_uri", image_uri),
        ("context_get_url", context_get),
        ("result_put_url", result_put),
        ("odoo_image_base", base),
        ("odoo_git_url", profile.get("odoo_git_url")
         or "https://github.com/odoo/odoo"),
        ("odoo_git_ref", odoo_ref),
        ("custom_addons_git_url", profile.get("addons_git_url") or ""),
        ("custom_addons_git_ref", profile.get("addons_git_ref") or ""),
        ("python_deps", deps),
        ("git_token_secret", profile.get("git_token_secret") or ""),
        ("issue", profile.get("id") or ""),
    ]
    ws_name = f"build-{uuid.uuid4().hex[:8]}"
    args = ["create", "-t", BUILDER_TEMPLATE, "-y", "--no-wait", ws_name]
    for k, v in params:
        args += ["--parameter", f"{k}={v}"]
    subprocess.run(["coder", *args], env={**os.environ, **_coder_env()},
                   check=True, capture_output=True, text=True, timeout=120)
    return ws_name


def _delete_builder_workspace(ws_name: str, emit: LogSink) -> None:
    """Delete the ephemeral builder workspace once its result has been
    collected. The workspace powers itself off on completion; this tears down
    the (now stopped) EC2 instance + Coder record so we don't accumulate idle
    builder VMs. Best-effort: a failure here is logged, not raised."""
    import os
    import subprocess

    try:
        subprocess.run(["coder", "delete", ws_name, "-y"],
                       env={**os.environ, **_coder_env()},
                       check=True, capture_output=True, text=True, timeout=120)
        emit(f"[panel] builder workspace {ws_name} deleted")
    except Exception as exc:  # noqa: BLE001
        emit(f"[panel] builder workspace {ws_name} cleanup failed: {exc}")


def _poll_result(get_url: str, emit: LogSink, timeout_s: int = 45 * 60) -> Optional[dict]:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        time.sleep(20)
        try:
            with urllib.request.urlopen(get_url, timeout=30) as r:  # noqa: S310
                return json.loads(r.read().decode())
        except Exception:  # noqa: BLE001 — 403/404 until the object exists
            emit("[panel] waiting for builder to finish ...")
    return None


def run_build(profile_id: str, emit: LogSink, run_id: str | None = None) -> dict:
    """Blocking: package context, launch the ephemeral builder, poll for its
    result, and fold the immutable image into the profile."""
    profile = store.get_profile(profile_id)
    if not profile:
        raise KeyError(profile_id)
    dhash = profile.get("discovery_hash")
    if not dhash:
        raise ValueError("run discovery first (no discovery_hash on the profile)")
    registry = _ecr_registry()
    proj = config.require("PROJECT")
    image_uri = f"{registry}/{proj}/odoo:{profile_id}-{dhash}"
    emit(f"[panel] target image: {image_uri}")

    store.update_profile(profile_id, image_status="building", error=None)

    emit("[panel] packaging odoo/ build context ...")
    include_ent = bool(profile.get("needs_enterprise")) and _have_enterprise_zip()
    if profile.get("needs_enterprise") and not include_ent:
        emit("[panel] NOTE profile needs enterprise but odoo/enterprise.zip is not "
             "present; the image will build without enterprise addons.")
    context_get, context_uri = _upload_context(profile_id, include_enterprise=include_ent)
    result_put, result_get, _key = _presign_result(profile_id)

    user_data = _render_user_data(image_uri, context_get, result_put, profile)
    s = _builder_settings()
    use_coder = _builder_use_coder()
    if use_coder:
        emit(f"[panel] launching Coder builder workspace ({s['instance_type']}) ...")
        try:
            iid = _launch_builder_workspace(image_uri, context_get, result_put,
                                            profile, s)
        except Exception as exc:  # noqa: BLE001
            store.update_profile(profile_id, image_status="failed", error=str(exc))
            raise
        emit(f"[panel] builder workspace {iid} launched; waiting for image build+push ...")
    else:
        emit(f"[panel] launching ephemeral builder ({s['instance_type']}) ...")
        try:
            iid = _launch_builder(user_data, image_uri, profile_id, s)
        except Exception as exc:  # noqa: BLE001
            store.update_profile(profile_id, image_status="failed", error=str(exc))
            raise
        emit(f"[panel] builder instance {iid} launched; waiting for image build+push ...")

    result = _poll_result(result_get, emit)
    # Option E: delete the ephemeral builder workspace now that its result is
    # collected (success or failure). The workspace already powered itself off;
    # this reclaims the EC2 instance + Coder record so idle VMs don't pile up.
    if use_coder:
        _delete_builder_workspace(iid, emit)
    if not result:
        store.update_profile(profile_id, image_status="failed",
                             error="builder timed out (no result)")
        return {"exit_code": 1, "error": "builder timed out"}

    tail = result.get("log_tail") or ""
    if tail:
        for ln in tail.splitlines()[-40:]:
            emit(ln)

    if result.get("status") != "succeeded":
        err = result.get("error") or "builder reported failure"
        store.update_profile(profile_id, image_status="failed", error=err)
        return {"exit_code": 1, "error": err}

    # success: rotate current image into history, set the new immutable image.
    history = list(profile.get("image_history") or [])
    if profile.get("image_uri"):
        history.append({"uri": profile["image_uri"], "created_at": time.time()})
    store.update_profile(
        profile_id,
        image_uri=image_uri,
        image_status="ready",
        image_history=history,
        error=None,
    )
    emit(f"[panel] image ready: {image_uri}")
    return {"exit_code": 0, "image_uri": image_uri, "context_uri": context_uri}


# ---------------------------------------------------------------------------
# retention / cleanup (decision 4: immutable images kept; explicit cleanup)
# ---------------------------------------------------------------------------

def _parse_image_uri(uri: str) -> tuple[str, str]:
    """Split '<registry>/<repo>:<tag>' into (repo, tag)."""
    ref, _, tag = uri.rpartition(":")
    repo = ref.split("/", 1)[1] if "/" in ref else ref
    return repo, tag


def list_images(profile_id: str) -> dict:
    """Return the profile's current image + history, enriched with live ECR
    metadata (pushed_at, size, whether the tag still exists)."""
    profile = store.get_profile(profile_id)
    if not profile:
        raise KeyError(profile_id)

    current = profile.get("image_uri")
    history = list(profile.get("image_history") or [])
    entries: list[dict] = []
    seen: set[str] = set()

    def add(uri: str, is_current: bool, created_at=None) -> None:
        if not uri or uri in seen:
            return
        seen.add(uri)
        entries.append({"uri": uri, "current": is_current,
                        "created_at": created_at})

    add(current, True)
    for h in reversed(history):
        add(h.get("uri"), False, h.get("created_at"))

    # enrich from ECR in one batch per repo
    proj = config.get("PROJECT")
    repo = f"{proj}/odoo" if proj else None
    meta: dict[str, dict] = {}
    if repo:
        try:
            ecr = boto3.client("ecr", region_name=_region())
            tags = [_parse_image_uri(e["uri"])[1] for e in entries]
            resp = ecr.describe_images(
                repositoryName=repo,
                imageIds=[{"imageTag": t} for t in tags if t])
            for d in resp.get("imageDetails", []):
                for t in d.get("imageTags", []):
                    meta[t] = {
                        "pushed_at": d.get("imagePushedAt").timestamp()
                        if d.get("imagePushedAt") else None,
                        "size_mb": round(d.get("imageSizeInBytes", 0) / 1e6, 1),
                    }
        except Exception:  # noqa: BLE001 — ECR unreachable or tags gone
            pass

    for e in entries:
        _, tag = _parse_image_uri(e["uri"])
        m = meta.get(tag)
        e["exists"] = m is not None
        e["pushed_at"] = m.get("pushed_at") if m else None
        e["size_mb"] = m.get("size_mb") if m else None

    return {"profile_id": profile_id, "current": current, "images": entries}


def delete_image(profile_id: str, image_uri: str) -> dict:
    """Delete an ECR tag from a profile's history. The *current* image cannot be
    deleted (guards against orphaning the ready image). Removes the entry from
    image_history and the ECR tag itself."""
    profile = store.get_profile(profile_id)
    if not profile:
        raise KeyError(profile_id)
    if image_uri == profile.get("image_uri"):
        raise ValueError("cannot delete the profile's current image")

    history = [h for h in (profile.get("image_history") or [])
               if h.get("uri") != image_uri]

    proj = config.get("PROJECT")
    deleted = False
    if proj:
        repo, tag = _parse_image_uri(image_uri)
        try:
            ecr = boto3.client("ecr", region_name=_region())
            ecr.batch_delete_image(repositoryName=repo,
                                   imageIds=[{"imageTag": tag}])
            deleted = True
        except Exception:  # noqa: BLE001 — already gone / not found
            pass

    store.update_profile(profile_id, image_history=history)
    return {"deleted": deleted, "image_uri": image_uri}

