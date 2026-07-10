#!/usr/bin/env bash
# Full pipeline. The independent SOURCE stack must exist first (deploy/source/run.sh)
# so the masker has a database to read. RDS + image build are the long poles.
set -euo pipefail
cd "$(dirname "$0")/.."
bash deploy/01_ecr.sh
bash deploy/03_network.sh
bash deploy/04_rds.sh
bash deploy/05_cluster.sh
bash deploy/02_build_push.sh
# Source stack (persistent). Skipped automatically if it already exists.
if ! grep -q '^SRC_RDS_ENDPOINT=' deploy/state.env 2>/dev/null; then
  bash deploy/source/run.sh
fi
bash deploy/07_mask.sh
bash deploy/08_odoo_service.sh
