#!/usr/bin/env bash
# Render odoo.conf from env and launch Odoo FROM SOURCE (odoo-bin) against the DB.
set -euo pipefail

: "${TARGET_DB_HOST:?}" "${TARGET_DB_NAME:?}" "${TARGET_DB_USER:?}" "${TARGET_DB_PASSWORD:?}"
TARGET_DB_PORT="${TARGET_DB_PORT:-5432}"
ODOO_MASTER_PASSWORD="${ODOO_MASTER_PASSWORD:-change_me_master}"

SRC="/opt/odoo-src"
# Source addons: core (odoo/addons) + enterprise-style (addons) from the checkout.
ADDONS="/mnt/extra-addons-custom"
for d in "$SRC/odoo/addons" "$SRC/addons" /mnt/extra-addons; do
  [ -d "$d" ] && ADDONS="${ADDONS},${d}"
done

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

COMMIT="$(cat /opt/odoo-src.commit 2>/dev/null || echo unknown)"
echo "[odoo] source commit ${COMMIT}"
echo "[odoo] launching against ${TARGET_DB_NAME}@${TARGET_DB_HOST}; addons_path=${ADDONS}"
exec python3 "$SRC/odoo-bin" -c /etc/odoo/odoo.conf
