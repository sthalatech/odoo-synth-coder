#!/usr/bin/env bash
# 02: build + push masker and odoo images.
source "$(dirname "$0")/lib.sh"

log "building masker image ..."
docker build --platform linux/amd64 \
  --build-arg GREENMASK_VERSION="$GREENMASK_VERSION" \
  -t "$ECR/$PROJECT/masker:latest" "$HERE/masker"
docker push "$ECR/$PROJECT/masker:latest"

log "building odoo image (from source ref ${ODOO_GIT_REF}) ..."
# Ensure bake-in dirs exist so the Dockerfile COPY steps never fail.
mkdir -p "$HERE/odoo/enterprise" "$HERE/odoo/custom-addons"
# Auto-unzip an enterprise bundle if provided as odoo/enterprise.zip. Flatten a
# single top-level wrapper dir (e.g. enterprise-19.0/) so module folders land
# directly under odoo/enterprise/.
if [ -f "$HERE/odoo/enterprise.zip" ]; then
  log "unzipping enterprise.zip into odoo/enterprise/ ..."
  tmp="$(mktemp -d)"; unzip -q -o "$HERE/odoo/enterprise.zip" -d "$tmp"
  inner="$tmp"
  if [ "$(find "$tmp" -maxdepth 1 -mindepth 1 -type d | wc -l)" = "1" ] \
     && [ -z "$(find "$tmp" -maxdepth 1 -type f)" ]; then
    inner="$(find "$tmp" -maxdepth 1 -mindepth 1 -type d)"
  fi
  rm -rf "$HERE/odoo/enterprise"; mkdir -p "$HERE/odoo/enterprise"
  cp -a "$inner/." "$HERE/odoo/enterprise/"
  rm -rf "$tmp"
  log "enterprise modules staged: $(find "$HERE/odoo/enterprise" -maxdepth 1 -mindepth 1 -type d | wc -l)"
fi
docker build --platform linux/amd64 \
  --build-arg ODOO_IMAGE="$ODOO_IMAGE" \
  --build-arg ODOO_GIT_URL="$ODOO_GIT_URL" \
  --build-arg ODOO_GIT_REF="$ODOO_GIT_REF" \
  --build-arg CUSTOM_ADDONS_GIT_URL="$CUSTOM_ADDONS_GIT_URL" \
  --build-arg CUSTOM_ADDONS_GIT_REF="${CUSTOM_ADDONS_GIT_REF:-}" \
  -t "$ECR/$PROJECT/odoo:latest" "$HERE/odoo"
docker push "$ECR/$PROJECT/odoo:latest"

log "images pushed"
