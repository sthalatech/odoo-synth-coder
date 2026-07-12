"""ECS/Fargate orchestration via boto3 — mirrors deploy/07_mask.sh and
deploy/source/restore_dump.sh, but runs in-process so we can tail the container's
CloudWatch logs live and stream them to the browser.

Operations
  * restore  -> load a dump into a target DB. Source of the dump is selectable:
                sql_url | zip_url | sql_upload | zip_upload | db_dsn.
  * mask     -> greenmask SOURCE -> TARGET, neutralize (toggleable), set admin
                password. Connections, profile, jobs and toggles are all inputs.
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


def _resolve_conn(conn_id: str) -> dict:
    c = config.get_connection(conn_id)
    if not c:
        raise ValueError(f"unknown connection profile: {conn_id}")
    if not c.get("host"):
        raise ValueError(f"connection '{conn_id}' has no host (check env vars)")
    return c


# ---------------------------------------------------------------------------
# RESTORE
# ---------------------------------------------------------------------------

def _restore_command(target: dict, db_name: str, source_type: str,
                     url: Optional[str], dsn: Optional[str]) -> str:
    """Build the restore shell script for the chosen source type. Runs inside the
    masker image (curl + psql16 + pg_dump16 + python3). Recreates db_name on the
    target connection, loads the dump, prints row counts."""
    host, port, user, pw = target["host"], target["port"], target["user"], target["password"]

    header = f'''set -o pipefail
export PGPASSWORD="{pw}"
H="{host}"; PORT="{port}"; U="{user}"; DB="{db_name}"
ADM="psql -v ON_ERROR_STOP=1 -h $H -p $PORT -U $U -d postgres"
echo "[restore] recreating database $DB on $H ..."
$ADM -c "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname='$DB' AND pid<>pg_backend_pid();" >/dev/null 2>&1 || true
$ADM -c "DROP DATABASE IF EXISTS \\"$DB\\";"
$ADM -c "CREATE DATABASE \\"$DB\\" ENCODING 'UTF8' TEMPLATE template0;"'''

    if source_type in ("sql_url", "sql_upload"):
        body = f'''
echo "[restore] streaming SQL dump into $DB ..."
curl -fsSL "{url}" | psql -h "$H" -p "$PORT" -U "$U" -d "$DB" >/tmp/restore.log 2>&1
echo "[restore] psql stream exit=$? (non-fatal errors tolerated); tail:"
tail -5 /tmp/restore.log || true'''
    elif source_type in ("zip_url", "zip_upload"):
        body = f'''
echo "[restore] downloading backup zip ..."
curl -fsSL "{url}" -o /tmp/backup.zip
echo "[restore] extracting dump.sql from zip ..."
python3 - <<'PY' > /tmp/dump.sql
import zipfile,sys
z=zipfile.ZipFile("/tmp/backup.zip")
names=[n for n in z.namelist() if n.endswith("dump.sql") or n=="dump.sql"]
if not names:
    sys.stderr.write("no dump.sql in zip: %r\\n"%z.namelist()); sys.exit(2)
sys.stdout.buffer.write(z.read(names[0]))
PY
echo "[restore] streaming extracted dump.sql into $DB ..."
psql -h "$H" -p "$PORT" -U "$U" -d "$DB" -f /tmp/dump.sql >/tmp/restore.log 2>&1
echo "[restore] psql exit=$? (non-fatal errors tolerated); tail:"
tail -5 /tmp/restore.log || true'''
    elif source_type == "db_dsn":
        body = f'''
echo "[restore] pg_dump from live source DSN -> $DB (streaming) ..."
pg_dump --no-owner --no-privileges "{dsn}" | psql -h "$H" -p "$PORT" -U "$U" -d "$DB" >/tmp/restore.log 2>&1
echo "[restore] stream exit=$? (non-fatal errors tolerated); tail:"
tail -5 /tmp/restore.log || true'''
    else:
        raise ValueError(f"unknown restore source_type: {source_type}")

    footer = '''
P=$(psql -tA -h "$H" -p "$PORT" -U "$U" -d "$DB" -c "SELECT count(*) FROM res_partner;" 2>/dev/null || echo "?")
US=$(psql -tA -h "$H" -p "$PORT" -U "$U" -d "$DB" -c "SELECT count(*) FROM res_users;" 2>/dev/null || echo "?")
echo "[restore] DONE: res_partner=$P res_users=$US in $DB"'''
    return header + body + footer


def _register_restore_taskdef(ecs, command: str) -> tuple[str, str, str, str]:
    proj = config.require("PROJECT")
    family = f"{proj}-src-restore"
    log_group = f"/ecs/{proj}-source"
    prefix, container = "restore", "restore"
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
                "command": [command],
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
# MASK
# ---------------------------------------------------------------------------

def _register_mask_taskdef(ecs, src: dict, tgt: dict, params: dict) -> tuple[str, str, str, str]:
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
        kv("SOURCE_DB_NAME", params.get("source_db") or src["dbname"]),
        kv("SOURCE_DB_USER", src["user"]),
        kv("SOURCE_DB_PASSWORD", src["password"]),
        kv("TARGET_DB_HOST", tgt["host"]),
        kv("TARGET_DB_PORT", tgt["port"]),
        kv("TARGET_DB_NAME", params.get("target_db") or tgt["dbname"]),
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
    ecs, ec2, logs = _clients()

    if operation == "restore":
        target = _resolve_conn(params.get("target_conn") or "source")
        db_name = params.get("target_db") or target["dbname"]
        source_type = params.get("source_type")
        url = params.get("url")
        dsn = params.get("dsn")
        if source_type in ("sql_url", "zip_url", "sql_upload", "zip_upload") and not url:
            raise ValueError(f"{source_type} requires a resolved URL")
        if source_type == "db_dsn" and not dsn:
            raise ValueError("db_dsn requires a source DSN")
        cluster = config.require("SOURCE_ECS_CLUSTER")
        sg = config.require("SRC_TASK_SG")
        emit(f"[panel] restore source={source_type} target-conn={target['id']} db={db_name}")
        cmd = _restore_command(target, db_name, source_type, url, dsn)
        family, container, log_group, prefix = _register_restore_taskdef(ecs, cmd)

    elif operation == "mask":
        src = _resolve_conn(params.get("source_conn") or "source")
        tgt = _resolve_conn(params.get("target_conn") or "masked")
        cluster = config.require("ECS_CLUSTER")
        sg = config.require("TASK_SG")
        tdb = params.get("target_db") or tgt["dbname"]
        emit(f"[panel] mask {src['id']}({src['dbname']}) -> {tgt['id']}({tdb}) profile={params.get('mask_profile')}")
        family, container, log_group, prefix = _register_mask_taskdef(ecs, src, tgt, params)
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

    exit_code = _tail_until_stopped(ecs, logs, cluster, task_arn, log_group, log_stream, emit)
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
