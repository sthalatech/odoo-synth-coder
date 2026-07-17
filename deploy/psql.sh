#!/usr/bin/env bash
# one-off psql runner. Runs `psql` inside a Fargate task (masker image has the
# client) against a Postgres host you specify via env vars. RDS-free (Phase B):
# there is no longer a managed masked RDS or a source-stack RDS to target by
# default, so the host/creds MUST be supplied in the env.
#
# Usage: bash deploy/psql.sh "SQL" [db]
# Required env: PSQL_HOST, PSQL_USER, PSQL_PASSWORD. Defaults for
# user/password/dbname fall back to the shared TARGET_DB_* config vars.
source "$(dirname "$0")/lib.sh"
: "${EXEC_ARN:?}"
SQL="${1:?need SQL}"

: "${PSQL_HOST:?ERROR: export PSQL_HOST (no managed RDS to default to)}"
HOST="$PSQL_HOST"
USER="${PSQL_USER:-$TARGET_DB_USER}"
PW="${PSQL_PASSWORD:-$TARGET_DB_PASSWORD}"
DB="${2:-${PSQL_DB:-$TARGET_DB_NAME}}"
SG="${TASK_SG:?run 03_network.sh}"
CL="${ECS_CLUSTER}"
LG="/ecs/$PROJECT"
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
