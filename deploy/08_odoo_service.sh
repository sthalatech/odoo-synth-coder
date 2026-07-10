#!/usr/bin/env bash
# 08: ALB + Odoo service pointed at the masked replica DB.
source "$(dirname "$0")/lib.sh"
: "${RDS_ENDPOINT:?}" "${EXEC_ARN:?}" "${ALB_SG:?}" "${TASK_SG:?}"
VPC="$(vpc_id)"; SUBNETS="$(subnet_ids)"

# ALB
ALB_ARN="$(aws elbv2 describe-load-balancers --names "$PROJECT-alb" --region "$AWS_REGION" \
  --query 'LoadBalancers[0].LoadBalancerArn' --output text 2>/dev/null || true)"
if [ -z "$ALB_ARN" ] || [ "$ALB_ARN" = "None" ]; then
  ALB_ARN="$(aws elbv2 create-load-balancer --name "$PROJECT-alb" --type application \
    --subnets $SUBNETS --security-groups "$ALB_SG" --region "$AWS_REGION" \
    --query 'LoadBalancers[0].LoadBalancerArn' --output text)"
fi
# Target group (ip target, 8069)
TG_ARN="$(aws elbv2 describe-target-groups --names "$PROJECT-tg" --region "$AWS_REGION" \
  --query 'TargetGroups[0].TargetGroupArn' --output text 2>/dev/null || true)"
if [ -z "$TG_ARN" ] || [ "$TG_ARN" = "None" ]; then
  TG_ARN="$(aws elbv2 create-target-group --name "$PROJECT-tg" --protocol HTTP --port 8069 \
    --vpc-id "$VPC" --target-type ip --health-check-path /web/login \
    --health-check-interval-seconds 30 --healthy-threshold-count 2 \
    --region "$AWS_REGION" --query 'TargetGroups[0].TargetGroupArn' --output text)"
fi
# Listener 80 -> TG
aws elbv2 describe-listeners --load-balancer-arn "$ALB_ARN" --region "$AWS_REGION" \
  --query 'Listeners[?Port==`80`]' --output text | grep -q . \
  || aws elbv2 create-listener --load-balancer-arn "$ALB_ARN" --protocol HTTP --port 80 \
       --default-actions Type=forward,TargetGroupArn="$TG_ARN" --region "$AWS_REGION" >/dev/null

# Odoo service task def
env_kv(){ printf '{"name":"%s","value":"%s"}' "$1" "$2"; }
ENVJSON="$(paste -sd, <<EOF
$(env_kv TARGET_DB_HOST "$RDS_ENDPOINT")
$(env_kv TARGET_DB_PORT "5432")
$(env_kv TARGET_DB_NAME "$TARGET_DB_NAME")
$(env_kv TARGET_DB_USER "$TARGET_DB_USER")
$(env_kv TARGET_DB_PASSWORD "$TARGET_DB_PASSWORD")
$(env_kv ODOO_MASTER_PASSWORD "$ODOO_MASTER_PASSWORD")
EOF
)"
cat > /tmp/td-odoo.json <<JSON
{
  "family": "$PROJECT-odoo",
  "networkMode": "awsvpc",
  "requiresCompatibilities": ["FARGATE"],
  "cpu": "$TASK_CPU", "memory": "$TASK_MEM",
  "executionRoleArn": "$EXEC_ARN",
  "containerDefinitions": [{
    "name": "odoo",
    "image": "$ECR/$PROJECT/odoo:latest",
    "portMappings": [{"containerPort": 8069, "protocol": "tcp"}],
    "environment": [$ENVJSON],
    "logConfiguration": {"logDriver":"awslogs","options":{
      "awslogs-group":"/ecs/$PROJECT","awslogs-region":"$AWS_REGION",
      "awslogs-stream-prefix":"odoo"}}
  }]
}
JSON
aws ecs register-task-definition --cli-input-json file:///tmp/td-odoo.json --region "$AWS_REGION" >/dev/null

SUBS_CSV="$(echo "$SUBNETS" | tr '[:space:]' ',' | sed 's/,$//')"
if aws ecs describe-services --cluster "$ECS_CLUSTER" --services "$PROJECT-odoo" \
    --region "$AWS_REGION" --query 'services[0].status' --output text 2>/dev/null | grep -q ACTIVE; then
  aws ecs update-service --cluster "$ECS_CLUSTER" --service "$PROJECT-odoo" \
    --task-definition "$PROJECT-odoo" --force-new-deployment --region "$AWS_REGION" >/dev/null
else
  aws ecs create-service --cluster "$ECS_CLUSTER" --service-name "$PROJECT-odoo" \
    --task-definition "$PROJECT-odoo" --desired-count 1 --launch-type FARGATE \
    --network-configuration "awsvpcConfiguration={subnets=[$SUBS_CSV],securityGroups=[$TASK_SG],assignPublicIp=ENABLED}" \
    --load-balancers "targetGroupArn=$TG_ARN,containerName=odoo,containerPort=8069" \
    --health-check-grace-period-seconds 180 --region "$AWS_REGION" >/dev/null
fi
DNS="$(aws elbv2 describe-load-balancers --load-balancer-arns "$ALB_ARN" \
  --region "$AWS_REGION" --query 'LoadBalancers[0].DNSName' --output text)"
put_state ALB_DNS "$DNS"
log "Odoo service deploying. URL: http://$DNS  (login: admin / $ODOO_ADMIN_PASSWORD)"
