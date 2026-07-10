#!/usr/bin/env bash
# 07: run the masker task (dump+mask SOURCE -> restore MASKED -> neutralize).
# SOURCE lives on the independent source stack (deploy/source/*). Falls back to
# the masked RDS for backward-compat if the source stack isn't present.
source "$(dirname "$0")/lib.sh"
: "${RDS_ENDPOINT:?run 04_rds.sh}" "${EXEC_ARN:?run 05_cluster.sh}"
SRC_HOST="${SRC_RDS_ENDPOINT:-$RDS_ENDPOINT}"
SRC_USER="${SOURCE_DB_MASTER_USER:-$TARGET_DB_USER}"
SRC_PW="${SOURCE_DB_MASTER_PASSWORD:-$TARGET_DB_PASSWORD}"

env_kv(){ printf '{"name":"%s","value":"%s"}' "$1" "$2"; }
ENVJSON="$(paste -sd, <<EOF
$(env_kv SOURCE_DB_HOST "$SRC_HOST")
$(env_kv SOURCE_DB_PORT "5432")
$(env_kv SOURCE_DB_NAME "$SOURCE_DB_NAME")
$(env_kv SOURCE_DB_USER "$SRC_USER")
$(env_kv SOURCE_DB_PASSWORD "$SRC_PW")
$(env_kv TARGET_DB_HOST "$RDS_ENDPOINT")
$(env_kv TARGET_DB_PORT "5432")
$(env_kv TARGET_DB_NAME "$TARGET_DB_NAME")
$(env_kv TARGET_DB_USER "$TARGET_DB_USER")
$(env_kv TARGET_DB_PASSWORD "$TARGET_DB_PASSWORD")
$(env_kv ODOO_ADMIN_PASSWORD "$ODOO_ADMIN_PASSWORD")
EOF
)"

cat > /tmp/td-mask.json <<JSON
{
  "family": "$PROJECT-mask",
  "networkMode": "awsvpc",
  "requiresCompatibilities": ["FARGATE"],
  "cpu": "$TASK_CPU", "memory": "$TASK_MEM",
  "executionRoleArn": "$EXEC_ARN",
  "containerDefinitions": [{
    "name": "masker",
    "image": "$ECR/$PROJECT/masker:latest",
    "environment": [$ENVJSON],
    "logConfiguration": {"logDriver":"awslogs","options":{
      "awslogs-group":"/ecs/$PROJECT","awslogs-region":"$AWS_REGION",
      "awslogs-stream-prefix":"mask"}}
  }]
}
JSON
aws ecs register-task-definition --cli-input-json file:///tmp/td-mask.json --region "$AWS_REGION" >/dev/null
log "running masker ..."
CODE="$(run_task_wait "$PROJECT-mask")"
[ "$CODE" = "0" ] || { log "mask FAILED (exit $CODE); check logs /ecs/$PROJECT"; exit 1; }
log "masking complete -> masked db '$TARGET_DB_NAME'"
