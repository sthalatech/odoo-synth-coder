#!/usr/bin/env bash
# Full pipeline. RDS-free (Phase B): no managed RDS; the masker runs a
# postgres:16 sidecar and restores into a throwaway in-task DB. The persistent
# artifact is the masked pg_dump in S3. The source DB is the user's prod DSN
# (set via the control panel per run); image build is the long pole.
set -euo pipefail
cd "$(dirname "$0")/.."
# Install local prerequisites (idempotent): aws + python (pyyaml, boto3) +
# coder CLI. Skips anything already present. Safe to re-run.
bash deploy/00_install_prereqs.sh
# Validate config (config.yaml or legacy config.env) before touching AWS.
bash deploy/00_validate_config.sh
bash deploy/01_ecr.sh
bash deploy/03_network.sh
bash deploy/05_cluster.sh
bash deploy/02_build_push.sh
# Ephemeral-builder IAM (role + instance-profile) used by the profile image
# build path. One-time, idempotent, account-level — same category as ECR/IAM.
bash deploy/10_builder.sh
# Mask: dumps+masks the source (prod DSN) -> throwaway in-task postgres ->
# uploads the masked pg_dump to S3. SOURCE_DB_HOST/creds must be in the env.
bash deploy/07_mask.sh
# Developer-environment control plane: Coder server (one EC2) + publish the
# odoo-synth-env and odoo-synth-builder templates to it. Requires `coder
# login` once (interactive). The builder template must be republished whenever
# coder/templates/odoo-synth-builder changes or builds run a stale user-data.
bash deploy/11_coder_server.sh
bash deploy/12_publish_template.sh
