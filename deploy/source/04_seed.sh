#!/usr/bin/env bash
# SOURCE stack 04: seed the source DB on the source RDS with Odoo + demo data.
source "$(dirname "$0")/../lib.sh"
: "${SRC_RDS_ENDPOINT:?run source/02_rds.sh}" "${EXEC_ARN:?}" "${SRC_TASK_SG:?}"

cat > /tmp/td-src-seed.json <<JSON
{
  "family": "$PROJECT-src-seed",
  "networkMode": "awsvpc",
  "requiresCompatibilities": ["FARGATE"],
  "cpu": "$TASK_CPU", "memory": "$TASK_MEM",
  "executionRoleArn": "$EXEC_ARN",
  "containerDefinitions": [{
    "name": "seed",
    "image": "$ECR/$PROJECT/odoo:latest",
    "entryPoint": ["python3","/opt/odoo-src/odoo-bin"],
    "command": ["-d","$SOURCE_DB_NAME","-i","base","--without-demo=False",
      "--stop-after-init",
      "--addons-path","/opt/odoo-src/odoo/addons,/opt/odoo-src/addons",
      "--db_host","$SRC_RDS_ENDPOINT","--db_port","5432",
      "--db_user","$SOURCE_DB_MASTER_USER","--db_password","$SOURCE_DB_MASTER_PASSWORD"],
    "logConfiguration": {"logDriver":"awslogs","options":{
      "awslogs-group":"/ecs/$PROJECT-source","awslogs-region":"$AWS_REGION",
      "awslogs-stream-prefix":"seed"}}
  }]
}
JSON
aws ecs register-task-definition --cli-input-json file:///tmp/td-src-seed.json --region "$AWS_REGION" >/dev/null
log "seeding source db '$SOURCE_DB_NAME' on source RDS (a few min) ..."
CODE="$(run_task_wait_on "$SOURCE_ECS_CLUSTER" "$PROJECT-src-seed" "$SRC_TASK_SG")"
[ "$CODE" = "0" ] || { log "source seed FAILED (exit $CODE); check /ecs/$PROJECT-source"; exit 1; }
log "source seed complete"
