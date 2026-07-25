#!/usr/bin/env bash
# DEPRECATED: use the guided installer instead:  bash deploy/00_setup.sh
# Kept temporarily as a thin non-interactive wrapper around the same deploy
# scripts. Will be removed once the guided installer is confirmed end-to-end.
#
# Full pipeline provisioning: ECR + basic images (masker, discovery) + builder
# IAM + Coder server + templates. Masking + envs run on demand via the
# `odoo-synth` CLI (Coder runner/builder/env workspaces) -- no standing
# ECS/Fargate cluster to provision. The Odoo image is NOT built here; it's
# baked per-profile via `odoo-synth profile build`.
set -euo pipefail
cd "$(dirname "$0")/.."
# Install local prerequisites (idempotent): aws + python (pyyaml, boto3) +
# coder CLI. Skips anything already present. Safe to re-run.
bash deploy/00_install_prereqs.sh
# Validate config.yaml before touching AWS.
bash deploy/00_validate_config.sh
bash deploy/01_ecr.sh
bash deploy/02_build_push.sh
# Ephemeral-builder IAM (role + instance-profile) used by the profile image
# build path. One-time, idempotent, account-level — same category as ECR/IAM.
bash deploy/10_builder.sh
# Developer-environment instance role + profile + SG + subnet (infra-only:
# no AMI bake; that happens per-profile). Creates ENV_INSTANCE_PROFILE that
# 11_coder_server.sh grants iam:PassRole on (so the Coder server can launch
# workspace VMs assuming the env-instance role).
bash deploy/09_dev_env.sh
# Masking now runs on demand via `odoo-synth run mask` (Coder runner workspace)
# -- no standing ECS cluster to provision.
# Developer-environment control plane: Coder server (one EC2) + publish the
# odoo-synth-env and odoo-synth-builder templates to it. Requires `coder
# login` once (interactive). The builder template must be republished whenever
# coder/templates/odoo-synth-builder changes or builds run a stale user-data.
bash deploy/11_coder_server.sh
set -a; . deploy/state.env; set +a
# Coder login: headless first-admin setup + persist a session token to
# state.env so 12_publish_template.sh (and later CLI runs) can auth. If an
# admin already exists, reuse a persisted token or skip (re-auth manually).
if ! coder whoami >/dev/null 2>&1; then
  FIRST="$(curl -fsS "$CODER_URL/api/v2/users/first" </dev/null 2>/dev/null || true)"
  if ! printf '%s' "$FIRST" | grep -q '"already been created"'; then
    ADMIN_PW="${CODER_ADMIN_PASSWORD:-$(python3 -c 'import secrets,string as s; print("".join(secrets.choice(s.ascii_letters+s.digits) for _ in range(20)))')}"
    CODER_FIRST_USER_EMAIL="${CODER_ADMIN_EMAIL:-acct.exedev@sthala.dev}" \
    CODER_FIRST_USER_USERNAME="${CODER_ADMIN_USER:-admin}" \
    CODER_FIRST_USER_PASSWORD="$ADMIN_PW" \
    CODER_FIRST_USER_TRIAL=false \
    coder login "$CODER_URL" </dev/null >/dev/null 2>&1
    TOK="$(coder tokens create --name runall-$(date +%s) 2>/dev/null | tail -1)"
    [ -n "$TOK" ] && export CODER_SESSION_TOKEN="$TOK"       && grep -q '^CODER_SESSION_TOKEN=' deploy/state.env 2>/dev/null         && sed -i "s|^CODER_SESSION_TOKEN=.*|CODER_SESSION_TOKEN=$TOK|" deploy/state.env         || printf 'CODER_SESSION_TOKEN=%s\n' "$TOK" >> deploy/state.env
  fi
fi
bash deploy/12_publish_template.sh --quiet
