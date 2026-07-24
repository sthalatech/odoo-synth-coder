#!/usr/bin/env bash
# 02: build + push the basic-infrastructure images (masker + discovery).
#
# These two need no Odoo source and are built every run. The Odoo image is NOT
# built here -- it is a profile-level concern baked per-profile from the
# profile's pinned odoo_git_ref + custom addons via 'odoo-synth profile build'
# (the odoo-synth-builder Coder template). Nothing in basic infra consumes the
# Odoo image; the ECR repo for it is still created by 01_ecr.sh so per-profile
# builds have somewhere to push.
source "$(dirname "$0")/lib.sh"

QUIET=0
for a in "$@"; do case "$a" in --no-odoo) ;; # accepted + ignored (obsolete)
 --quiet) QUIET=1;; *) ;; esac; done

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

log "basic-infra images pushed (masker, discovery)"
