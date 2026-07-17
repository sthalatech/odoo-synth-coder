#!/usr/bin/env bash
# Shared helpers for deploy scripts. `source` this.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# Config: config.yaml is the single source of truth (see config.example.yaml).
# deploy/_yaml_to_env.py loads it and exports the KEY=VALUE env vars the
# pipeline scripts expect (with `ref:` secret resolution for env/SSM). The
# python3 dependency is installed by deploy/00_install_prereqs.sh.
_load_config() {
  if [ ! -f "$HERE/config.yaml" ]; then
    echo "== ERROR: $HERE/config.yaml not found ==" >&2
    echo "       copy config.example.yaml to config.yaml and fill it in, then" >&2
    echo "       run bash deploy/00_validate_config.sh." >&2
    return 1
  fi
  if ! command -v python3 >/dev/null 2>&1; then
    echo "== ERROR: python3 not found on PATH (needed to load config.yaml) ==" >&2
    echo "       run bash deploy/00_install_prereqs.sh first." >&2
    return 1
  fi
  eval "$(python3 "$HERE/deploy/_yaml_to_env.py" "$HERE/config.yaml")"
  return $?
}
_load_config

ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"
ECR="${ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com"
export HERE ACCOUNT_ID ECR
export AWS_PAGER=""

log(){ echo "== $* ==" >&2; }

STATE="$HERE/deploy/state.env"
touch "$STATE"
set -a; source "$STATE"; set +a
put_state(){ # key value
  # ensure the file ends with a newline so appends never glue onto the last line
  [ -s "$STATE" ] && [ -n "$(tail -c1 "$STATE")" ] && echo >> "$STATE"
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
  run_task_wait_on "$ECS_CLUSTER" "$fam" "$sg"
}

# generalized: run a one-shot task on a specific cluster. args: cluster family sg
run_task_wait_on(){ local cl="$1" fam="$2" sg="$3"
  local arn; arn="$(aws ecs run-task --cluster "$cl" --launch-type FARGATE \
    --task-definition "$fam" --network-configuration "$(net_config "$sg")" \
    --region "$AWS_REGION" --query 'tasks[0].taskArn' --output text)"
  log "task $fam started on $cl: ${arn##*/}"
  aws ecs wait tasks-stopped --cluster "$cl" --tasks "$arn" --region "$AWS_REGION"
  # Prefer the masker container's exit code (the mask task runs a postgres
  # sidecar + masker; the sidecar's exitCode is meaningless). Fall back to the
  # last container that has one, then containers[0].
  local code; code="$(aws ecs describe-tasks --cluster "$cl" --tasks "$arn" \
    --region "$AWS_REGION" --query 'tasks[0].containers[?name==`masker`].exitCode | [0]' --output text 2>/dev/null || true)"
  [ "$code" != "None" ] && [ -n "$code" ] ||     code="$(aws ecs describe-tasks --cluster "$cl" --tasks "$arn" \
      --region "$AWS_REGION" --query 'tasks[0].containers[-1].exitCode' --output text)"
  log "task $fam exit code: $code"
  echo "$code"
}
