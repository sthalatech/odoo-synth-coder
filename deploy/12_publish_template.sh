#!/usr/bin/env bash
# 12: publish the odoo-synth-env Coder template to the running Coder server.
#
# `coder templates push` reads the Terraform in coder/templates/odoo-synth-env
# and creates/updates the named template so developers (and the control panel's
# `coder create -t odoo-synth-env ...`) can launch workspaces from it. Requires
# CODER_URL + CODER_SESSION_TOKEN (set after `coder login`).
source "$(dirname "$0")/lib.sh"

TPL_NAME="${CODER_TEMPLATE:-odoo-synth-env}"
TPL_DIR="$HERE/coder/templates/odoo-synth-env"
[ -d "$TPL_DIR" ] || { log "missing $TPL_DIR"; exit 1; }
[ -n "${CODER_URL:-}" ] || { log "CODER_URL not set; run deploy/11_coder_server.sh first"; exit 1; }
[ -n "${CODER_SESSION_TOKEN:-}" ] || { log "CODER_SESSION_TOKEN not set; run 'coder login $CODER_URL' first"; exit 1; }

log "publishing Coder template $TPL_NAME from $TPL_DIR ..."
cd "$TPL_DIR"
if coder templates push -y "$TPL_NAME" 2>&1 | tee /tmp/coder-push.log; then
  log "template $TPL_NAME published -> $CODER_URL/templates/$TPL_NAME"
else
  log "template push failed (see /tmp/coder-push.log)"
  exit 1
fi
