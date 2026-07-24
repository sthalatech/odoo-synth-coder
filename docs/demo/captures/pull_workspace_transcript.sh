#!/usr/bin/env bash
# Pull an opencode session transcript (JSONL) from a running Coder workspace
# to a local file. Uses the Coder server's ssh_exec helper.
# Usage: pull_workspace_transcript.sh <env_id> <out_jsonl>
set -euo pipefail
ENV_ID="$1"; OUT="$2"
REPO="/home/exedev/odoo-synth-coder"
EXTRACT="$REPO/docs/demo/captures/extract_opencode_session.py"
B64="$(base64 -w0 "$EXTRACT")"
ssh -o StrictHostKeyChecking=no -o BatchMode=yes ubuntu@13.222.25.98 \
  "cd /opt/odoo-synth-coder && .venv/bin/python3 -c \"
import sys; sys.path.insert(0,'lib')
from backend import environments as E
rc,out=E.ssh_exec('$ENV_ID','echo $B64 | base64 -d > /tmp/e.py && sudo -u dev HOME=/home/dev python3 /tmp/e.py /home/dev/.local/share/opencode/opencode.db',timeout=120)
sys.stdout.write(out)
\"" > "$OUT" 2>/dev/null
echo "$OUT: $(grep -c . "$OUT" 2>/dev/null || echo 0) records"
