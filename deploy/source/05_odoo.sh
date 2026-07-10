#!/usr/bin/env bash
# SOURCE stack 05: dedicated ALB + Odoo service pointed at the UNMASKED source DB.
source "$(dirname "$0")/../lib.sh"
: "${SRC_RDS_ENDPOINT:?}" "${EXEC_ARN:?}" "${SRC_ALB_SG:?}" "${SRC_TASK_SG:?}"
VPC="$(vpc_id)"; SUBNETS="$(subnet_ids)"

# ALB
ALB_ARN="$(aws elbv2 describe-load-balancers --names "$PROJECT-src-alb" --region "$AWS_REGION" \
  --query 'LoadBalancers[0].LoadBalancerArn' --output text 2>/dev/null || true)"
if [ -z "$ALB_ARN" ] || [ "$ALB_ARN" = "None" ]; then
  ALB_ARN="$(aws elbv2 create-load-balancer --name "$PROJECT-src-alb" --type application \
    --subnets $SUBNETS --security-groups "$SRC_ALB_SG" --region "$AWS_REGION" \
    --query 'LoadBalancers[0].LoadBalancerArn' --output text)"
fi
# Target group
TG_ARN="$(aws elbv2 describe-target-groups --names "$PROJECT-src-tg" --region "$AWS_REGION" \
  --query 'TargetGroups[0].TargetGroupArn' --output text 2>/dev/null || true)"
if [ -z "$TG_ARN" ] || [ "$TG_ARN" = "None" ]; then
  TG_ARN="$(aws elbv2 create-target-group --name "$PROJECT-src-tg" --protocol HTTP --port 8069 \
    --vpc-id "$VPC" --target-type ip --health-check-path /web/login \
    --health-check-interval-seconds 30 --healthy-threshold-count 2 \
    --region "$AWS_REGION" --query 'TargetGroups[0].TargetGroupArn' --output text)"
fi
# Listener 80 -> TG
aws elbv2 describe-listeners --load-balancer-arn "$ALB_ARN" --region "$AWS_REGION" \
  --query 'Listeners[?Port==`80`]' --output text | grep -q . \
  || aws elbv2 create-listener --load-balancer-arn "$ALB_ARN" --protocol HTTP --port 80 \
       --default-actions Type=forward,TargetGroupArn="$TG_ARN" --region "$AWS_REGION" >/dev/null

env_kv(){ printf '{"name":"%s","value":"%s"}' "$1" "$2"; }
ENVJSON="$(paste -sd, <<EOF
$(env_kv TARGET_DB_HOST "$SRC_RDS_ENDPOINT")
$(env_kv TARGET_DB_PORT "5432")
$(env_kv TARGET_DB_NAME "$SOURCE_DB_NAME")
$(env_kv TARGET_DB_USER "$SOURCE_DB_MASTER_USER")
$(env_kv TARGET_DB_PASSWORD "$SOURCE_DB_MASTER_PASSWORD")
$(env_kv ODOO_MASTER_PASSWORD "$ODOO_MASTER_PASSWORD")
EOF
)"
cat > /tmp/td-src-odoo.json <<JSON
{
  "family": "$PROJECT-src-odoo",
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
      "awslogs-group":"/ecs/$PROJECT-source","awslogs-region":"$AWS_REGION",
      "awslogs-stream-prefix":"odoo"}}
  }]
}
JSON
aws ecs register-task-definition --cli-input-json file:///tmp/td-src-odoo.json --region "$AWS_REGION" >/dev/null

SUBS_CSV="$(echo "$SUBNETS" | tr '[:space:]' ',' | sed 's/,$//')"
if aws ecs describe-services --cluster "$SOURCE_ECS_CLUSTER" --services "$PROJECT-src-odoo" \
    --region "$AWS_REGION" --query 'services[0].status' --output text 2>/dev/null | grep -q ACTIVE; then
  aws ecs update-service --cluster "$SOURCE_ECS_CLUSTER" --service "$PROJECT-src-odoo" \
    --task-definition "$PROJECT-src-odoo" --force-new-deployment --region "$AWS_REGION" >/dev/null
else
  aws ecs create-service --cluster "$SOURCE_ECS_CLUSTER" --service-name "$PROJECT-src-odoo" \
    --task-definition "$PROJECT-src-odoo" --desired-count 1 --launch-type FARGATE \
    --network-configuration "awsvpcConfiguration={subnets=[$SUBS_CSV],securityGroups=[$SRC_TASK_SG],assignPublicIp=ENABLED}" \
    --load-balancers "targetGroupArn=$TG_ARN,containerName=odoo,containerPort=8069" \
    --health-check-grace-period-seconds 180 --region "$AWS_REGION" >/dev/null
fi
DNS="$(aws elbv2 describe-load-balancers --load-balancer-arns "$ALB_ARN" \
  --region "$AWS_REGION" --query 'LoadBalancers[0].DNSName' --output text)"
put_state SRC_ALB_DNS "$DNS"
log "SOURCE Odoo (UNMASKED) deploying. URL: http://$DNS  (login: admin / admin)"
