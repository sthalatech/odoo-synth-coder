#!/usr/bin/env bash
# SOURCE stack 01: dedicated SGs (alb + task + rds) for the independent source.
source "$(dirname "$0")/../lib.sh"
VPC="$(vpc_id)"

ensure_sg(){ local id; id="$(sg_id "$1")"
  if [ -z "$id" ] || [ "$id" = "None" ]; then
    id="$(aws ec2 create-security-group --group-name "$1" --description "$2" \
      --vpc-id "$VPC" --region "$AWS_REGION" --query GroupId --output text)"
  fi; echo "$id"; }

SRC_ALB_SG="$(ensure_sg "$PROJECT-src-alb-sg" "source ALB")"
SRC_TASK_SG="$(ensure_sg "$PROJECT-src-task-sg" "source Fargate tasks")"
SRC_RDS_SG="$(ensure_sg "$PROJECT-src-rds-sg" "source RDS")"

auth(){ aws ec2 authorize-security-group-ingress --region "$AWS_REGION" "$@" >/dev/null 2>&1 || true; }
auth --group-id "$SRC_ALB_SG"  --protocol tcp --port 80   --cidr 0.0.0.0/0
auth --group-id "$SRC_TASK_SG" --protocol tcp --port 8069 --source-group "$SRC_ALB_SG"
auth --group-id "$SRC_RDS_SG"  --protocol tcp --port 5432 --source-group "$SRC_TASK_SG"
# The masker (runs on the masked TASK_SG) must also reach the source RDS to dump it.
[ -n "${TASK_SG:-}" ] && [ "${TASK_SG}" != "None" ] && \
  auth --group-id "$SRC_RDS_SG" --protocol tcp --port 5432 --source-group "$TASK_SG"

put_state SRC_ALB_SG "$SRC_ALB_SG"
put_state SRC_TASK_SG "$SRC_TASK_SG"
put_state SRC_RDS_SG "$SRC_RDS_SG"
log "source SGs: alb=$SRC_ALB_SG task=$SRC_TASK_SG rds=$SRC_RDS_SG"
