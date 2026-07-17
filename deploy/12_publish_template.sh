#!/usr/bin/env bash
# 12: publish the Coder templates to the running Coder server.
#
# `coder templates push` reads the Terraform in coder/templates/<name> and
# creates/updates the named template so workspaces can be launched from it.
# Publishes both the developer-environment template (odoo-synth-env) and the
# ephemeral image-builder template (odoo-synth-builder) -- the panel/CLI build
# path launches builder workspaces from the published template version, so it
# must be republished whenever coder/templates/odoo-synth-builder changes or
# builds will run a stale user-data script. Requires CODER_URL +
# CODER_SESSION_TOKEN (set after `coder login`).
source "$(dirname "$0")/lib.sh"

# Space-separated list of Coder templates to publish. Override with
# CODER_TEMPLATES="odoo-synth-env" to publish only one.
TEMPLATES="${CODER_TEMPLATES:-odoo-synth-env odoo-synth-builder}"
[ -n "${CODER_URL:-}" ] || { log "CODER_URL not set; run deploy/11_coder_server.sh first"; exit 1; }
[ -n "${CODER_SESSION_TOKEN:-}" ] || { log "CODER_SESSION_TOKEN not set; run 'coder login $CODER_URL' first"; exit 1; }

# Regenerate workspace presets from the profile store (one preset per profile
# with a built image + a successful mask run) before pushing the env template,
# so the Coder dashboard "Create workspace" flow shows every available masked
# profile. (Only meaningful for odoo-synth-env; harmless for the builder.)
python3 "$HERE/deploy/_gen_presets.py" || log "WARN: preset generation failed (continuing)"

rc=0
for TPL_NAME in $TEMPLATES; do
  TPL_DIR="$HERE/coder/templates/$TPL_NAME"
  [ -d "$TPL_DIR" ] || { log "missing $TPL_DIR; skipping $TPL_NAME"; rc=1; continue; }
  log "publishing Coder template $TPL_NAME from $TPL_DIR ..."
  cd "$TPL_DIR"
  # --directory is required: without it the CLI uploads a 0-byte source archive
  # and the server-side import provision fails with "No configuration files".
  if coder templates push -y --directory "$TPL_DIR" "$TPL_NAME" 2>&1 | tee "/tmp/coder-push-$TPL_NAME.log"; then
    log "template $TPL_NAME published -> $CODER_URL/templates/$TPL_NAME"
  else
    log "template $TPL_NAME push failed (see /tmp/coder-push-$TPL_NAME.log)"
    rc=1
  fi
done
exit $rc
