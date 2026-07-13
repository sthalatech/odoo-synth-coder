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


def _region() -> str:
    return config.require("AWS_REGION")


def _ecr_registry() -> str:
    acct = boto3.client("sts", region_name=_region()).get_caller_identity()["Account"]
    return f"{acct}.dkr.ecr.{_region()}.amazonaws.com"


def _make_context_tarball() -> bytes:
    """Tar.gz the odoo/ build context (excluding a few heavy/irrelevant dirs)."""
    buf = io.BytesIO()
    skip = {".git", "enterprise", "custom-addons"}
    skip_files = {"enterprise.zip"}
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


def _upload_context(profile_id: str) -> tuple[str, str]:
    """Upload the context tarball; return (get_url, s3_uri)."""
    bucket = _bucket()
    prefix = config.dump_s3_prefix().rstrip("/").rsplit("/", 1)[0] + "/builds"
    key = f"{prefix}/{profile_id}/{uuid.uuid4().hex[:12]}/context.tgz"
    s3 = _s3()
    s3.put_object(Bucket=bucket, Key=key, Body=_make_context_tarball())
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
        "__ODOO_GIT_REF__": profile.get("odoo_git_ref") or "",
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


def run_build(profile_id: str, emit: LogSink) -> dict:
    """Blocking: package context, launch the ephemeral builder, poll for its
    result, and fold the immutable image into the profile."""
    profile = store.get_profile(profile_id)
    if not profile:
        raise KeyError(profile_id)
    dhash = profile.get("discovery_hash")
    if not dhash:
        raise ValueError("run discovery first (no discovery_hash on the profile)")
    if profile.get("needs_enterprise") and not profile.get("enterprise_source"):
        emit("[panel] NOTE profile needs enterprise but no enterprise_source set; "
             "the image will build without enterprise addons.")

    registry = _ecr_registry()
    proj = config.require("PROJECT")
    image_uri = f"{registry}/{proj}/odoo:{profile_id}-{dhash}"
    emit(f"[panel] target image: {image_uri}")

    store.update_profile(profile_id, image_status="building", error=None)

    emit("[panel] packaging odoo/ build context ...")
    context_get, context_uri = _upload_context(profile_id)
    result_put, result_get, _key = _presign_result(profile_id)

    user_data = _render_user_data(image_uri, context_get, result_put, profile)
    s = _builder_settings()
    emit(f"[panel] launching ephemeral builder ({s['instance_type']}) ...")
    try:
        iid = _launch_builder(user_data, image_uri, profile_id, s)
    except Exception as exc:  # noqa: BLE001
        store.update_profile(profile_id, image_status="failed", error=str(exc))
        raise
    emit(f"[panel] builder instance {iid} launched; waiting for image build+push ...")

    result = _poll_result(result_get, emit)
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
