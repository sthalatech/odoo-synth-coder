#!/usr/bin/env bash
# masker orchestration: dump(mask) SOURCE -> restore into TARGET RDS ->
# neutralize -> set admin password. Idempotent on the target DB.
set -euo pipefail

: "${SOURCE_DB_HOST:?}" "${SOURCE_DB_NAME:?}" "${SOURCE_DB_USER:?}"
: "${TARGET_DB_HOST:?}" "${TARGET_DB_NAME:?}" "${TARGET_DB_USER:?}" "${TARGET_DB_PASSWORD:?}"
export SOURCE_DB_PORT="${SOURCE_DB_PORT:-5432}"
export TARGET_DB_PORT="${TARGET_DB_PORT:-5432}"
export SOURCE_DB_PASSWORD="${SOURCE_DB_PASSWORD:-}"
ODOO_ADMIN_PASSWORD="${ODOO_ADMIN_PASSWORD:-admin}"
export GM_STORAGE="/tmp/gm_storage"

say(){ echo "[masker] $*"; }
rm -rf "$GM_STORAGE"; mkdir -p "$GM_STORAGE"

# 1. render greenmask config
export SOURCE_DB_HOST SOURCE_DB_PORT SOURCE_DB_USER SOURCE_DB_PASSWORD SOURCE_DB_NAME GM_STORAGE
envsubst < /work/greenmask.tmpl.yml > /tmp/greenmask.yml
say "rendered config:"; sed 's/password=[^ ]*/password=***/' /tmp/greenmask.yml

# 2. masked dump from source
say "dumping + masking source ${SOURCE_DB_NAME}@${SOURCE_DB_HOST} ..."
greenmask --config /tmp/greenmask.yml dump
say "dump done."

# 3. (re)create target DB on RDS
export PGPASSWORD="$TARGET_DB_PASSWORD"
PSQL_ADMIN="psql -v ON_ERROR_STOP=1 -h ${TARGET_DB_HOST} -p ${TARGET_DB_PORT} -U ${TARGET_DB_USER} -d postgres"
say "recreating target database ${TARGET_DB_NAME} ..."
$PSQL_ADMIN -c "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname='${TARGET_DB_NAME}' AND pid<>pg_backend_pid();" >/dev/null 2>&1 || true
$PSQL_ADMIN -c "DROP DATABASE IF EXISTS \"${TARGET_DB_NAME}\";"
$PSQL_ADMIN -c "CREATE DATABASE \"${TARGET_DB_NAME}\" ENCODING 'UTF8' TEMPLATE template0;"
$PSQL_ADMIN -c "ALTER DATABASE \"${TARGET_DB_NAME}\" SET search_path TO public;"

# 4. restore masked dump into target (no -C: we pre-created with our name)
say "restoring masked dump into ${TARGET_DB_NAME} ..."
set +e
greenmask --config /tmp/greenmask.yml restore latest \
  -h "${TARGET_DB_HOST}" -p "${TARGET_DB_PORT}" -U "${TARGET_DB_USER}" -d "${TARGET_DB_NAME}" \
  --no-owner --no-privileges --jobs 4
set -e
PSQL_T="psql -v ON_ERROR_STOP=1 -h ${TARGET_DB_HOST} -p ${TARGET_DB_PORT} -U ${TARGET_DB_USER} -d ${TARGET_DB_NAME}"
GOT="$($PSQL_T -A -t -c "SELECT to_regclass('public.res_partner');" || true)"
[ "$GOT" = "res_partner" ] || { echo "[masker] ERROR restore verification failed"; exit 1; }
say "restore verified (res_partner present)."

# 5. neutralize (guard each table: modules like fetchmail/payment may be absent)
say "neutralizing ..."
$PSQL_T <<'SQL'
DO $$ BEGIN
  IF to_regclass('public.ir_mail_server') IS NOT NULL THEN
    UPDATE ir_mail_server SET active=false, smtp_host=NULL, smtp_user=NULL, smtp_pass=NULL;
  END IF;
  IF to_regclass('public.fetchmail_server') IS NOT NULL THEN
    EXECUTE 'UPDATE fetchmail_server SET active=false, password=NULL, "user"=NULL';
  END IF;
  IF to_regclass('public.payment_provider') IS NOT NULL THEN
    UPDATE payment_provider SET state='disabled';
  END IF;
  IF to_regclass('public.ir_config_parameter') IS NOT NULL THEN
    UPDATE ir_config_parameter SET value='0' WHERE key='mail.force.smtp.from' AND value IS NOT NULL;
  END IF;
END $$;
SQL

# 6. set admin password (pbkdf2-sha512, Odoo passlib scheme)
say "setting admin password ..."
HASH="$(python3 - "$ODOO_ADMIN_PASSWORD" <<'PY'
import sys
from passlib.context import CryptContext
print(CryptContext(schemes=["pbkdf2_sha512"]).hash(sys.argv[1]), end="")
PY
)"
UID_ADMIN="$($PSQL_T -A -t -c "SELECT res_id FROM ir_model_data WHERE module='base' AND name='user_admin' LIMIT 1;" || true)"
[ -n "$UID_ADMIN" ] || UID_ADMIN=2
$PSQL_T -c "UPDATE res_users SET password='${HASH}', login='admin' WHERE id=${UID_ADMIN};"
say "admin uid ${UID_ADMIN} password + login('admin') set."

say "DONE: masked replica ready in ${TARGET_DB_NAME}@${TARGET_DB_HOST}"
