#!/usr/bin/env bash
# discovery orchestration: optionally open an SSH tunnel to the SOURCE DB, then
# run the discovery worker which inspects the live DB + addons repo and uploads
# discovery.json to the presigned S3 URL.
set -euo pipefail

: "${SOURCE_DB_HOST:?}" "${SOURCE_DB_NAME:?}" "${SOURCE_DB_USER:?}"
export SOURCE_DB_PORT="${SOURCE_DB_PORT:-5432}"
export SOURCE_DB_PASSWORD="${SOURCE_DB_PASSWORD:-}"

say(){ echo "[discover] $*"; }
is_true(){ case "${1,,}" in true|1|yes|on) return 0;; *) return 1;; esac; }

SSH_ENABLED="${SSH_ENABLED:-false}"
if is_true "$SSH_ENABLED"; then
  : "${SSH_BASTION_HOST:?SSH_ENABLED but SSH_BASTION_HOST unset}"
  : "${SSH_BASTION_USER:?SSH_ENABLED but SSH_BASTION_USER unset}"
  : "${SSH_PRIVATE_KEY:?SSH_ENABLED but SSH_PRIVATE_KEY unset}"
  SSH_BASTION_PORT="${SSH_BASTION_PORT:-22}"
  SSH_LOCAL_PORT="${SSH_LOCAL_PORT:-15432}"
  REMOTE_HOST="$SOURCE_DB_HOST"; REMOTE_PORT="$SOURCE_DB_PORT"
  KEY=/tmp/ssh_key
  printf '%b\n' "$SSH_PRIVATE_KEY" | sed 's/\\n/\n/g' > "$KEY"
  chmod 600 "$KEY"
  say "opening SSH tunnel: localhost:${SSH_LOCAL_PORT} -> ${REMOTE_HOST}:${REMOTE_PORT} via ${SSH_BASTION_USER}@${SSH_BASTION_HOST}:${SSH_BASTION_PORT} ..."
  if ! ssh -f -N \
        -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
        -o ExitOnForwardFailure=yes -o ConnectTimeout=15 \
        -o ServerAliveInterval=30 -o ServerAliveCountMax=3 \
        -i "$KEY" -p "$SSH_BASTION_PORT" \
        -L "127.0.0.1:${SSH_LOCAL_PORT}:${REMOTE_HOST}:${REMOTE_PORT}" \
        "${SSH_BASTION_USER}@${SSH_BASTION_HOST}" 2>/tmp/ssh.err; then
    echo "[discover] SSH TUNNEL FAILED to ${SSH_BASTION_USER}@${SSH_BASTION_HOST}:${SSH_BASTION_PORT}" >&2
    sed 's/^/[discover]   ssh: /' /tmp/ssh.err >&2 || true
    exit 4
  fi
  export SOURCE_DB_HOST="127.0.0.1"; export SOURCE_DB_PORT="$SSH_LOCAL_PORT"
  say "SSH tunnel up; SOURCE now via 127.0.0.1:${SSH_LOCAL_PORT}."
fi

exec python3 /work/discover.py
