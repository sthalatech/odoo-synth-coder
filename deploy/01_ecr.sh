#!/usr/bin/env bash
# 01: create ECR repos and docker login.
source "$(dirname "$0")/lib.sh"

for name in masker odoo; do
  aws ecr describe-repositories --repository-names "$PROJECT/$name" --region "$AWS_REGION" >/dev/null 2>&1 \
    || aws ecr create-repository --repository-name "$PROJECT/$name" \
         --image-scanning-configuration scanOnPush=true --region "$AWS_REGION" >/dev/null
  log "ecr repo ready: $PROJECT/$name"
done

aws ecr get-login-password --region "$AWS_REGION" \
  | docker login --username AWS --password-stdin "$ECR"
log "docker logged in to $ECR"
