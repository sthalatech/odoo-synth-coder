#!/usr/bin/env bash
# 02: build + push masker and odoo images.
source "$(dirname "$0")/lib.sh"

log "building masker image ..."
docker build --platform linux/amd64 \
  --build-arg GREENMASK_VERSION="$GREENMASK_VERSION" \
  -t "$ECR/$PROJECT/masker:latest" "$HERE/masker"
docker push "$ECR/$PROJECT/masker:latest"

log "building discovery image ..."
docker build --platform linux/amd64 \
  -t "$ECR/$PROJECT/discovery:latest" "$HERE/discovery"
docker push "$ECR/$PROJECT/discovery:latest"

log "building odoo image (from source ref ${ODOO_GIT_REF}) ..."
export DOCKER_BUILDKIT=1
# Resolve a GitHub token for cloning the (possibly private) custom addons repo.
# Priority: GITHUB_TOKEN/GH_TOKEN env > gh CLI > git credential helper. Written
# to a temp file passed as a BuildKit secret (never a build-arg / image layer).
GH_TOKEN_FILE="$(mktemp)"; chmod 600 "$GH_TOKEN_FILE"
trap 'rm -f "$GH_TOKEN_FILE"' EXIT
TOK="${GITHUB_TOKEN:-${GH_TOKEN:-}}"
if [ -z "$TOK" ] && command -v gh >/dev/null 2>&1; then
  TOK="$(gh auth token 2>/dev/null || true)"
fi
if [ -z "$TOK" ]; then
  TOK="$(printf 'protocol=https\nhost=github.com\n\n' | git credential fill 2>/dev/null | sed -n 's/^password=//p')"
fi
printf '%s' "$TOK" > "$GH_TOKEN_FILE"
if [ -s "$GH_TOKEN_FILE" ]; then log "github token resolved for custom-addons clone"; \
  else log "WARNING: no github token; private custom-addons clone will fail"; fi
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
  --secret id=gh_token,src="$GH_TOKEN_FILE" \
  -t "$ECR/$PROJECT/odoo:latest" "$HERE/odoo"
docker push "$ECR/$PROJECT/odoo:latest"

log "images pushed"
