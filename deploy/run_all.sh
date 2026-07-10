#!/usr/bin/env bash
# Run the full pipeline in order. RDS + image build run first (long poles).
set -euo pipefail
cd "$(dirname "$0")/.."
bash deploy/01_ecr.sh
bash deploy/03_network.sh
bash deploy/04_rds.sh
bash deploy/05_cluster.sh
bash deploy/02_build_push.sh
bash deploy/06_seed.sh
bash deploy/07_mask.sh
bash deploy/08_odoo_service.sh
