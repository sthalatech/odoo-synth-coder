#!/usr/bin/env bash
# Render odoo.conf from env and launch Odoo FROM SOURCE (odoo-bin) against the DB.
set -euo pipefail

: "${TARGET_DB_HOST:?}" "${TARGET_DB_NAME:?}" "${TARGET_DB_USER:?}" "${TARGET_DB_PASSWORD:?}"
TARGET_DB_PORT="${TARGET_DB_PORT:-5432}"
ODOO_MASTER_PASSWORD="${ODOO_MASTER_PASSWORD:-change_me_master}"

SRC="/opt/odoo-src"
# addons_path priority (Odoo: first match wins, so earlier = overrides later):
#   EXTRA_ADDONS_PATH (EFS live-dev) > git custom > local custom > enterprise
#   > community > core > legacy /mnt/extra-addons.
ADDONS=""
# A directory is an "addons dir" if it directly contains a module
# (child/__manifest__.py). add() registers DIR if it is one; otherwise it scans
# one level down and registers each nested addons dir (handles repo layouts like
# custom_addons/ + third_party_addons/).
is_addons_dir(){ compgen -G "$1/*/__manifest__.py" >/dev/null 2>&1; }
add(){ local d="$1"
  [ -d "$d" ] || return 0
  if is_addons_dir "$d"; then ADDONS="${ADDONS:+$ADDONS,}$d"; return 0; fi
  local c; for c in "$d"/*/; do c="${c%/}"; is_addons_dir "$c" && ADDONS="${ADDONS:+$ADDONS,}$c"; done
  return 0
}
add /mnt/extra-addons-custom      # custom addons from git (nested layouts ok)
add /opt/custom                   # custom modules baked from build context
add /opt/enterprise               # enterprise modules baked from zip/dir
add "$SRC/addons"                 # community
add "$SRC/odoo/addons"            # core
add /mnt/extra-addons             # legacy mount point
# Live-dev override: an EFS/volume mount takes highest priority when provided.
[ -n "${EXTRA_ADDONS_PATH:-}" ] && ADDONS="${EXTRA_ADDONS_PATH},${ADDONS}"

cat > /etc/odoo/odoo.conf <<CONF
[options]
admin_passwd = ${ODOO_MASTER_PASSWORD}
addons_path = ${ADDONS}
db_host = ${TARGET_DB_HOST}
db_port = ${TARGET_DB_PORT}
db_user = ${TARGET_DB_USER}
db_password = ${TARGET_DB_PASSWORD}
db_name = ${TARGET_DB_NAME}
dbfilter = ^${TARGET_DB_NAME}$
list_db = False
proxy_mode = True
CONF

# Per-profile extra odoo.conf options (base64-encoded newline-delimited
# key = value lines). Some source deployments read custom config keys from
# odoo.conf (e.g. an SSO addon doing config['sso_api_secret']); without the key
# defined, Odoo raises KeyError and the page 500s. Injecting a key WITH A VALUE
# lets those addons load in the masked dev replica.
#
# IMPORTANT: lines with an empty value (``key =``) are STRIPPED here. Odoo's
# config parser type-checks every option (int/bool/string), and an empty string
# fails for typed options -- e.g. ``limit_time_real_cron =`` aborts startup
# with ``invalid integer value: ''`` before any addon runs. An empty placeholder
# never helped addons anyway: addons read config via ``config['key']`` (a dict
# lookup that returns None/default if the key is absent), so omitting an empty
# key is equivalent to present-but-empty for addon code, and it keeps Odoo
# booting. Discovery seeds these placeholders; the generator + this filter drop
# them at render time.
if [ -n "${ODOO_CONF_EXTRA_B64:-}" ]; then
  echo "[odoo] appending profile odoo.conf extras"
  # Decode + drop lines whose value is empty (``key =`` with nothing after =).
  # Odoo's config parser type-checks every option, and an empty string fails
  # for int/bool options (e.g. ``limit_time_real_cron =`` -> crash). Keep
  # comments, blank lines, and any key with a non-empty value. Use sed (not
  # awk word-splitting) so values containing spaces / = survive intact.
  printf '%s' "${ODOO_CONF_EXTRA_B64}" | base64 -d 2>/dev/null \
    | sed -E '/^[[:space:]]*[A-Za-z0-9_.]+[[:space:]]*=[[:space:]]*$/d' \
    >> /etc/odoo/odoo.conf 2>/dev/null \
    || echo "[odoo] WARN: could not decode ODOO_CONF_EXTRA_B64"
fi

COMMIT="$(cat /opt/odoo-src.commit 2>/dev/null || echo unknown)"
echo "[odoo] source commit ${COMMIT}"
echo "[odoo] launching against ${TARGET_DB_NAME}@${TARGET_DB_HOST}; addons_path=${ADDONS}"
# Extra args ("$@") are forwarded to odoo-bin. With none, this starts the HTTP
# server as usual; passing e.g. `-u all --stop-after-init` runs a one-off schema
# migration/update against the same DB + addons_path (used after restoring an
# older prod dump into newer source code).
exec python3 "$SRC/odoo-bin" -c /etc/odoo/odoo.conf "$@"
