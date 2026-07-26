#!/usr/bin/env bash
# 13: GitHub webhook listener on the Coder server.
#
# Installs scripts/webhook_listener.py ON THE CODER SERVER (the always-on
# control plane), as a systemd service, and opens a 2nd inbound port on the
# existing Coder SG so GitHub can reach it. GitHub POSTs `issues` events from
# the profile's addons repo here; the listener verifies the HMAC signature and
# runs scripts/issue_to_env.py to create an env from the latest preset for that
# repo + drive the agent (opencode/claude-code via superpowers).
#
# Why the Coder server (not a dev VM): it is the only always-on box, it already
# holds CODER_URL + CODER_SESSION_TOKEN, and its IAM role + the dumps bucket
# give it access to the S3-backed profile/run stores (the preset resolver).
# `coder create` then talks to the Coder API on localhost.
#
# Prereqs: deploy/11_coder_server.sh must have run (CODER_SERVER_IP +
# CODER_SG_ID + CODER_INSTANCE_ID in deploy/state.env). The Coder server's SG
# must allow SSH from this host (22 is opened to the caller's IP by 11).
#
# Usage:
#   deploy/13_webhook_listener.sh                         # install/upgrade
#   deploy/13_webhook_listener.sh --secret <WEBHOOK_SECRET>  # set the GitHub secret
#   deploy/13_webhook_listener.sh --port 8080             # listener port (default 8080)
#   deploy/13_webhook_listener.sh --status                # show service + SG port
#
# The webhook secret is read from (in order): --secret, config.yaml
# (github.webhook_secret / github.webhook_secret_env), or an env var named by
# github.webhook_secret_env. If none is set the service is installed but will
# reject all POSTs until a secret is provided (fail-closed).
source "$(dirname "$0")/lib.sh"

PORT=8080
SECRET=""
DOSTATUS=0
while [ $# -gt 0 ]; do
  case "$1" in
    --secret) SECRET="$2"; shift 2 ;;
    --port)  PORT="$2"; shift 2 ;;
    --status) DOSTATUS=1; shift ;;
    *) log "unknown arg: $1"; exit 2 ;;
  esac
done

# resolve the webhook secret from config.yaml if not passed on the CLI
if [ -z "$SECRET" ]; then
  GITHUB_CFG="$(python3 - "$HERE/config.yaml" <<'PY' 2>/dev/null || true
import sys, yaml
c = yaml.safe_load(open(sys.argv[1])) or {}
g = c.get("github") or {}
s = g.get("webhook_secret")
if not s:
    env = g.get("webhook_secret_env")
    if env:
        import os
        s = os.environ.get(env)
print(s or "")
PY
)"
  SECRET="$GITHUB_CFG"
fi

: "${CODER_SERVER_IP:?CODER_SERVER_IP not in deploy/state.env -- run deploy/11_coder_server.sh first}"
: "${CODER_SG_ID:?CODER_SG_ID not in deploy/state.env -- run deploy/11_coder_server.sh first}"
: "${CODER_INSTANCE_ID:?CODER_INSTANCE_ID not in deploy/state.env}"

# ---------------------------------------------------------------------------
# 1. NO public port: the listener binds 127.0.0.1 only. Caddy (deploy/14) fronts
#    it on :443, routing /webhook -> 127.0.0.1:$PORT and restricting that route to
#    GitHub's hook IPs. So we do NOT open $PORT on the SG -- only 443 (done by 14).
# ---------------------------------------------------------------------------
put_state CODER_WEBHOOK_PORT "$PORT"

# ---------------------------------------------------------------------------
# 2. install the listener + launcher + repo on the Coder server over SSH.
#    The server's SG allows 22 from the caller's IP (opened by 11). We use the
#    default ubuntu user; the user-data in 11 runs as root but the instance is
#    ubuntu 24.04 canonical -> default user is 'ubuntu'.
# ---------------------------------------------------------------------------
SSH_TARGET="${CODER_SSH_USER:-ubuntu}@${CODER_SERVER_IP}"
SSH_OPTS=(-o StrictHostKeyChecking=accept-new -o ConnectTimeout=15)

# wait for SSH (the box may still be booting if 11 just ran)
log "waiting for SSH to $SSH_TARGET ..."
for i in $(seq 1 30); do
  if ssh "${SSH_OPTS[@]}" "$SSH_TARGET" "true" 2>/dev/null; then break; fi
  sleep 5
done

# ship the repo (slim: just scripts/ + the backend + config.yaml's non-secret
# bits the launcher needs). The launcher reads CODER_URL/CODER_SESSION_TOKEN/
# AWS from the unit EnvironmentFile, NOT from config.yaml, so we do not ship
# secrets in the repo tarball.
log "shipping repo + listener to the Coder server ..."
REMOTE_DIR="/opt/odoo-synth-coder"
ssh "${SSH_OPTS[@]}" "$SSH_TARGET" "sudo mkdir -p \"$REMOTE_DIR\" && sudo chown -R \"\$USER:\$USER\" \"$REMOTE_DIR\""
# rsync if available, else tar over ssh. CRITICAL: ship only the CODE, never
# the runtime stores -- the Coder server holds its own envs.yaml / profiles /
# runs (live per-issue workspace linkage + secret ARNs). Syncing them from a
# dev VM would clobber the server's records (e.g. an env a webhook created
# since the last deploy). Exclude them defensively even though lib/***
# is included.
if command -v rsync >/dev/null 2>&1; then
  # rsync filter rules are first-match-wins, so the runtime-store excludes MUST
  # come BEFORE the broad `lib/***` include -- otherwise envs.yaml etc.
  # match the include first and get shipped, clobbering the server's live per-issue
  # env linkage (this exact bug silently wiped the 493/495 env records on a
  # previous deploy). Excludes first; then include the rest of the code tree.
  rsync -az --delete \
    --exclude='lib/backend/envs.yaml' \
    --exclude='lib/backend/profiles/' \
    --exclude='lib/backend/runs.yaml' \
    --exclude='lib/backend/__pycache__/' \
    --exclude='lib/.venv/' \
    --exclude='__pycache__' \
    --include='scripts/' --include='scripts/***' \
    --include='lib/' --include='lib/***' \
    --include='deploy/' --include='deploy/_yaml_to_env.py' --include='deploy/lib.sh' \
    --exclude='deploy/state.env' \
    --include='coder/templates/odoo-synth-workspacer/agent-system-prompt.md' \
    --include='coder/templates/odoo-synth-workspacer/' \
    --exclude='*' \
    "$HERE/" "$SSH_TARGET:$REMOTE_DIR/"
else
  tar -czf - -C "$HERE" scripts lib \
    --exclude='lib/backend/envs.yaml' \
    --exclude='lib/backend/profiles' \
    --exclude='lib/backend/runs.yaml' \
    deploy/_yaml_to_env.py deploy/lib.sh \
    coder/templates/odoo-synth-workspacer/agent-system-prompt.md \
    | ssh "${SSH_OPTS[@]}" "$SSH_TARGET" "tar -xzf - -C $REMOTE_DIR"
fi

# install python deps (flask) + write the EnvironmentFile + systemd unit
log "installing python deps + systemd unit ..."
SECRET_ENV=""
[ -n "$SECRET" ] && SECRET_ENV="GITHUB_WEBHOOK_SECRET=$SECRET"
ssh "${SSH_OPTS[@]}" "$SSH_TARGET" "bash -s" -- "$REMOTE_DIR" "$PORT" "$SECRET_ENV" <<'REMOTE'
set -euo pipefail
REMOTE_DIR="$1"; PORT="$2"; SECRET_ENV="$3"
export DEBIAN_FRONTEND=noninteractive
sudo apt-get update -qq
sudo apt-get install -y -qq python3 python3-venv >/dev/null
# use a venv (Ubuntu 24.04 PEP 668 blocks system-wide pip). The launcher needs
# boto3/pyyaml (S3 profile/run stores); the listener needs flask.
PYBIN="$REMOTE_DIR/.venv/bin/python3"
if [ ! -x "$PYBIN" ]; then
  python3 -m venv "$REMOTE_DIR/.venv"
fi
"$REMOTE_DIR/.venv/bin/pip" install -q --upgrade pip >/dev/null 2>&1 || true
"$REMOTE_DIR/.venv/bin/pip" install -q flask boto3 pyyaml

# Coder server already runs the coder server daemon with CODER_* in
# /etc/coder/coder.env. Reuse those for the launcher, plus the webhook
# secret + port.
sudo install -d -m 700 /etc/odoo-synth
{
  printf 'WEBHOOK_PORT=%s\n' "$PORT"
  printf 'WEBHOOK_BIND=127.0.0.1\n'
  if [ -n "$SECRET_ENV" ]; then printf '%s\n' "$SECRET_ENV"; fi
} | sudo tee /etc/odoo-synth/webhook.env >/dev/null
sudo chmod 600 /etc/odoo-synth/webhook.env

# CODER_URL/CODER_SESSION_TOKEN: source from the coder server env if present,
# else leave for the launcher to resolve.
if [ -f /etc/coder/coder.env ]; then
  grep -E '^CODER_ACCESS_URL=' /etc/coder/coder.env 2>/dev/null \
    | sed 's/CODER_ACCESS_URL=/CODER_URL=/' | sudo tee -a /etc/odoo-synth/webhook.env >/dev/null || true
fi

sudo tee /etc/systemd/system/odoo-synth-webhook.service >/dev/null <<UNIT
[Unit]
Description=odoo-synth GitHub webhook listener (issue -> env + agent)
After=network-online.target coder-server.service
Wants=network-online.target
[Service]
Type=simple
User=root
WorkingDirectory=$REMOTE_DIR
EnvironmentFile=/etc/odoo-synth/webhook.env
ExecStart=$REMOTE_DIR/.venv/bin/python3 $REMOTE_DIR/scripts/webhook_listener.py
Restart=always
RestartSec=5
[Install]
WantedBy=multi-user.target
UNIT
sudo systemctl daemon-reload
sudo systemctl enable --now odoo-synth-webhook
sudo systemctl restart odoo-synth-webhook
REMOTE

log "webhook listener installed (localhost only: 127.0.0.1:$PORT) -- Caddy fronts it."
log "run deploy/14_caddy_https.sh (or re-run) to add the /webhook -> 127.0.0.1:$PORT route."
log "GitHub repo (the addons repo) -> Settings -> Webhooks -> Add webhook:"
log "  Payload URL: https://coder.$CODER_SERVER_IP.nip.io/webhook"
log "  Content type: application/json"
log "  Events: Issues ; Secret: <same as GITHUB_WEBHOOK_SECRET>"

if [ "$DOSTATUS" = 1 ]; then
  ssh "${SSH_OPTS[@]}" "$SSH_TARGET" "systemctl --no-pager status odoo-synth-webhook" 2>&1 || true
fi
log "done."
