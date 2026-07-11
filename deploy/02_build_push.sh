#!/usr/bin/env bash
# 02: build + push masker and odoo images.
source "$(dirname "$0")/lib.sh"

log "building masker image ..."
docker build --platform linux/amd64 \
  --build-arg GREENMASK_VERSION="$GREENMASK_VERSION" \
  -t "$ECR/$PROJECT/masker:latest" "$HERE/masker"
docker push "$ECR/$PROJECT/masker:latest"

log "building odoo image (from source ref ${ODOO_GIT_REF}) ..."
docker build --platform linux/amd64 \
  --build-arg ODOO_IMAGE="$ODOO_IMAGE" \
  --build-arg ODOO_GIT_URL="$ODOO_GIT_URL" \
  --build-arg ODOO_GIT_REF="$ODOO_GIT_REF" \
  --build-arg CUSTOM_ADDONS_GIT_URL="$CUSTOM_ADDONS_GIT_URL" \
  -t "$ECR/$PROJECT/odoo:latest" "$HERE/odoo"
docker push "$ECR/$PROJECT/odoo:latest"

log "images pushed"
