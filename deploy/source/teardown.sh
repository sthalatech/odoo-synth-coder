#!/usr/bin/env bash
# Tear down ONLY the independent SOURCE stack. Idempotent.
source "$(dirname "$0")/../lib.sh"
R="$AWS_REGION"

log "deleting source ECS service ..."
aws ecs update-service --cluster "$SOURCE_ECS_CLUSTER" --service "$PROJECT-src-odoo" \
  --desired-count 0 --region "$R" >/dev/null 2>&1 || true
aws ecs delete-service --cluster "$SOURCE_ECS_CLUSTER" --service "$PROJECT-src-odoo" \
  --force --region "$R" >/dev/null 2>&1 || true
for t in $(aws ecs list-tasks --cluster "$SOURCE_ECS_CLUSTER" --region "$R" --query 'taskArns' --output text 2>/dev/null); do
  aws ecs stop-task --cluster "$SOURCE_ECS_CLUSTER" --task "$t" --region "$R" >/dev/null 2>&1 || true
done

log "deleting source ALB + target group ..."
ALB_ARN="$(aws elbv2 describe-load-balancers --names "$PROJECT-src-alb" --region "$R" \
  --query 'LoadBalancers[0].LoadBalancerArn' --output text 2>/dev/null || true)"
if [ -n "$ALB_ARN" ] && [ "$ALB_ARN" != "None" ]; then
  for L in $(aws elbv2 describe-listeners --load-balancer-arn "$ALB_ARN" --region "$R" \
      --query 'Listeners[].ListenerArn' --output text 2>/dev/null); do
    aws elbv2 delete-listener --listener-arn "$L" --region "$R" >/dev/null 2>&1 || true
  done
  aws elbv2 delete-load-balancer --load-balancer-arn "$ALB_ARN" --region "$R" >/dev/null 2>&1 || true
  aws elbv2 wait load-balancers-deleted --load-balancer-arns "$ALB_ARN" --region "$R" 2>/dev/null || true
fi
TG_ARN="$(aws elbv2 describe-target-groups --names "$PROJECT-src-tg" --region "$R" \
  --query 'TargetGroups[0].TargetGroupArn' --output text 2>/dev/null || true)"
[ -n "$TG_ARN" ] && [ "$TG_ARN" != "None" ] && \
  aws elbv2 delete-target-group --target-group-arn "$TG_ARN" --region "$R" >/dev/null 2>&1 || true

log "deleting source RDS ..."
if aws rds describe-db-instances --db-instance-identifier "$SOURCE_RDS_INSTANCE_ID" --region "$R" >/dev/null 2>&1; then
  aws rds delete-db-instance --db-instance-identifier "$SOURCE_RDS_INSTANCE_ID" \
    --skip-final-snapshot --delete-automated-backups --region "$R" >/dev/null 2>&1 || true
  aws rds wait db-instance-deleted --db-instance-identifier "$SOURCE_RDS_INSTANCE_ID" --region "$R" 2>/dev/null || true
fi
aws rds delete-db-subnet-group --db-subnet-group-name "$PROJECT-src-subnets" --region "$R" >/dev/null 2>&1 || true

log "deregistering source task definitions ..."
for fam in src-seed src-odoo; do
  for arn in $(aws ecs list-task-definitions --family-prefix "$PROJECT-$fam" --region "$R" \
      --query 'taskDefinitionArns' --output text 2>/dev/null); do
    aws ecs deregister-task-definition --task-definition "$arn" --region "$R" >/dev/null 2>&1 || true
  done
done

log "deleting source ECS cluster ..."
aws ecs delete-cluster --cluster "$SOURCE_ECS_CLUSTER" --region "$R" >/dev/null 2>&1 || true

log "deleting source security groups ..."
for pass in 1 2 3; do
  for name in "$PROJECT-src-rds-sg" "$PROJECT-src-task-sg" "$PROJECT-src-alb-sg"; do
    id="$(sg_id "$name")"
    [ -n "$id" ] && [ "$id" != "None" ] && \
      aws ec2 delete-security-group --group-id "$id" --region "$R" >/dev/null 2>&1 || true
  done
  sleep 3
done

aws logs delete-log-group --log-group-name "/ecs/$PROJECT-source" --region "$R" >/dev/null 2>&1 || true

for k in SRC_ALB_SG SRC_TASK_SG SRC_RDS_SG SRC_RDS_ENDPOINT SRC_ALB_DNS SRC_ALB_URL; do
  grep -v "^$k=" "$STATE" > "$STATE.tmp" 2>/dev/null || true; mv "$STATE.tmp" "$STATE"
done
log "SOURCE TEARDOWN COMPLETE"
