#!/usr/bin/env bash
# 03: security groups (alb, task, rds) + ingress rules.
source "$(dirname "$0")/lib.sh"
VPC="$(vpc_id)"

ensure_sg(){ # name desc
  local id; id="$(sg_id "$1")"
  if [ -z "$id" ] || [ "$id" = "None" ]; then
    id="$(aws ec2 create-security-group --group-name "$1" --description "$2" \
      --vpc-id "$VPC" --region "$AWS_REGION" --query GroupId --output text)"
  fi
  echo "$id"
}

ALB_SG="$(ensure_sg "$PROJECT-alb-sg" "odoo-synth ALB")"
TASK_SG="$(ensure_sg "$PROJECT-task-sg" "odoo-synth Fargate tasks")"
RDS_SG="$(ensure_sg "$PROJECT-rds-sg" "odoo-synth RDS")"

auth(){ aws ec2 authorize-security-group-ingress --region "$AWS_REGION" "$@" >/dev/null 2>&1 || true; }
# ALB: 80 from anywhere
auth --group-id "$ALB_SG" --protocol tcp --port 80 --cidr 0.0.0.0/0
# Task: 8069 from ALB
auth --group-id "$TASK_SG" --protocol tcp --port 8069 --source-group "$ALB_SG"
# RDS: 5432 from tasks
auth --group-id "$RDS_SG" --protocol tcp --port 5432 --source-group "$TASK_SG"

put_state ALB_SG "$ALB_SG"
put_state TASK_SG "$TASK_SG"
put_state RDS_SG "$RDS_SG"
log "SGs: alb=$ALB_SG task=$TASK_SG rds=$RDS_SG"
