#!/usr/bin/env bash
# Tear down ALL odoo-synth AWS resources. Safe to re-run (idempotent).
source "$(dirname "$0")/lib.sh"
R="$AWS_REGION"

log "deleting ECS service ..."
aws ecs update-service --cluster "$ECS_CLUSTER" --service "$PROJECT-odoo" \
  --desired-count 0 --region "$R" >/dev/null 2>&1 || true
aws ecs delete-service --cluster "$ECS_CLUSTER" --service "$PROJECT-odoo" \
  --force --region "$R" >/dev/null 2>&1 || true

log "stopping stray tasks ..."
for t in $(aws ecs list-tasks --cluster "$ECS_CLUSTER" --region "$R" --query 'taskArns' --output text 2>/dev/null); do
  aws ecs stop-task --cluster "$ECS_CLUSTER" --task "$t" --region "$R" >/dev/null 2>&1 || true
done

log "deleting ALB + target group ..."
ALB_ARN="$(aws elbv2 describe-load-balancers --names "$PROJECT-alb" --region "$R" \
  --query 'LoadBalancers[0].LoadBalancerArn' --output text 2>/dev/null || true)"
if [ -n "$ALB_ARN" ] && [ "$ALB_ARN" != "None" ]; then
  for L in $(aws elbv2 describe-listeners --load-balancer-arn "$ALB_ARN" --region "$R" \
      --query 'Listeners[].ListenerArn' --output text 2>/dev/null); do
    aws elbv2 delete-listener --listener-arn "$L" --region "$R" >/dev/null 2>&1 || true
  done
  aws elbv2 delete-load-balancer --load-balancer-arn "$ALB_ARN" --region "$R" >/dev/null 2>&1 || true
  log "waiting for ALB deletion ..."
  aws elbv2 wait load-balancers-deleted --load-balancer-arns "$ALB_ARN" --region "$R" 2>/dev/null || true
fi
TG_ARN="$(aws elbv2 describe-target-groups --names "$PROJECT-tg" --region "$R" \
  --query 'TargetGroups[0].TargetGroupArn' --output text 2>/dev/null || true)"
[ -n "$TG_ARN" ] && [ "$TG_ARN" != "None" ] && \
  aws elbv2 delete-target-group --target-group-arn "$TG_ARN" --region "$R" >/dev/null 2>&1 || true

log "deleting RDS instance ..."
if aws rds describe-db-instances --db-instance-identifier "$RDS_INSTANCE_ID" --region "$R" >/dev/null 2>&1; then
  aws rds delete-db-instance --db-instance-identifier "$RDS_INSTANCE_ID" \
    --skip-final-snapshot --delete-automated-backups --region "$R" >/dev/null 2>&1 || true
  log "waiting for RDS deletion ..."
  aws rds wait db-instance-deleted --db-instance-identifier "$RDS_INSTANCE_ID" --region "$R" 2>/dev/null || true
fi
aws rds delete-db-subnet-group --db-subnet-group-name "$PROJECT-subnets" --region "$R" >/dev/null 2>&1 || true

log "deregistering task definitions ..."
for fam in seed mask odoo psql verify users; do
  for arn in $(aws ecs list-task-definitions --family-prefix "$PROJECT-$fam" --region "$R" \
      --query 'taskDefinitionArns' --output text 2>/dev/null); do
    aws ecs deregister-task-definition --task-definition "$arn" --region "$R" >/dev/null 2>&1 || true
  done
done

log "deleting ECS cluster ..."
aws ecs delete-cluster --cluster "$ECS_CLUSTER" --region "$R" >/dev/null 2>&1 || true

log "deleting security groups ..."
# RDS/task/alb order matters (dependencies); retry a couple times.
for pass in 1 2 3; do
  for name in "$PROJECT-rds-sg" "$PROJECT-task-sg" "$PROJECT-alb-sg"; do
    id="$(sg_id "$name")"
    [ -n "$id" ] && [ "$id" != "None" ] && \
      aws ec2 delete-security-group --group-id "$id" --region "$R" >/dev/null 2>&1 || true
  done
  sleep 3
done

log "deleting CloudWatch log group ..."
aws logs delete-log-group --log-group-name "/ecs/$PROJECT" --region "$R" >/dev/null 2>&1 || true

# Keep ECR repos + images (re-push is cheap; delete if --ecr passed).
if [ "${1:-}" = "--ecr" ]; then
  log "deleting ECR repos ..."
  for name in masker odoo; do
    aws ecr delete-repository --repository-name "$PROJECT/$name" --force --region "$R" >/dev/null 2>&1 || true
  done
fi

# Reset state (keep nothing infra-specific).
: > "$STATE"
log "TEARDOWN COMPLETE"
