#!/usr/bin/env bash
# 06: seed the SOURCE db on RDS with a fresh Odoo install + demo data.
source "$(dirname "$0")/lib.sh"
: "${RDS_ENDPOINT:?run 04_rds.sh}" "${EXEC_ARN:?run 05_cluster.sh}"

cat > /tmp/td-seed.json <<JSON
{
  "family": "$PROJECT-seed",
  "networkMode": "awsvpc",
  "requiresCompatibilities": ["FARGATE"],
  "cpu": "$TASK_CPU", "memory": "$TASK_MEM",
  "executionRoleArn": "$EXEC_ARN",
  "containerDefinitions": [{
    "name": "seed",
    "image": "$ECR/$PROJECT/odoo:latest",
    "entryPoint": ["odoo"],
    "command": ["-d","$SOURCE_DB_NAME","-i","base","--without-demo=False",
      "--stop-after-init","--db_host","$RDS_ENDPOINT","--db_port","5432",
      "--db_user","$TARGET_DB_USER","--db_password","$TARGET_DB_PASSWORD"],
    "logConfiguration": {"logDriver":"awslogs","options":{
      "awslogs-group":"/ecs/$PROJECT","awslogs-region":"$AWS_REGION",
      "awslogs-stream-prefix":"seed"}}
  }]
}
JSON
aws ecs register-task-definition --cli-input-json file:///tmp/td-seed.json --region "$AWS_REGION" >/dev/null
log "seeding source db '$SOURCE_DB_NAME' with demo data (this takes a few min) ..."
CODE="$(run_task_wait "$PROJECT-seed")"
[ "$CODE" = "0" ] || { log "seed FAILED (exit $CODE); check logs /ecs/$PROJECT"; exit 1; }
log "seed complete"
