#!/usr/bin/env bash
# Manage Coder users on the odoo-synth Coder server.
#
# Coder is the auth + access boundary for developer environments:
#   - the server uses built-in password auth (no --auth flag => 'password')
#   - each workspace's apps default to sharing_level=owner (DB default), so
#     ONLY the workspace's owner (and admins) can open them -- no extra config
#   - a regular user (org member) can create their own workspaces from the
#     org's templates (odoo-synth-env / builder / runner) and only ever see
#     their own workspaces' apps
#   - sharing is OPT-IN per app, done by the owner from the Coder dashboard:
#     open the workspace -> click an app -> Share -> authenticated|organization|public
#
# Usage:
#   deploy/coder_users.sh add    <email> [password]   # create a user
#   deploy/coder_users.sh list                        # list users
#   deploy/coder_users.sh roles   <email> [roles...]  # e.g. roles alice@x.com member
#
# Requires: coder CLI logged in as admin (CODER_URL + CODER_SESSION_TOKEN in
# config.yaml, or `coder login <url>` interactively).
set -euo pipefail
cd "$(dirname "$0")/.."
# Config: config.yaml is the single source of truth. deploy/lib.sh loads it and
# exports CODER_URL / CODER_SESSION_TOKEN etc. (also re-exports AWS creds).
# shellcheck disable=SC1091
source deploy/lib.sh

cmd="${1:-help}"
shift || true

case "$cmd" in
  add)
    email="${1:?usage: add <email> [password]}"
    pw="${2:-}"
    args=(users create -e "$email")
    [ -n "$pw" ] && args+=(-p "$pw")
    coder "${args[@]}"
    echo "=> created $email (member, default org). They can now log in and"
    echo "   create their own workspaces. Their apps are owner-private by"
    echo "   default; they opt into sharing from the Coder dashboard."
    ;;
  list)
    coder users list
    ;;
  roles)
    email="${1:?usage: roles <email> [roles...]}"
    shift
    coder users edit-roles "$email" --roles "${@:-member}"
    ;;
  help|*)
    sed -n '2,/^$/p' "$0" | sed 's/^# \{0,1\}//'
    ;;
esac
