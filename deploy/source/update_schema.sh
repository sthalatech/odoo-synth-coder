#!/usr/bin/env bash
# SOURCE stack: one-off schema migration. Runs `odoo-bin -d <db> -u all
# --stop-after-init` inside the odoo image so the installed modules' schema is
# reconciled with the (newer, baked) enterprise/custom code after restoring an
# older prod dump. The entrypoint forwards these args to odoo-bin, so the DB
# connection + addons_path come from the same rendered odoo.conf as the server.
#
# Scale the source Odoo service to 0 before running this. Usage:
#   bash deploy/source/update_schema.sh [modules]     (default: all)
source "$(dirname "$0")/../lib.sh"
: "${SRC_RDS_ENDPOINT:?run source/02_rds.sh}" "${EXEC_ARN:?}" "${SRC_TASK_SG:?}"
MODULES="${1:-all}"

env_kv(){ printf '{"name":"%s","value":"%s"}' "$1" "$2"; }
ENVJSON="$(paste -sd, <<EOF
$(env_kv TARGET_DB_HOST "$SRC_RDS_ENDPOINT")
$(env_kv TARGET_DB_PORT "5432")
$(env_kv TARGET_DB_NAME "$SOURCE_DB_NAME")
$(env_kv TARGET_DB_USER "$SOURCE_DB_MASTER_USER")
$(env_kv TARGET_DB_PASSWORD "$SOURCE_DB_MASTER_PASSWORD")
$(env_kv ODOO_MASTER_PASSWORD "$ODOO_MASTER_PASSWORD")
EOF
)"

cat > /tmp/td-src-update.json <<JSON
{
  "family": "$PROJECT-src-update",
  "networkMode": "awsvpc",
  "requiresCompatibilities": ["FARGATE"],
  "cpu": "2048", "memory": "8192",
  "executionRoleArn": "$EXEC_ARN",
  "containerDefinitions": [{
    "name": "update",
    "image": "$ECR/$PROJECT/odoo:latest",
    "environment": [$ENVJSON],
    "command": ["-d","$SOURCE_DB_NAME","-u","$MODULES","--stop-after-init","--no-http"],
    "logConfiguration": {"logDriver":"awslogs","options":{
      "awslogs-group":"/ecs/$PROJECT-source","awslogs-region":"$AWS_REGION",
      "awslogs-stream-prefix":"update"}}
  }]
}
JSON
aws ecs register-task-definition --cli-input-json file:///tmp/td-src-update.json --region "$AWS_REGION" >/dev/null
log "running schema update (-u $MODULES) on source db '$SOURCE_DB_NAME' (can take 10-20 min) ..."
CODE="$(run_task_wait_on "$SOURCE_ECS_CLUSTER" "$PROJECT-src-update" "$SRC_TASK_SG")"
[ "$CODE" = "0" ] || { log "source schema update FAILED (exit $CODE); check /ecs/$PROJECT-source"; exit 1; }
log "source schema update complete"
