#!/usr/bin/env bash
# 05: ECS cluster + task execution role + log group.
source "$(dirname "$0")/lib.sh"

aws ecs describe-clusters --clusters "$ECS_CLUSTER" --region "$AWS_REGION" \
  --query 'clusters[0].status' --output text 2>/dev/null | grep -q ACTIVE \
  || aws ecs create-cluster --cluster-name "$ECS_CLUSTER" \
       --capacity-providers FARGATE --region "$AWS_REGION" >/dev/null
log "cluster: $ECS_CLUSTER"

ROLE="$PROJECT-exec"
if ! aws iam get-role --role-name "$ROLE" >/dev/null 2>&1; then
  aws iam create-role --role-name "$ROLE" \
    --assume-role-policy-document '{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Service":"ecs-tasks.amazonaws.com"},"Action":"sts:AssumeRole"}]}' >/dev/null
  aws iam attach-role-policy --role-name "$ROLE" \
    --policy-arn arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy >/dev/null
fi
EXEC_ARN="$(aws iam get-role --role-name "$ROLE" --query 'Role.Arn' --output text)"
put_state EXEC_ARN "$EXEC_ARN"

aws logs create-log-group --log-group-name "/ecs/$PROJECT" --region "$AWS_REGION" 2>/dev/null || true
log "exec role: $EXEC_ARN"
