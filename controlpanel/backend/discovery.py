"""Provenance discovery orchestration.

Launches the *discovery* Fargate task for a profile: it inspects the live source
DB + addons repo and uploads a discovery.json to S3. On success we fold the
result back into the profile (odoo_series, installed_modules, python_deps,
apt_deps, discovery_yaml_uri) and advance image_status to ``discovered``.

Reuses the ECS/log-tail plumbing from :mod:`pipeline` so discovery streams live
to the same run-log UI.
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


def _register_taskdef(ecs, profile: dict, put_url: str) -> tuple[str, str, str, str]:
    proj = config.require("PROJECT")
    family = f"{proj}-discover"
    log_group = f"/ecs/{proj}"
    prefix, container = "discover", "discover"

    def kv(k: str, v) -> dict:
        return {"name": k, "value": str(v)}

    conn = profile.get("source_conn") or {}
    password = profiles._get_secret(profile.get("source_password_secret"))
    env = [
        kv("PROFILE_ID", profile["id"]),
        kv("SOURCE_DB_HOST", conn.get("host", "")),
        kv("SOURCE_DB_PORT", conn.get("port", 5432)),
        kv("SOURCE_DB_NAME", conn.get("dbname", "")),
        kv("SOURCE_DB_USER", conn.get("user", "")),
        kv("SOURCE_DB_PASSWORD", password),
        kv("ODOO_GIT_REF", profile.get("odoo_git_ref") or ""),
        kv("ADDONS_GIT_URL", profile.get("addons_git_url") or ""),
        kv("ADDONS_GIT_REF", profile.get("addons_git_ref") or ""),
        kv("GIT_TOKEN", profiles._get_secret(profile.get("git_token_secret"))),
        kv("DISCOVERY_PUT_URL", put_url),
    ]
    if conn.get("ssh_enabled") and conn.get("ssh_bastion"):
        b = pipeline.parse_bastion(conn["ssh_bastion"])
        env += [
            kv("SSH_ENABLED", "true"),
            kv("SSH_BASTION_HOST", b["host"]),
            kv("SSH_BASTION_USER", b["user"]),
            kv("SSH_BASTION_PORT", b["port"]),
            kv("SSH_PRIVATE_KEY", profiles._get_secret(profile.get("ssh_key_secret"))),
        ]

    ecs.register_task_definition(
        family=family,
        networkMode="awsvpc",
        requiresCompatibilities=["FARGATE"],
        cpu=config.get("DISCOVER_CPU", "1024"),
        memory=config.get("DISCOVER_MEM", "2048"),
        executionRoleArn=config.require("EXEC_ARN"),
        containerDefinitions=[
            {
                "name": container,
                "image": f"{pipeline._ecr()}/{proj}/discovery:latest",
                "environment": env,
                "logConfiguration": {
                    "logDriver": "awslogs",
                    "options": {
                        "awslogs-group": log_group,
                        "awslogs-region": _region(),
                        "awslogs-stream-prefix": prefix,
                    },
                },
            }
        ],
    )
    return family, container, log_group, prefix


def run_discovery(profile_id: str, emit: LogSink) -> dict:
    """Blocking: launch the discovery task, stream logs, fold results into the
    profile. Returns a small result dict (exit_code, discovery_uri, hash)."""
    profile = store.get_profile(profile_id)
    if not profile:
        raise KeyError(profile_id)
    conn = profile.get("source_conn") or {}
    if not conn.get("host"):
        raise ValueError("profile has no source connection; add a source DB URL first")

    ecs, ec2, logs = pipeline._clients()
    put_url, get_url, s3_uri = _presign_discovery(profile_id)
    emit(f"[panel] discovery output -> {s3_uri}")

    family, container, log_group, prefix = _register_taskdef(ecs, profile, put_url)
    cluster = config.require("ECS_CLUSTER")
    sg = config.require("TASK_SG")

    store.update_profile(profile_id, image_status="discovering", error=None)
    emit(f"[panel] launching discovery task ({family}) on {cluster} ...")
    resp = ecs.run_task(
        cluster=cluster, launchType="FARGATE", taskDefinition=family,
        networkConfiguration=pipeline._net_config(ec2, sg), count=1)
    failures = resp.get("failures") or []
    if failures:
        store.update_profile(profile_id, image_status="failed",
                             error=f"run_task failed: {failures}")
        raise RuntimeError(f"run_task failed: {failures}")

    task_arn = resp["tasks"][0]["taskArn"]
    task_id = task_arn.split("/")[-1]
    log_stream = f"{prefix}/{container}/{task_id}"
    emit(f"[panel] task {task_id} started; streaming logs ...")
    exit_code = pipeline._tail_until_stopped(
        ecs, logs, cluster, task_arn, log_group, log_stream, emit)
    emit(f"[panel] discovery task exited with code {exit_code}")

    if exit_code != 0:
        store.update_profile(profile_id, image_status="failed",
                             error=f"discovery task exited {exit_code}")
        return {"exit_code": exit_code, "error": f"discovery task exited {exit_code}"}

    # fetch the discovery.json we just produced and fold it into the profile
    data = _fetch_discovery(get_url)
    fields: dict = {
        "discovery_yaml_uri": s3_uri,
        "installed_modules": data.get("installed_modules") or [],
        "python_deps": data.get("python_deps") or [],
        "apt_deps": data.get("apt_deps") or [],
        "image_status": "discovered",
        "error": None,
    }
    if data.get("odoo_series") and not profile.get("odoo_series"):
        fields["odoo_series"] = data["odoo_series"]
    store.update_profile(profile_id, **fields)

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
