#!/usr/bin/env bash
# 03: security groups (alb, task) + ingress rules.
#
# RDS-free (Phase B): there is no managed RDS, so the old RDS security group is
# gone. The masker restores into a throwaway in-task postgres on 127.0.0.1; dev
# workspaces run their own local postgres. Only the ALB + Fargate-task SGs are
# needed (for the legacy ECS masker/odoo task path in run_all.sh).
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

auth(){ aws ec2 authorize-security-group-ingress --region "$AWS_REGION" "$@" >/dev/null 2>&1 || true; }
# ALB: 80 from anywhere
auth --group-id "$ALB_SG" --protocol tcp --port 80 --cidr 0.0.0.0/0
# Task: 8069 from ALB
auth --group-id "$TASK_SG" --protocol tcp --port 8069 --source-group "$ALB_SG"

put_state ALB_SG "$ALB_SG"
put_state TASK_SG "$TASK_SG"
log "SGs: alb=$ALB_SG task=$TASK_SG"
