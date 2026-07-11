#!/usr/bin/env bash
# one-off psql runner. Targets the MASKED RDS by default, or the SOURCE RDS when
# the 3rd arg is 'source'. Usage: bash deploy/psql.sh "SQL" [db] [source|masked]
source "$(dirname "$0")/lib.sh"
: "${EXEC_ARN:?}"
SQL="${1:?need SQL}"; TARGET="${3:-masked}"

if [ "$TARGET" = "source" ]; then
  : "${SRC_RDS_ENDPOINT:?run source/02_rds.sh}"
  HOST="$SRC_RDS_ENDPOINT"; USER="$SOURCE_DB_MASTER_USER"; PW="$SOURCE_DB_MASTER_PASSWORD"; SG="$SRC_TASK_SG"; CL="$SOURCE_ECS_CLUSTER"; LG="/ecs/$PROJECT-source"
else
  : "${RDS_ENDPOINT:?}"
  HOST="$RDS_ENDPOINT"; USER="$TARGET_DB_USER"; PW="$TARGET_DB_PASSWORD"; SG="$TASK_SG"; CL="$ECS_CLUSTER"; LG="/ecs/$PROJECT"
fi
DB="${2:-$TARGET_DB_NAME}"
B64="$(printf '%s' "$SQL" | base64 -w0)"

python3 - "$PROJECT" "$EXEC_ARN" "$ECR" "$HOST" "$USER" "$PW" "$DB" "$AWS_REGION" "$B64" "$LG" > /tmp/td-psql.json <<'PY'
import json,sys
proj,exec_arn,ecr,host,user,pw,db,region,b64,lg=sys.argv[1:11]
cmd=f'export PGPASSWORD={pw}; echo {b64} | base64 -d > /tmp/q.sql; psql -h {host} -U {user} -d {db} -f /tmp/q.sql'
td={"family":f"{proj}-psql","networkMode":"awsvpc","requiresCompatibilities":["FARGATE"],
"cpu":"512","memory":"1024","executionRoleArn":exec_arn,
"containerDefinitions":[{"name":"psql","image":f"{ecr}/{proj}/masker:latest",
"entryPoint":["bash","-lc"],"command":[cmd],
"logConfiguration":{"logDriver":"awslogs","options":{"awslogs-group":lg,"awslogs-region":region,"awslogs-stream-prefix":"psql"}}}]}
print(json.dumps(td))
PY
aws ecs register-task-definition --cli-input-json file:///tmp/td-psql.json --region "$AWS_REGION" >/dev/null
CODE="$(run_task_wait_on "$CL" "$PROJECT-psql" "$SG")"
log "psql exit $CODE"
sleep 8
st=$(aws logs describe-log-streams --log-group-name "$LG" --region "$AWS_REGION" --order-by LastEventTime --descending --query 'logStreams[?starts_with(logStreamName,`psql/`)].logStreamName' --output text | tr '\t' '\n' | head -1)
aws logs get-log-events --log-group-name "$LG" --log-stream-name "$st" --region "$AWS_REGION" --limit 60 --query 'events[].message' --output text
