#!/usr/bin/env bash
# Stand up the full independent SOURCE stack (persistent). Requires the shared
# exec role (05_cluster.sh) and images (02_build_push.sh) to exist.
set -euo pipefail
cd "$(dirname "$0")/../.."
bash deploy/source/01_network.sh
bash deploy/source/02_rds.sh
bash deploy/source/03_cluster.sh
bash deploy/source/04_seed.sh
bash deploy/source/05_odoo.sh
