#!/usr/bin/env bash
# 02: build + push masker, discovery, and odoo images.
#
# The masker + discovery images are basic infrastructure (no Odoo source
# needed) -- they're built every run. The odoo image is a base-layer cache for
# the per-profile provenance build (odoo-synth profile build); it needs an
# ODOO_GIT_REF + optional custom-addons repo, which are profile-level concerns.
# Pass --no-odoo to skip it (used by the guided installer's basic-infra step,
# which has no Odoo ref yet).
source "$(dirname "$0")/lib.sh"

NO_ODOO=0; QUIET=0
for a in "$@"; do case "$a" in --no-odoo) NO_ODOO=1;; --quiet) QUIET=1;; *) ;; esac; done

# Quiet mode (used by the guided installer): docker build/push output goes to
# a logfile; we only surface a one-line progress marker + tail on failure.
BUILD_LOG=""
if [ "$QUIET" = 1 ]; then
  BUILD_LOG="$(mktemp)"
  trap 'rm -f "$BUILD_LOG"' EXIT
fi
# docker_build <image-tag> <ctx> [extra build args...] -- pushes on success.
docker_build(){
  local tag="$1" ctx="$2"; shift 2
  if [ "$QUIET" = 1 ]; then
    printf '  building %s ... ' "$tag"
    if docker build --platform linux/amd64 "$@" -t "$tag" "$ctx" >"$BUILD_LOG" 2>&1 \
       && docker push "$tag" >>"$BUILD_LOG" 2>&1; then
      printf 'ok\n'
    else
      printf 'FAILED\n'; tail -n 20 "$BUILD_LOG"; return 1
    fi
  else
    docker build --platform linux/amd64 "$@" -t "$tag" "$ctx" || return 1
    docker push "$tag" || return 1
  fi
}
# Optional: override the pinned greenmask tarball checksum (see masker/Dockerfile)
# by exporting GREENMASK_TARBALL_SHA256 in your env. If unset, the Dockerfile's
# default (pinned for the default GREENMASK_VERSION) is used, and the build
# fails on any mismatch -- update both together when bumping GREENMASK_VERSION.
docker_build "$ECR/$PROJECT/masker:latest" "$HERE/masker" \
  --build-arg GREENMASK_VERSION="$GREENMASK_VERSION" \
  ${GREENMASK_TARBALL_SHA256:+--build-arg GREENMASK_TARBALL_SHA256="$GREENMASK_TARBALL_SHA256"} || exit 1

docker_build "$ECR/$PROJECT/discovery:latest" "$HERE/discovery" || exit 1

if [ "$NO_ODOO" = 1 ]; then
  log "skipping odoo base image (--no-odoo); it's built per-profile via 'odoo-synth profile build'"
else
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
docker_build "$ECR/$PROJECT/odoo:latest" "$HERE/odoo" \
  --build-arg ODOO_IMAGE="$ODOO_IMAGE" \
  --build-arg ODOO_GIT_URL="$ODOO_GIT_URL" \
  --build-arg ODOO_GIT_REF="$ODOO_GIT_REF" \
  --build-arg CUSTOM_ADDONS_GIT_URL="$CUSTOM_ADDONS_GIT_URL" \
  --build-arg CUSTOM_ADDONS_GIT_REF="${CUSTOM_ADDONS_GIT_REF:-}" \
  --secret id=gh_token,src="$GH_TOKEN_FILE" || exit 1
fi

log "images pushed"
