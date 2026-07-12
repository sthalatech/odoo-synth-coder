"""ECS/Fargate orchestration via boto3.

Single operation: **mask**.
  * SOURCE      = a live Postgres DB the user points at (a connection URL/DSN).
                  greenmask dumps + masks it directly.
  * DESTINATION = the managed masked DB on RDS (from config, resolved server-side).
                  Always dropped + recreated by the masker.
  * OUTPUT      = optionally a downloadable pg_dump of the masked DB (uploaded to
                  S3 via a presigned PUT; a presigned GET is returned to the UI).

Runs the masker Fargate task in-process so we can tail its CloudWatch logs live.
"""
from __future__ import annotations
import time
import uuid
from typing import Callable, Optional
from urllib.parse import urlparse, unquote

import boto3

from . import config

LogSink = Callable[[str], None]


def _region() -> str:
    return config.require("AWS_REGION")


def _clients():
    r = _region()
    return (
        boto3.client("ecs", region_name=r),
        boto3.client("ec2", region_name=r),
        boto3.client("logs", region_name=r),
    )


def _default_subnets(ec2) -> list[str]:
    vpcs = ec2.describe_vpcs(Filters=[{"Name": "isDefault", "Values": ["true"]}])["Vpcs"]
    vpc_id = vpcs[0]["VpcId"]
    subs = ec2.describe_subnets(
        Filters=[
            {"Name": "vpc-id", "Values": [vpc_id]},
            {"Name": "default-for-az", "Values": ["true"]},
        ]
    )["Subnets"]
    return [s["SubnetId"] for s in subs]


def _net_config(ec2, sg: str) -> dict:
    return {
        "awsvpcConfiguration": {
            "subnets": _default_subnets(ec2),
            "securityGroups": [sg],
            "assignPublicIp": "ENABLED",
        }
    }


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


# ---------------------------------------------------------------------------
# masked-dump download staging (presigned PUT for upload, GET for download)
# ---------------------------------------------------------------------------

def _presign_masked_dump() -> tuple[str, str]:
    """Return (put_url, get_url) for a fresh masked-dump object, or raise."""
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
    return put_url, get_url


# ---------------------------------------------------------------------------
# mask task definition
# ---------------------------------------------------------------------------

def _register_mask_taskdef(ecs, src: dict, tgt: dict, params: dict,
                           masked_dump_put_url: Optional[str]) -> tuple[str, str, str, str]:
    proj = config.require("PROJECT")
    family = f"{proj}-mask"
    log_group = f"/ecs/{proj}"
    prefix, container = "mask", "masker"

    def kv(k: str, v) -> dict:
        return {"name": k, "value": str(v)}

    nd = config.neutralize_defaults()

    def flag(key: str, default: bool) -> str:
        v = params.get(key, default)
        return "true" if v else "false"

    env = [
        kv("SOURCE_DB_HOST", src["host"]),
        kv("SOURCE_DB_PORT", src["port"]),
        kv("SOURCE_DB_NAME", src["dbname"]),
        kv("SOURCE_DB_USER", src["user"]),
        kv("SOURCE_DB_PASSWORD", src["password"]),
        kv("TARGET_DB_HOST", tgt["host"]),
        kv("TARGET_DB_PORT", tgt["port"]),
        kv("TARGET_DB_NAME", tgt["dbname"]),
        kv("TARGET_DB_USER", tgt["user"]),
        kv("TARGET_DB_PASSWORD", tgt["password"]),
        kv("ODOO_ADMIN_PASSWORD", params.get("admin_password") or config.get("ODOO_ADMIN_PASSWORD", "admin")),
        kv("MASK_PROFILE", params.get("mask_profile") or "odoo-core-pii"),
        kv("GM_JOBS", params.get("gm_jobs") or nd.get("gm_jobs", 4)),
        kv("NEUTRALIZE_MAIL", flag("neutralize_mail", nd.get("mail", True))),
        kv("NEUTRALIZE_FETCHMAIL", flag("neutralize_fetchmail", nd.get("fetchmail", True))),
        kv("NEUTRALIZE_PAYMENT", flag("neutralize_payment", nd.get("payment", True))),
        kv("NEUTRALIZE_SMTP_PARAM", flag("neutralize_smtp_param", nd.get("smtp_param", True))),
        kv("RESET_ADMIN_LOGIN", flag("reset_admin_login", config.panel().get("reset_admin_login", True))),
    ]
    if masked_dump_put_url:
        env.append(kv("MASKED_DUMP_PUT_URL", masked_dump_put_url))

    ecs.register_task_definition(
        family=family,
        networkMode="awsvpc",
        requiresCompatibilities=["FARGATE"],
        cpu=config.get("TASK_CPU", "1024"),
        memory=config.get("TASK_MEM", "2048"),
        executionRoleArn=config.require("EXEC_ARN"),
        containerDefinitions=[
            {
                "name": container,
                "image": f"{_ecr()}/{proj}/masker:latest",
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


# ---------------------------------------------------------------------------
# live log tail
# ---------------------------------------------------------------------------

def _tail_until_stopped(ecs, logs, cluster, task_arn, log_group, log_stream, emit) -> int:
    token: Optional[str] = None
    stopped = False
    exit_code = 1
    stream_ready = False
    idle_after_stop = 0

    while True:
        try:
            kwargs = {"logGroupName": log_group, "logStreamName": log_stream, "startFromHead": True}
            if token:
                kwargs["nextToken"] = token
            resp = logs.get_log_events(**kwargs)
            stream_ready = True
            for ev in resp.get("events", []):
                emit(ev["message"].rstrip("\n"))
            token = resp.get("nextForwardToken")
            got_events = bool(resp.get("events"))
        except logs.exceptions.ResourceNotFoundException:
            got_events = False

        if stopped:
            idle_after_stop += 0 if got_events else 1
            if idle_after_stop >= 2:
                break

        if not stopped:
            desc = ecs.describe_tasks(cluster=cluster, tasks=[task_arn])["tasks"]
            if desc:
                t = desc[0]
                if t.get("lastStatus") == "STOPPED":
                    stopped = True
                    conts = t.get("containers", [])
                    if conts and conts[0].get("exitCode") is not None:
                        exit_code = conts[0]["exitCode"]
                    reason = t.get("stoppedReason")
                    if reason:
                        emit(f"[task stopped] {reason}")

        time.sleep(2 if (got_events or not stream_ready) else 3)

    return exit_code


# ---------------------------------------------------------------------------
# entry
# ---------------------------------------------------------------------------

def run_operation(operation: str, params: dict, emit: LogSink) -> dict:
    if operation != "mask":
        raise ValueError(f"unknown operation: {operation}")

    ecs, ec2, logs = _clients()

    # SOURCE: a live DB the user pointed at (DSN)
    dsn = params.get("source_dsn")
    if not dsn:
        raise ValueError("source database URL (postgresql://…) is required")
    src = parse_dsn(dsn)

    # DESTINATION: managed masked DB on RDS (from config)
    tgt = config.destination()
    if not tgt.get("host"):
        raise RuntimeError("destination not configured (check RDS_ENDPOINT / config.yml)")

    # optional downloadable masked dump
    masked_dump_get_url = None
    masked_dump_put_url = None
    if params.get("produce_dump"):
        masked_dump_put_url, masked_dump_get_url = _presign_masked_dump()
        emit("[panel] masked dump download requested; will upload pg_dump to S3")

    cluster = config.require("ECS_CLUSTER")
    sg = config.require("TASK_SG")
    emit(f"[panel] mask source={src['user']}@{src['host']}:{src['port']}/{src['dbname']} "
         f"-> {tgt['dbname']}@{tgt['host']} profile={params.get('mask_profile')}")

    family, container, log_group, prefix = _register_mask_taskdef(
        ecs, src, tgt, params, masked_dump_put_url
    )

    emit(f"[panel] launching Fargate task ({family}) on cluster {cluster} ...")
    resp = ecs.run_task(
        cluster=cluster,
        launchType="FARGATE",
        taskDefinition=family,
        networkConfiguration=_net_config(ec2, sg),
        count=1,
    )
    failures = resp.get("failures") or []
    if failures:
        raise RuntimeError(f"run_task failed: {failures}")
    task_arn = resp["tasks"][0]["taskArn"]
    task_id = task_arn.split("/")[-1]
    log_stream = f"{prefix}/{container}/{task_id}"
    emit(f"[panel] task {task_id} started; streaming logs from {log_group}:{log_stream}")

    exit_code = _tail_until_stopped(ecs, logs, cluster, task_arn, log_group, log_stream, emit)
    emit(f"[panel] task exited with code {exit_code}")

    result: dict = {"task_arn": task_arn, "exit_code": exit_code}
    if exit_code == 0:
        alb = config.get("ALB_DNS")
        if alb:
            result["target_url"] = f"http://{alb}/web/login"
        if masked_dump_get_url:
            result["masked_dump_url"] = masked_dump_get_url
    elif exit_code == 3:
        result["error"] = ("preflight failed: source or destination DB was not "
                            "reachable from the masker task (check the URL, "
                            "credentials, and network/security-group access).")
    else:
        result["error"] = f"task exited non-zero ({exit_code})"
    return result
