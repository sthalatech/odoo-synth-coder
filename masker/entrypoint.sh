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

# ---- configurable knobs (all overridable via env; nothing hardcoded) ----
# masking profile: /work/profiles/<MASK_PROFILE>.yml (falls back to the legacy
# baked template for backward-compat).
MASK_PROFILE="${MASK_PROFILE:-odoo-core-pii}"
export GM_JOBS="${GM_JOBS:-4}"
# neutralize toggles (true|false) — post-mask hygiene on the masked replica.
NEUTRALIZE_MAIL="${NEUTRALIZE_MAIL:-true}"
NEUTRALIZE_FETCHMAIL="${NEUTRALIZE_FETCHMAIL:-true}"
NEUTRALIZE_PAYMENT="${NEUTRALIZE_PAYMENT:-true}"
NEUTRALIZE_SMTP_PARAM="${NEUTRALIZE_SMTP_PARAM:-true}"
# reset the admin login string to 'admin' (in addition to the password).
RESET_ADMIN_LOGIN="${RESET_ADMIN_LOGIN:-true}"

say(){ echo "[masker] $*"; }
is_true(){ case "${1,,}" in true|1|yes|on) return 0;; *) return 1;; esac; }
rm -rf "$GM_STORAGE"; mkdir -p "$GM_STORAGE"

# 0a. optional SSH tunnel to reach the SOURCE DB through a bastion.
#     When SSH_ENABLED=true, open  localhost:LOCAL -> SOURCE_DB_HOST:SOURCE_DB_PORT
#     via  SSH_BASTION_USER@SSH_BASTION_HOST:SSH_BASTION_PORT  using SSH_PRIVATE_KEY,
#     then rewrite SOURCE_DB_HOST/PORT to the local end so everything downstream
#     (preflight, greenmask) connects through the tunnel transparently.
SSH_ENABLED="${SSH_ENABLED:-false}"
if is_true "$SSH_ENABLED"; then
  : "${SSH_BASTION_HOST:?SSH_ENABLED but SSH_BASTION_HOST unset}"
  : "${SSH_BASTION_USER:?SSH_ENABLED but SSH_BASTION_USER unset}"
  : "${SSH_PRIVATE_KEY:?SSH_ENABLED but SSH_PRIVATE_KEY unset}"
  SSH_BASTION_PORT="${SSH_BASTION_PORT:-22}"
  SSH_LOCAL_PORT="${SSH_LOCAL_PORT:-15432}"
  REMOTE_HOST="$SOURCE_DB_HOST"; REMOTE_PORT="$SOURCE_DB_PORT"
  KEY=/tmp/ssh_key
  # accept keys pasted with literal "\n" as well as real newlines
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
    echo "[masker] SSH TUNNEL FAILED to ${SSH_BASTION_USER}@${SSH_BASTION_HOST}:${SSH_BASTION_PORT}" >&2
    sed 's/^/[masker]   ssh: /' /tmp/ssh.err >&2 || true
    echo "[masker] hint: check the bastion host/user/port, the SSH key, and that the bastion can reach ${REMOTE_HOST}:${REMOTE_PORT}." >&2
    exit 4
  fi
  export SOURCE_DB_HOST="127.0.0.1"; export SOURCE_DB_PORT="$SSH_LOCAL_PORT"
  say "SSH tunnel up; SOURCE now via 127.0.0.1:${SSH_LOCAL_PORT}."
fi

# 0b. pre-flight reachability checks (fail fast with a clear message instead of a
#    cryptic pg_dump/greenmask error deep into the run).
preflight(){ # role host port user pass db
  local role="$1" host="$2" port="$3" user="$4" pass="$5" db="$6"
  say "preflight: checking ${role} ${user}@${host}:${port}/${db} ..."
  if ! PGCONNECT_TIMEOUT=10 PGPASSWORD="$pass" \
       psql -h "$host" -p "$port" -U "$user" -d "$db" -tAc 'SELECT 1' >/dev/null 2>/tmp/pf.err; then
    echo "[masker] PREFLIGHT FAILED for ${role}: cannot connect to ${host}:${port}/${db} as ${user}" >&2
    sed 's/^/[masker]   psql: /' /tmp/pf.err >&2 || true
    echo "[masker] hint: verify the URL/credentials and that the DB is reachable from this task's network (security group / VPC egress / firewall)." >&2
    return 1
  fi
  say "preflight: ${role} reachable."
}
preflight "SOURCE" "$SOURCE_DB_HOST" "$SOURCE_DB_PORT" "$SOURCE_DB_USER" "$SOURCE_DB_PASSWORD" "$SOURCE_DB_NAME" || exit 3
# target: connect to the admin 'postgres' db (target DB itself is (re)created later)
preflight "TARGET" "$TARGET_DB_HOST" "$TARGET_DB_PORT" "$TARGET_DB_USER" "$TARGET_DB_PASSWORD" "postgres" || exit 3

# 1. render greenmask config from the selected profile
export SOURCE_DB_HOST SOURCE_DB_PORT SOURCE_DB_USER SOURCE_DB_PASSWORD SOURCE_DB_NAME GM_STORAGE
PROFILE_FILE="/work/profiles/${MASK_PROFILE}.yml"
[ -f "$PROFILE_FILE" ] || PROFILE_FILE="/work/greenmask.tmpl.yml"
say "using masking profile: ${MASK_PROFILE} (${PROFILE_FILE}); jobs=${GM_JOBS}"
envsubst < "$PROFILE_FILE" > /tmp/greenmask.yml
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
  --no-owner --no-privileges --jobs "${GM_JOBS}"
set -e
PSQL_T="psql -v ON_ERROR_STOP=1 -h ${TARGET_DB_HOST} -p ${TARGET_DB_PORT} -U ${TARGET_DB_USER} -d ${TARGET_DB_NAME}"
GOT="$($PSQL_T -A -t -c "SELECT to_regclass('public.res_partner');" || true)"
[ "$GOT" = "res_partner" ] || { echo "[masker] ERROR restore verification failed"; exit 1; }
say "restore verified (res_partner present)."

# 5. neutralize (guard each table: modules like fetchmail/payment may be absent)
#    each step is individually toggleable via NEUTRALIZE_* env vars.
say "neutralizing (mail=${NEUTRALIZE_MAIL} fetchmail=${NEUTRALIZE_FETCHMAIL} payment=${NEUTRALIZE_PAYMENT} smtp_param=${NEUTRALIZE_SMTP_PARAM}) ..."
export N_MAIL N_FETCH N_PAY N_SMTP
is_true "$NEUTRALIZE_MAIL"      && N_MAIL=1  || N_MAIL=0
is_true "$NEUTRALIZE_FETCHMAIL" && N_FETCH=1 || N_FETCH=0
is_true "$NEUTRALIZE_PAYMENT"   && N_PAY=1   || N_PAY=0
is_true "$NEUTRALIZE_SMTP_PARAM" && N_SMTP=1 || N_SMTP=0
$PSQL_T <<SQL
DO \$\$ BEGIN
  IF ${N_MAIL} = 1 AND to_regclass('public.ir_mail_server') IS NOT NULL THEN
    UPDATE ir_mail_server SET active=false, smtp_host=NULL, smtp_user=NULL, smtp_pass=NULL;
  END IF;
  IF ${N_FETCH} = 1 AND to_regclass('public.fetchmail_server') IS NOT NULL THEN
    EXECUTE 'UPDATE fetchmail_server SET active=false, password=NULL, "user"=NULL';
  END IF;
  IF ${N_PAY} = 1 AND to_regclass('public.payment_provider') IS NOT NULL THEN
    UPDATE payment_provider SET state='disabled';
  END IF;
  IF ${N_SMTP} = 1 AND to_regclass('public.ir_config_parameter') IS NOT NULL THEN
    UPDATE ir_config_parameter SET value='0' WHERE key='mail.force.smtp.from' AND value IS NOT NULL;
  END IF;
END \$\$;
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
if is_true "$RESET_ADMIN_LOGIN"; then
  $PSQL_T -c "UPDATE res_users SET password='${HASH}', login='admin' WHERE id=${UID_ADMIN};"
  say "admin uid ${UID_ADMIN} password + login('admin') set."
else
  $PSQL_T -c "UPDATE res_users SET password='${HASH}' WHERE id=${UID_ADMIN};"
  say "admin uid ${UID_ADMIN} password set (login unchanged)."
fi

# 7. (optional) produce a downloadable pg_dump of the masked DB and upload it to
#    the presigned S3 URL provided by the control panel (MASKED_DUMP_PUT_URL).
if [ -n "${MASKED_DUMP_PUT_URL:-}" ]; then
  say "producing downloadable pg_dump of masked DB ${TARGET_DB_NAME} ..."
  DUMP_FILE="/tmp/masked.dump"
  pg_dump -Fc --no-owner --no-privileges \
    -h "${TARGET_DB_HOST}" -p "${TARGET_DB_PORT}" -U "${TARGET_DB_USER}" \
    -d "${TARGET_DB_NAME}" -f "$DUMP_FILE"
  SZ="$(stat -c%s "$DUMP_FILE" 2>/dev/null || echo '?')"
  say "uploading masked dump (${SZ} bytes) to S3 ..."
  curl -fsS -X PUT -T "$DUMP_FILE" "${MASKED_DUMP_PUT_URL}" \
    && say "masked dump uploaded; download link is available in the control panel." \
    || { echo "[masker] ERROR masked dump upload failed"; exit 1; }
  rm -f "$DUMP_FILE"
fi

say "DONE: masked replica ready in ${TARGET_DB_NAME}@${TARGET_DB_HOST}"
