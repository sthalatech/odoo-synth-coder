#!/usr/bin/env bash
# 09: OPTIONAL second Odoo service pointed at the UNMASKED source DB, exposed on
# the same ALB via a listener on port 8080 (masked stays on port 80). For
# side-by-side testing only. Run after 08. Tear down with teardown.sh.
source "$(dirname "$0")/lib.sh"
: "${RDS_ENDPOINT:?}" "${EXEC_ARN:?}" "${ALB_SG:?}" "${TASK_SG:?}"
VPC="$(vpc_id)"; SUBNETS="$(subnet_ids)"
PORT=8080

# Allow 8080 into the ALB.
aws ec2 authorize-security-group-ingress --group-id "$ALB_SG" --protocol tcp \
  --port "$PORT" --cidr 0.0.0.0/0 --region "$AWS_REGION" >/dev/null 2>&1 || true

# ALB (must already exist from 08).
ALB_ARN="$(aws elbv2 describe-load-balancers --names "$PROJECT-alb" --region "$AWS_REGION" \
  --query 'LoadBalancers[0].LoadBalancerArn' --output text 2>/dev/null || true)"
[ -n "$ALB_ARN" ] && [ "$ALB_ARN" != "None" ] || { log "run 08 first (no ALB)"; exit 1; }

# Source target group.
TG_ARN="$(aws elbv2 describe-target-groups --names "$PROJECT-src-tg" --region "$AWS_REGION" \
  --query 'TargetGroups[0].TargetGroupArn' --output text 2>/dev/null || true)"
if [ -z "$TG_ARN" ] || [ "$TG_ARN" = "None" ]; then
  TG_ARN="$(aws elbv2 create-target-group --name "$PROJECT-src-tg" --protocol HTTP --port 8069 \
    --vpc-id "$VPC" --target-type ip --health-check-path /web/login \
    --health-check-interval-seconds 30 --healthy-threshold-count 2 \
    --region "$AWS_REGION" --query 'TargetGroups[0].TargetGroupArn' --output text)"
fi

# Listener on 8080 -> source TG.
aws elbv2 describe-listeners --load-balancer-arn "$ALB_ARN" --region "$AWS_REGION" \
  --query "Listeners[?Port==\`$PORT\`]" --output text | grep -q . \
  || aws elbv2 create-listener --load-balancer-arn "$ALB_ARN" --protocol HTTP --port "$PORT" \
       --default-actions Type=forward,TargetGroupArn="$TG_ARN" --region "$AWS_REGION" >/dev/null

# Odoo task def pointed at the SOURCE db (reuses TARGET_DB_* env; entrypoint is
# generic). dbfilter locks the UI to the source database name.
env_kv(){ printf '{"name":"%s","value":"%s"}' "$1" "$2"; }
ENVJSON="$(paste -sd, <<EOF
$(env_kv TARGET_DB_HOST "$RDS_ENDPOINT")
$(env_kv TARGET_DB_PORT "5432")
$(env_kv TARGET_DB_NAME "$SOURCE_DB_NAME")
$(env_kv TARGET_DB_USER "$TARGET_DB_USER")
$(env_kv TARGET_DB_PASSWORD "$TARGET_DB_PASSWORD")
$(env_kv ODOO_MASTER_PASSWORD "$ODOO_MASTER_PASSWORD")
EOF
)"
cat > /tmp/td-odoo-src.json <<JSON
{
  "family": "$PROJECT-odoo-src",
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
      "awslogs-stream-prefix":"odoo-src"}}
  }]
}
JSON
aws ecs register-task-definition --cli-input-json file:///tmp/td-odoo-src.json --region "$AWS_REGION" >/dev/null

SUBS_CSV="$(echo "$SUBNETS" | tr '[:space:]' ',' | sed 's/,$//')"
if aws ecs describe-services --cluster "$ECS_CLUSTER" --services "$PROJECT-odoo-src" \
    --region "$AWS_REGION" --query 'services[0].status' --output text 2>/dev/null | grep -q ACTIVE; then
  aws ecs update-service --cluster "$ECS_CLUSTER" --service "$PROJECT-odoo-src" \
    --task-definition "$PROJECT-odoo-src" --force-new-deployment --region "$AWS_REGION" >/dev/null
else
  aws ecs create-service --cluster "$ECS_CLUSTER" --service-name "$PROJECT-odoo-src" \
    --task-definition "$PROJECT-odoo-src" --desired-count 1 --launch-type FARGATE \
    --network-configuration "awsvpcConfiguration={subnets=[$SUBS_CSV],securityGroups=[$TASK_SG],assignPublicIp=ENABLED}" \
    --load-balancers "targetGroupArn=$TG_ARN,containerName=odoo,containerPort=8069" \
    --health-check-grace-period-seconds 180 --region "$AWS_REGION" >/dev/null
fi
DNS="$(aws elbv2 describe-load-balancers --load-balancer-arns "$ALB_ARN" \
  --region "$AWS_REGION" --query 'LoadBalancers[0].DNSName' --output text)"
put_state SRC_ALB_URL "http://$DNS:$PORT"
log "SOURCE Odoo (UNMASKED) deploying. URL: http://$DNS:$PORT"
