#!/usr/bin/env bash
# 07: run the masker task (dump+mask SOURCE -> restore MASKED -> neutralize).
#
# RDS-free (Phase B): the masked DB no longer lives on managed RDS. The masker
# task now runs a postgres:16 sidecar (`target-db`) in the same awsvpc task so
# the masker restores into a THROWAWAY local postgres on 127.0.0.1. The
# persistent artifact is the masked pg_dump uploaded to S3
# (MASKED_DUMP_PUT_URL, driven by env); the transient in-task DB is discarded
# when the task stops.
#
# SOURCE: a real source DB must be configured. The primary profile flow points
# the masker at the user's prod DSN directly (handled by the control panel);
# this operator-run script reads SOURCE_DB_HOST from the env (set by the
# caller). For backward compat, if the legacy source-stack endpoint
# (SRC_RDS_ENDPOINT) or masked RDS endpoint (RDS_ENDPOINT) is present in state,
# it is used (legacy compat). Otherwise the caller must export SOURCE_DB_HOST / creds.
source "$(dirname "$0")/lib.sh"
: "${EXEC_ARN:?run 05_cluster.sh}"

# Resolve a source host: explicit SOURCE_DB_HOST wins, then the source stack,
# then the legacy masked RDS endpoint (backward compat).
SRC_HOST="${SOURCE_DB_HOST:-${SRC_RDS_ENDPOINT:-${RDS_ENDPOINT:-}}}"
[ -n "$SRC_HOST" ] || {
  log "ERROR: source DB not configured. Export" >&2
  log "       SOURCE_DB_HOST (+ SOURCE_DB_USER/PASSWORD/NAME) pointing at your prod DSN." >&2
  exit 1
}
SRC_USER="${SOURCE_DB_MASTER_USER:-$TARGET_DB_USER}"
SRC_PW="${SOURCE_DB_MASTER_PASSWORD:-$TARGET_DB_PASSWORD}"
SRC_NAME="${SOURCE_DB_NAME:-source}"

# Two containers in one awsvpc task share localhost, so the masker reaches the
# throwaway target postgres on 127.0.0.1. Bump task CPU/mem to fit both.
CPU=$((TASK_CPU*2)); MEM=$((TASK_MEM*2))

env_kv(){ printf '{"name":"%s","value":"%s"}' "$1" "$2"; }
MASK_ENV="$(paste -sd, <<EOF
$(env_kv SOURCE_DB_HOST "$SRC_HOST")
$(env_kv SOURCE_DB_PORT "5432")
$(env_kv SOURCE_DB_NAME "$SRC_NAME")
$(env_kv SOURCE_DB_USER "$SRC_USER")
$(env_kv SOURCE_DB_PASSWORD "$SRC_PW")
$(env_kv TARGET_DB_HOST "127.0.0.1")
$(env_kv TARGET_DB_PORT "5432")
$(env_kv TARGET_DB_NAME "$TARGET_DB_NAME")
$(env_kv TARGET_DB_USER "$TARGET_DB_USER")
$(env_kv TARGET_DB_PASSWORD "$TARGET_DB_PASSWORD")
$(env_kv ODOO_ADMIN_PASSWORD "$ODOO_ADMIN_PASSWORD")
EOF
)"

PG_ENV="$(paste -sd, <<EOF
$(env_kv POSTGRES_PASSWORD "$TARGET_DB_PASSWORD")
$(env_kv POSTGRES_USER "$TARGET_DB_USER")
$(env_kv POSTGRES_DB "postgres")
EOF
)"

cat > /tmp/td-mask.json <<JSON
{
  "family": "$PROJECT-mask",
  "networkMode": "awsvpc",
  "requiresCompatibilities": ["FARGATE"],
  "cpu": "$CPU", "memory": "$MEM",
  "executionRoleArn": "$EXEC_ARN",
  "containerDefinitions": [
    {
      "name": "target-db",
      "image": "postgres:16",
      "environment": [$PG_ENV],
      "healthCheck": {
        "command": ["CMD-SHELL", "pg_isready -U $TARGET_DB_USER"],
        "interval": 5, "timeout": 3, "retries": 10, "startPeriod": 10
      },
      "logConfiguration": {"logDriver":"awslogs","options":{
        "awslogs-group":"/ecs/$PROJECT","awslogs-region":"$AWS_REGION",
        "awslogs-stream-prefix":"mask-pg"}}
    },
    {
      "name": "masker",
      "image": "$ECR/$PROJECT/masker:latest",
      "environment": [$MASK_ENV],
      "dependsOn": [{"containerName":"target-db","condition":"HEALTHY"}],
      "logConfiguration": {"logDriver":"awslogs","options":{
        "awslogs-group":"/ecs/$PROJECT","awslogs-region":"$AWS_REGION",
        "awslogs-stream-prefix":"mask"}}
    }
  ]
}
JSON
aws ecs register-task-definition --cli-input-json file:///tmp/td-mask.json --region "$AWS_REGION" >/dev/null
log "running masker (postgres sidecar + masker) ..."
CODE="$(run_task_wait "$PROJECT-mask")"
[ "$CODE" = "0" ] || { log "mask FAILED (exit $CODE); check logs /ecs/$PROJECT"; exit 1; }
log "masking complete -> masked db '$TARGET_DB_NAME' (transient; dump in S3 if MASKED_DUMP_PUT_URL set)"
