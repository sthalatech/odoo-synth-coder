#!/usr/bin/env bash
# SOURCE stack: restore a real Odoo backup's dump.sql into the source DB on the
# source RDS. The dump is provided via a presigned S3 URL in DUMP_URL (streamed
# straight into psql inside a Fargate task, so nothing large lands on local disk
# or in the container image). Scale the source Odoo service to 0 before running
# this so it isn't holding connections to the DB being dropped.
#
# Usage: DUMP_URL="https://...presigned..." bash deploy/source/restore_dump.sh
source "$(dirname "$0")/../lib.sh"
: "${SRC_RDS_ENDPOINT:?run source/02_rds.sh}" "${EXEC_ARN:?}" "${SRC_TASK_SG:?}"
: "${DUMP_URL:?set DUMP_URL to a presigned S3 URL for dump.sql}"

# The restore command runs inside the masker image (has psql 16 + curl). It
# recreates the target DB, streams the dump in (tolerating benign restore
# errors), then prints row counts as a sanity check.
CMD='set -o pipefail
export PGPASSWORD="'"$SOURCE_DB_MASTER_PASSWORD"'"
H="'"$SRC_RDS_ENDPOINT"'"; U="'"$SOURCE_DB_MASTER_USER"'"; DB="'"$SOURCE_DB_NAME"'"
ADM="psql -v ON_ERROR_STOP=1 -h $H -U $U -d postgres"
echo "[restore] recreating database $DB ..."
$ADM -c "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname='"'"'$DB'"'"' AND pid<>pg_backend_pid();" >/dev/null 2>&1 || true
$ADM -c "DROP DATABASE IF EXISTS \"$DB\";"
$ADM -c "CREATE DATABASE \"$DB\" ENCODING '"'"'UTF8'"'"' TEMPLATE template0;"
echo "[restore] streaming dump into $DB (this takes a few minutes) ..."
curl -fsSL "$DUMP_URL" | psql -h "$H" -U "$U" -d "$DB" >/tmp/restore.log 2>&1
echo "[restore] psql stream exit=$? (non-fatal errors tolerated); tail:"
tail -5 /tmp/restore.log || true
P=$(psql -tA -h "$H" -U "$U" -d "$DB" -c "SELECT count(*) FROM res_partner;" 2>/dev/null || echo "?")
US=$(psql -tA -h "$H" -U "$U" -d "$DB" -c "SELECT count(*) FROM res_users;" 2>/dev/null || echo "?")
echo "[restore] DONE: res_partner=$P res_users=$US in $DB"'

python3 - "$PROJECT" "$EXEC_ARN" "$ECR" "$AWS_REGION" "$DUMP_URL" "$CMD" > /tmp/td-src-restore.json <<'PY'
import json,sys
proj,exec_arn,ecr,region,dump_url,cmd=sys.argv[1:7]
td={"family":f"{proj}-src-restore","networkMode":"awsvpc",
"requiresCompatibilities":["FARGATE"],"cpu":"1024","memory":"2048",
"executionRoleArn":exec_arn,
"containerDefinitions":[{"name":"restore","image":f"{ecr}/{proj}/masker:latest",
"entryPoint":["bash","-lc"],"command":[cmd],
"environment":[{"name":"DUMP_URL","value":dump_url}],
"logConfiguration":{"logDriver":"awslogs","options":{
"awslogs-group":f"/ecs/{proj}-source","awslogs-region":region,
"awslogs-stream-prefix":"restore"}}}]}
print(json.dumps(td))
PY
aws ecs register-task-definition --cli-input-json file:///tmp/td-src-restore.json --region "$AWS_REGION" >/dev/null
log "restoring prod dump into source db '$SOURCE_DB_NAME' (Fargate task; several minutes) ..."
CODE="$(run_task_wait_on "$SOURCE_ECS_CLUSTER" "$PROJECT-src-restore" "$SRC_TASK_SG")"
[ "$CODE" = "0" ] || { log "source dump restore FAILED (exit $CODE); check /ecs/$PROJECT-source"; exit 1; }
log "source dump restore complete"
