#!/usr/bin/env bash
# one-off psql runner against the masked DB. Usage: bash deploy/psql.sh "SQL" [db]
source "$(dirname "$0")/lib.sh"
: "${RDS_ENDPOINT:?}" "${EXEC_ARN:?}"
SQL="${1:?need SQL}"; DB="${2:-$TARGET_DB_NAME}"
B64="$(printf '%s' "$SQL" | base64 -w0)"

python3 - "$PROJECT" "$EXEC_ARN" "$ECR" "$RDS_ENDPOINT" "$TARGET_DB_USER" "$TARGET_DB_PASSWORD" "$DB" "$AWS_REGION" "$B64" > /tmp/td-psql.json <<'PY'
import json,sys
proj,exec_arn,ecr,host,user,pw,db,region,b64=sys.argv[1:10]
cmd=f'export PGPASSWORD={pw}; echo {b64} | base64 -d > /tmp/q.sql; psql -h {host} -U {user} -d {db} -f /tmp/q.sql'
td={"family":f"{proj}-psql","networkMode":"awsvpc","requiresCompatibilities":["FARGATE"],
"cpu":"512","memory":"1024","executionRoleArn":exec_arn,
"containerDefinitions":[{"name":"psql","image":f"{ecr}/{proj}/masker:latest",
"entryPoint":["bash","-lc"],"command":[cmd],
"logConfiguration":{"logDriver":"awslogs","options":{"awslogs-group":f"/ecs/{proj}","awslogs-region":region,"awslogs-stream-prefix":"psql"}}}]}
print(json.dumps(td))
PY
aws ecs register-task-definition --cli-input-json file:///tmp/td-psql.json --region "$AWS_REGION" >/dev/null
CODE="$(run_task_wait "$PROJECT-psql")"
log "psql exit $CODE"
sleep 8
st=$(aws logs describe-log-streams --log-group-name "/ecs/$PROJECT" --region "$AWS_REGION" --order-by LastEventTime --descending --query 'logStreams[?starts_with(logStreamName,`psql/`)].logStreamName' --output text | tr '\t' '\n' | head -1)
aws logs get-log-events --log-group-name "/ecs/$PROJECT" --log-stream-name "$st" --region "$AWS_REGION" --limit 60 --query 'events[].message' --output text
