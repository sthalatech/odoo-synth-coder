"""ECS/Fargate orchestration via boto3 — mirrors deploy/07_mask.sh and
deploy/source/restore_dump.sh, but runs in-process so we can tail the container's
CloudWatch logs live and stream them to the browser.

Two operations:
  * restore  -> stream a presigned dump.sql into the SOURCE db (masker image)
  * mask     -> greenmask SOURCE -> MASKED, neutralize, set admin password
"""
from __future__ import annotations
import time
from typing import Callable, Optional

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


# ---------------------------------------------------------------------------
# task-definition builders
# ---------------------------------------------------------------------------

def _restore_command() -> str:
    """Identical restore logic to deploy/source/restore_dump.sh, parameterised by
    the DUMP_URL env var so the same task def can be reused across runs."""
    host = config.require("SRC_RDS_ENDPOINT")
    user = config.require("SOURCE_DB_MASTER_USER")
    db = config.require("SOURCE_DB_NAME")
    pw = config.require("SOURCE_DB_MASTER_PASSWORD")
    return f'''set -o pipefail
export PGPASSWORD="{pw}"
H="{host}"; U="{user}"; DB="{db}"
ADM="psql -v ON_ERROR_STOP=1 -h $H -U $U -d postgres"
echo "[restore] recreating database $DB ..."
$ADM -c "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname='$DB' AND pid<>pg_backend_pid();" >/dev/null 2>&1 || true
$ADM -c "DROP DATABASE IF EXISTS \\"$DB\\";"
$ADM -c "CREATE DATABASE \\"$DB\\" ENCODING 'UTF8' TEMPLATE template0;"
echo "[restore] streaming dump into $DB (this takes a few minutes) ..."
curl -fsSL "$DUMP_URL" | psql -h "$H" -U "$U" -d "$DB" >/tmp/restore.log 2>&1
echo "[restore] psql stream exit=$? (non-fatal errors tolerated); tail:"
tail -5 /tmp/restore.log || true
P=$(psql -tA -h "$H" -U "$U" -d "$DB" -c "SELECT count(*) FROM res_partner;" 2>/dev/null || echo "?")
US=$(psql -tA -h "$H" -U "$U" -d "$DB" -c "SELECT count(*) FROM res_users;" 2>/dev/null || echo "?")
echo "[restore] DONE: res_partner=$P res_users=$US in $DB"'''


def _register_restore_taskdef(ecs, dump_url: str) -> tuple[str, str, str, str]:
    """Returns (family, container, log_group, stream_prefix)."""
    proj = config.require("PROJECT")
    family = f"{proj}-src-restore"
    log_group = f"/ecs/{proj}-source"
    prefix = "restore"
    container = "restore"
    ecs.register_task_definition(
        family=family,
        networkMode="awsvpc",
        requiresCompatibilities=["FARGATE"],
        cpu="1024",
        memory="2048",
        executionRoleArn=config.require("EXEC_ARN"),
        containerDefinitions=[
            {
                "name": container,
                "image": f"{_ecr()}/{proj}/masker:latest",
                "entryPoint": ["bash", "-lc"],
                "command": [_restore_command()],
                "environment": [{"name": "DUMP_URL", "value": dump_url}],
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


def _register_mask_taskdef(ecs, admin_password: Optional[str]) -> tuple[str, str, str, str]:
    proj = config.require("PROJECT")
    family = f"{proj}-mask"
    log_group = f"/ecs/{proj}"
    prefix = "mask"
    container = "masker"

    def kv(k: str, v: str) -> dict:
        return {"name": k, "value": v}

    src_host = config.get("SRC_RDS_ENDPOINT") or config.require("RDS_ENDPOINT")
    src_user = config.get("SOURCE_DB_MASTER_USER") or config.require("TARGET_DB_USER")
    src_pw = config.get("SOURCE_DB_MASTER_PASSWORD") or config.require("TARGET_DB_PASSWORD")
    env = [
        kv("SOURCE_DB_HOST", src_host),
        kv("SOURCE_DB_PORT", "5432"),
        kv("SOURCE_DB_NAME", config.require("SOURCE_DB_NAME")),
        kv("SOURCE_DB_USER", src_user),
        kv("SOURCE_DB_PASSWORD", src_pw),
        kv("TARGET_DB_HOST", config.require("RDS_ENDPOINT")),
        kv("TARGET_DB_PORT", "5432"),
        kv("TARGET_DB_NAME", config.require("TARGET_DB_NAME")),
        kv("TARGET_DB_USER", config.require("TARGET_DB_USER")),
        kv("TARGET_DB_PASSWORD", config.require("TARGET_DB_PASSWORD")),
        kv("ODOO_ADMIN_PASSWORD", admin_password or config.require("ODOO_ADMIN_PASSWORD")),
    ]
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
# run + live log tail
# ---------------------------------------------------------------------------

def _tail_until_stopped(
    ecs, logs, cluster: str, task_arn: str,
    log_group: str, log_stream: str, emit: LogSink,
) -> int:
    """Poll CloudWatch for the task's log stream and forward new lines to `emit`
    until the task stops. Returns the container exit code."""
    token: Optional[str] = None
    stopped = False
    exit_code = 1
    # allow a short grace period for the stream to appear
    stream_ready = False
    idle_after_stop = 0

    while True:
        # forward available log events
        try:
            kwargs = {
                "logGroupName": log_group,
                "logStreamName": log_stream,
                "startFromHead": True,
            }
            if token:
                kwargs["nextToken"] = token
            resp = logs.get_log_events(**kwargs)
            stream_ready = True
            for ev in resp.get("events", []):
                emit(ev["message"].rstrip("\n"))
            new_token = resp.get("nextForwardToken")
            got_events = bool(resp.get("events"))
            token = new_token
        except logs.exceptions.ResourceNotFoundException:
            got_events = False  # stream not created yet

        if stopped:
            # drain a couple extra cycles after stop to catch trailing logs
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


def run_operation(operation: str, params: dict, emit: LogSink) -> dict:
    """Execute a masking operation synchronously (call from a worker thread).
    Streams progress via `emit`. Returns a result dict."""
    ecs, ec2, logs = _clients()

    if operation == "restore":
        dump_url = params.get("dump_url")
        if not dump_url:
            raise ValueError("restore requires dump_url (presigned S3 URL)")
        cluster = config.require("SOURCE_ECS_CLUSTER")
        sg = config.require("SRC_TASK_SG")
        emit("[panel] registering restore task definition ...")
        family, container, log_group, prefix = _register_restore_taskdef(ecs, dump_url)
    elif operation == "mask":
        cluster = config.require("ECS_CLUSTER")
        sg = config.require("TASK_SG")
        emit("[panel] registering mask task definition ...")
        family, container, log_group, prefix = _register_mask_taskdef(
            ecs, params.get("admin_password")
        )
    else:
        raise ValueError(f"unknown operation: {operation}")

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

    exit_code = _tail_until_stopped(
        ecs, logs, cluster, task_arn, log_group, log_stream, emit
    )
    emit(f"[panel] task exited with code {exit_code}")

    result: dict = {"task_arn": task_arn, "exit_code": exit_code}
    if operation == "mask" and exit_code == 0:
        alb = config.get("ALB_DNS")
        if alb:
            result["target_url"] = f"http://{alb}/web/login"
    if operation == "restore" and exit_code == 0:
        src_alb = config.get("SRC_ALB_DNS")
        if src_alb:
            result["source_url"] = f"http://{src_alb}/web/login"
    if exit_code != 0:
        result["error"] = f"task exited non-zero ({exit_code})"
    return result
