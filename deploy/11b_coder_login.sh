#!/usr/bin/env bash
# 11b: headless Coder login + first-admin setup.
#
# Runs after 11_coder_server.sh brings the Coder server up and before
# 12_publish_template.sh (which needs a CODER_SESSION_TOKEN). A fresh Coder
# server has an empty DB, so interactive `coder login` would block on browser
# OAuth with no admin to authenticate as. This script:
#   1. Reuses a persisted CODER_SESSION_TOKEN from deploy/state.env if valid.
#   2. Otherwise, if the server has no admin yet, creates one headlessly via
#      CODER_FIRST_USER_* env vars (no browser), mints a named API token, and
#      persists it to state.env.
#   3. If an admin already exists but we have no token, prints the manual
#      re-auth command and exits non-zero so the caller can skip publish.
#
# Configurable via env: CODER_ADMIN_EMAIL, CODER_ADMIN_USER, CODER_ADMIN_PASSWORD
# (random-generated if unset; printed once so the operator can change it later).
# Idempotent + non-interactive: safe to re-run and safe under piped stdin.
source "$(dirname "$0")/lib.sh"

[ -n "${CODER_URL:-}" ] || { log "CODER_URL not set; run 11_coder_server.sh first"; exit 1; }

# Already logged in (persisted token or keyring session valid against this server)?
if coder whoami >/dev/null 2>&1; then
  log "already logged into Coder"
  exit 0
fi

# Does the server still have NO admin? CODER_FIRST_USER_* only works then.
FIRST="$(curl -fsS "$CODER_URL/api/v2/users/first" </dev/null 2>/dev/null || true)"
if printf '%s' "$FIRST" | grep -q '"The initial user has already been created!"'; then
  log "this Coder server already has an admin user. To publish templates you need a"
  log "valid session token. Run on the server host:"
  log "  coder login $CODER_URL   (browser/CLI auth)"
  log "  coder tokens create --name wizard   (then add CODER_SESSION_TOKEN=<token> to deploy/state.env)"
  exit 1
fi

ADMIN_EMAIL="${CODER_ADMIN_EMAIL:-acct.exedev@sthala.dev}"
ADMIN_USER="${CODER_ADMIN_USER:-admin}"
ADMIN_PW="${CODER_ADMIN_PASSWORD:-$(python3 -c 'import secrets,string as s; print("".join(secrets.choice(s.ascii_letters+s.digits) for _ in range(20)))')}"

# CODER_FIRST_USER_TRIAL=false skips the interactive "Start a trial of
# Enterprise? (yes/no)" prompt that otherwise blocks forever when stdin isn't
# a TTY. </dev/null is a safety net against any other prompt.
if ! CODER_FIRST_USER_EMAIL="$ADMIN_EMAIL" \
       CODER_FIRST_USER_USERNAME="$ADMIN_USER" \
       CODER_FIRST_USER_PASSWORD="$ADMIN_PW" \
       CODER_FIRST_USER_TRIAL=false \
       coder login "$CODER_URL" </dev/null >/dev/null 2>&1; then
  log "headless admin setup failed -- open $CODER_URL in a browser to create the"
  log "first admin, then run: coder login $CODER_URL"
  exit 1
fi

# coder login stored the session in the keyring; mint a named API token the
# publish step (12_publish_template.sh) can use, and persist it to state.env.
TOK="$(coder tokens create --name wizard-$(date +%s) 2>/dev/null | tail -1)"
if [ -z "$TOK" ]; then
  log "admin created but could not mint an API token -- run:"
  log "  coder tokens create --name wizard  (then export CODER_SESSION_TOKEN)"
  exit 1
fi
export CODER_SESSION_TOKEN="$TOK"
if ! grep -q '^CODER_SESSION_TOKEN=' "$HERE/deploy/state.env" 2>/dev/null; then
  printf 'CODER_SESSION_TOKEN=%s\n' "$TOK" >> "$HERE/deploy/state.env"
else
  sed -i "s|^CODER_SESSION_TOKEN=.*|CODER_SESSION_TOKEN=$TOK|" "$HERE/deploy/state.env"
fi
log "first admin created ($ADMIN_EMAIL) + logged in"
log "  admin password: $ADMIN_PW (change it in the UI later)"
