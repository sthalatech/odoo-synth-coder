#!/usr/bin/env bash
# 11b: headless Coder login + first-admin setup.
#
# Runs after 11_coder_server.sh brings the Coder server up and before
# 12_publish_template.sh (which needs a CODER_SESSION_TOKEN). A fresh Coder
# server has an empty DB, so interactive `coder login` would block on browser
# OAuth with no admin to authenticate as. This script:
#   1. Reuses a persisted CODER_SESSION_TOKEN from deploy/state.env if valid
#      (verified via `coder whoami`).
#   2. Otherwise, if the server has no admin yet, creates one headlessly via
#      CODER_FIRST_USER_* env vars (no browser), mints a named API token, and
#      persists the token + admin credentials to state.env.
#   3. If an admin already exists (server was set up on a previous run) but the
#      persisted token is stale/missing, re-authenticates via the Coder login
#      API (POST /api/v2/users/login with the persisted admin email+password)
#      and mints a fresh named API token. This is the common re-run case: the
#      first wizard run created the admin + token, a later run finds the token
#      expired (Coder session keys default to 7d) and must re-login without a
#      browser. Falls back to manual re-auth only if the admin password was
#      never persisted or no longer matches.
#
# Configurable via env: CODER_ADMIN_EMAIL, CODER_ADMIN_USER, CODER_ADMIN_PASSWORD
# (random-generated if unset; printed once so the operator can change it later).
# Idempotent + non-interactive: safe to re-run and safe under piped stdin.
source "$(dirname "$0")/lib.sh"

[ -n "${CODER_URL:-}" ] || { log "CODER_URL not set; run 11_coder_server.sh first"; exit 1; }

# ---------------------------------------------------------------------------
# Helper: mint a named API token from an existing session token and persist
# both the new token and the admin credentials to state.env.
#   $1 = a session token valid against $CODER_URL ("keyring" to use the
#        keyring session stored by `coder login`, e.g. after first-admin setup)
#   $2 = admin email (for state.env persistence; "" to skip cred persistence)
#   $3 = admin password (for state.env persistence; "" to skip cred persistence)
# Exports CODER_SESSION_TOKEN on success. Returns non-zero on failure.
# ---------------------------------------------------------------------------
mint_and_persist() {
  local sess="$1" email="$2" pw="$3" TOK
  if [ "$sess" = "keyring" ]; then
    TOK="$(coder tokens create --name "wizard-$(date +%s)" 2>/dev/null | tail -1)"
  else
    TOK="$(CODER_SESSION_TOKEN="$sess" coder tokens create --name "wizard-$(date +%s)" 2>/dev/null | tail -1)"
  fi
  if [ -z "$TOK" ]; then
    log "could not mint an API token from the session -- run:"
    log "  coder login $CODER_URL  &&  coder tokens create --name wizard"
    return 1
  fi
  put_state CODER_SESSION_TOKEN "$TOK"
  # Persist admin creds so a future run (token expired) can re-login headlessly
  # via the login API without a browser. Storing the password in state.env
  # (gitignored, host-local) is the same trust boundary as CODER_SESSION_TOKEN.
  [ -n "$email" ] && put_state CODER_ADMIN_EMAIL "$email"
  [ -n "$pw"   ] && put_state CODER_ADMIN_PASSWORD "$pw"
  export CODER_SESSION_TOKEN="$TOK"
  return 0
}

# ---------------------------------------------------------------------------
# 1. Already logged in? (persisted token or keyring session valid here)
#    If the keyring session is valid but NO token is persisted to state.env
#    (e.g. the operator ran `coder login` interactively after a fresh server
#    setup, or a prior run minted one but state.env was reset), mint a named
#    API token from that session and persist it -- otherwise step 7c (publish
#    templates) sees no CODER_SESSION_TOKEN and silently skips publishing.
# ---------------------------------------------------------------------------
if coder whoami >/dev/null 2>&1; then
  if [ -n "${CODER_SESSION_TOKEN:-}" ] &&      CODER_SESSION_TOKEN="$CODER_SESSION_TOKEN" coder whoami >/dev/null 2>&1; then
    log "already logged into Coder (persisted token valid)"
    exit 0
  fi
  log "already logged into Coder (keyring session valid); minting a persisted API token ..."
  # The keyring session authenticates `coder tokens create` (no explicit token).
  ADMIN_EMAIL="${CODER_ADMIN_EMAIL:-}"
  ADMIN_PW="${CODER_ADMIN_PASSWORD:-}"
  if mint_and_persist "keyring" "$ADMIN_EMAIL" "$ADMIN_PW"; then
    log "minted + persisted CODER_SESSION_TOKEN to deploy/state.env"
    exit 0
  fi
  # Could not mint (e.g. keyring session read-only / token-scope) -> tell the
  # operator to mint one manually so publish does not silently skip.
  log "could not mint an API token from the keyring session. Run on this host:"
  log "  coder login $CODER_URL  &&  coder tokens create --name wizard"
  log "  then add CODER_SESSION_TOKEN=<token> to deploy/state.env"
  exit 1
fi

# ---------------------------------------------------------------------------
# 2. Does the server still have NO admin? CODER_FIRST_USER_* only works then.
# ---------------------------------------------------------------------------
FIRST="$(curl -fsS "$CODER_URL/api/v2/users/first" </dev/null 2>/dev/null || true)"
if ! printf '%s' "$FIRST" | grep -q '"The initial user has already been created!"'; then
  # --- Fresh server: create the first admin headlessly. ---
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
  # publish step (12_publish_template.sh) can use. The keyring session is used
  # implicitly by the coder CLI (no explicit token needed right after login).
  if mint_and_persist "keyring" "$ADMIN_EMAIL" "$ADMIN_PW"; then
    log "first admin created ($ADMIN_EMAIL) + logged in"
    log "  admin password: $ADMIN_PW (change it in the UI later)"
    exit 0
  fi
  exit 1
fi

# ---------------------------------------------------------------------------
# 3. Admin already exists + persisted token is stale/missing. Try to re-login
#    via the login API using persisted admin credentials (no browser).
# ---------------------------------------------------------------------------
log "this Coder server already has an admin user; the persisted session token is"
log "stale or missing. Attempting headless re-login via the login API ..."

# Credentials passed explicitly for THIS run take priority (operator may have
# changed the password in the UI); otherwise use what's persisted in state.env
# (sourced by lib.sh as CODER_ADMIN_EMAIL / CODER_ADMIN_PASSWORD).
ADMIN_EMAIL="${CODER_ADMIN_EMAIL:-}"
ADMIN_PW="${CODER_ADMIN_PASSWORD:-}"

if [ -z "$ADMIN_EMAIL" ] || [ -z "$ADMIN_PW" ]; then
  log "no admin credentials available to re-login headlessly. Either:"
  log "  (a) re-run with: CODER_ADMIN_EMAIL=<email> CODER_ADMIN_PASSWORD=<pw> bash deploy/11b_coder_login.sh"
  log "  (b) log in interactively on a host with a browser:"
  log "      coder login $CODER_URL"
  log "      coder tokens create --name wizard   (then add CODER_SESSION_TOKEN=<token> to deploy/state.env)"
  exit 1
fi

# POST /api/v2/users/login -> {"session_token":"..."} on 201 Created.
# We build the JSON body with python3 (already a prerequisite) so a password
# containing quotes/backslashes is safely escaped, and parse the response with
# python3 to avoid a jq dependency.
BODY="$(python3 -c 'import json,sys; print(json.dumps({"email":sys.argv[1],"password":sys.argv[2]}))' "$ADMIN_EMAIL" "$ADMIN_PW")"
LOGIN_RESP="$(curl -fsS -X POST "$CODER_URL/api/v2/users/login" \
  -H 'Content-Type: application/json' \
  -d "$BODY" </dev/null 2>&1)" || true

SESS_TOKEN="$(printf '%s' "$LOGIN_RESP" | python3 -c 'import json,sys
try:
    print(json.load(sys.stdin).get("session_token",""))
except Exception:
    print("")
' 2>/dev/null)"

if [ -z "$SESS_TOKEN" ]; then
  log "login API rejected the admin credentials:"
  printf '%s\n' "$LOGIN_RESP" | sed 's/^/    /' >&2
  log "the admin password may have been changed in the UI since setup. Either:"
  log "  (a) re-run with the current password: CODER_ADMIN_EMAIL=<email> CODER_ADMIN_PASSWORD=<pw> bash deploy/11b_coder_login.sh"
  log "  (b) log in interactively: coder login $CODER_URL  &&  coder tokens create --name wizard"
  exit 1
fi

# The login session_token is itself a usable API key; mint a named long-lived
# token from it for the publish step + future runs, and persist creds.
if mint_and_persist "$SESS_TOKEN" "$ADMIN_EMAIL" "$ADMIN_PW"; then
  log "re-logged in as $ADMIN_EMAIL via the login API + minted a fresh token"
  exit 0
fi
exit 1
