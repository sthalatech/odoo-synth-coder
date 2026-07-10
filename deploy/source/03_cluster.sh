#!/usr/bin/env bash
# SOURCE stack 03: dedicated ECS cluster + log group (reuses shared exec role).
source "$(dirname "$0")/../lib.sh"
: "${EXEC_ARN:?run 05_cluster.sh (shared exec role)}"

for attempt in 1 2 3 4 5 6; do
  ST="$(aws ecs describe-clusters --clusters "$SOURCE_ECS_CLUSTER" --region "$AWS_REGION" \
    --query 'clusters[0].status' --output text 2>/dev/null || true)"
  [ "$ST" = "ACTIVE" ] && break
  aws ecs create-cluster --cluster-name "$SOURCE_ECS_CLUSTER" \
    --capacity-providers FARGATE --region "$AWS_REGION" >/dev/null 2>&1 || true
  sleep 5
done
[ "$ST" = "ACTIVE" ] || { log "source cluster not ACTIVE (got $ST)"; exit 1; }

aws logs create-log-group --log-group-name "/ecs/$PROJECT-source" --region "$AWS_REGION" 2>/dev/null || true
log "source cluster: $SOURCE_ECS_CLUSTER ($ST)"
