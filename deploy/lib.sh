#!/usr/bin/env bash
# Shared helpers for deploy scripts. `source` this.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
set -a; source "$HERE/config.env"; set +a

ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"
ECR="${ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com"
export HERE ACCOUNT_ID ECR
export AWS_PAGER=""

log(){ echo "== $* ==" >&2; }

STATE="$HERE/deploy/state.env"
touch "$STATE"
set -a; source "$STATE"; set +a
put_state(){ # key value
  grep -v "^$1=" "$STATE" > "$STATE.tmp" 2>/dev/null || true
  echo "$1=$2" >> "$STATE.tmp"; mv "$STATE.tmp" "$STATE"
  export "$1=$2"
}

vpc_id(){ aws ec2 describe-vpcs --filters Name=isDefault,Values=true \
    --query 'Vpcs[0].VpcId' --output text --region "$AWS_REGION"; }

subnet_ids(){ aws ec2 describe-subnets --filters Name=vpc-id,Values=$(vpc_id) \
    Name=default-for-az,Values=true --query 'Subnets[].SubnetId' \
    --output text --region "$AWS_REGION"; }

sg_id(){ aws ec2 describe-security-groups \
    --filters Name=group-name,Values="$1" Name=vpc-id,Values=$(vpc_id) \
    --query 'SecurityGroups[0].GroupId' --output text --region "$AWS_REGION" 2>/dev/null; }

# net config for RunTask/CreateService (public subnets, assign public IP so
# Fargate can pull from ECR without a NAT gateway).
net_config(){ local sg="$1"; local subs; subs="$(subnet_ids | tr '[:space:]' ',' | sed 's/,$//')"
  echo "awsvpcConfiguration={subnets=[$subs],securityGroups=[$sg],assignPublicIp=ENABLED}"; }

# run a one-shot task and wait; echo its exit code. args: family [sg]
run_task_wait(){ local fam="$1" sg="${2:-$TASK_SG}"
  local arn; arn="$(aws ecs run-task --cluster "$ECS_CLUSTER" --launch-type FARGATE \
    --task-definition "$fam" --network-configuration "$(net_config "$sg")" \
    --region "$AWS_REGION" --query 'tasks[0].taskArn' --output text)"
  log "task $fam started: ${arn##*/}"
  aws ecs wait tasks-stopped --cluster "$ECS_CLUSTER" --tasks "$arn" --region "$AWS_REGION"
  local code; code="$(aws ecs describe-tasks --cluster "$ECS_CLUSTER" --tasks "$arn" \
    --region "$AWS_REGION" --query 'tasks[0].containers[0].exitCode' --output text)"
  log "task $fam exit code: $code"
  echo "$code"
}
