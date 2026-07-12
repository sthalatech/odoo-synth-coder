#!/usr/bin/env bash
# Run the control panel locally against your existing AWS config.
# Requires AWS credentials in the environment (same ones the deploy scripts use).
set -euo pipefail
cd "$(dirname "$0")"

if [ ! -d .venv ]; then
  python3 -m venv .venv
  ./.venv/bin/pip install -q --upgrade pip
  ./.venv/bin/pip install -q -r requirements.txt
fi

export PORT="${PORT:-8000}"
echo "control panel -> http://localhost:${PORT}"
# run from the controlpanel dir; config.py finds ../config.env and ../deploy/state.env
exec ./.venv/bin/uvicorn backend.main:app --host 0.0.0.0 --port "${PORT}" --reload
