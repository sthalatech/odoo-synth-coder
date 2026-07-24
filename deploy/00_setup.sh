#!/usr/bin/env bash
# 00_setup.sh -- guided first-time setup for a community user.
#
# Walks you through every step: install prerequisites, collect + configure AWS
# credentials, collect the values config.yaml needs, generate the DB/Odoo
# passwords, write config.yaml + a gitignored secrets.env, then validate +
# smoke-test the CLI.
#
# Safe to re-run: it offers to keep or overwrite your existing config. It never
# commits anything (config.yaml + secrets.env are gitignored).
#
# Usage:
#   bash deploy/00_setup.sh
set -euo pipefail
HERE="$(cd "$(dirname "$0")/.." && pwd)"
cd "$HERE"

# --- pretty helpers --------------------------------------------------------
BOLD=$'\033[1m'; DIM=$'\033[2m'; GREEN=$'\033[32m'; YELLOW=$'\033[33m'; RED=$'\033[31m'; CYAN=$'\033[36m'; OFF=$'\033[0m'
if [ ! -t 1 ]; then BOLD=""; DIM=""; GREEN=""; YELLOW=""; RED=""; CYAN=""; OFF=""; fi
say(){ echo "${BOLD}$*${OFF}"; }
step(){ echo; echo "${CYAN}${BOLD}[$1]${OFF} ${BOLD}$2${OFF}"; }
note(){ echo "${DIM}$*${OFF}"; }
ok(){ echo "${GREEN}✓${OFF} $*"; }
warn(){ echo "${YELLOW}!${OFF} $*"; }
die(){ echo "${RED}✗${OFF} $*" >&2; exit 1; }
# prompt_default  "question" "default"  -> echoes answer (default if empty)
prompt_default(){
  local q="$1" d="$2" v
  printf '%s%s%s [%s]: ' "${BOLD}" "$q" "${OFF}" "$d" >&2
  read -r v
  echo "${v:-$d}"
}
prompt_required(){  # question
  local q="$1" v
  while true; do
    printf '%s%s%s: ' "${BOLD}" "$q" "${OFF}" >&2
    read -r v
    [ -n "$v" ] && { echo "$v"; return; }
    warn "a value is required"
  done
}
prompt_secret(){  # question  (no echo, optional confirm for generated)
  local q="$1" v
  printf '%s%s%s: ' "${BOLD}" "$q" "${OFF}" >&2
  read -rs v; echo >&2
  echo "$v"
}
confirm(){  # question  -> 0=yes 1=no  (default no)
  local q="$1" v
  printf '%s%s%s [y/N]: ' "${BOLD}" "$q" "${OFF}" >&2
  read -r v
  case "$v" in y|Y|yes|YES) return 0;; *) return 1;; esac
}
have(){ command -v "$1" >/dev/null 2>&1; }

say "${BOLD}odoo-synth guided setup${OFF}"
note "This walks you through a first-time install: prerequisites, AWS auth,"
note "config.yaml, secrets, and a smoke test. Re-run anytime; existing config"
note "is kept unless you choose to overwrite."

# ===========================================================================
# 1. PREREQUISITES
# ===========================================================================
step 1/7 "Install prerequisites (AWS CLI, python deps, Coder CLI, odoo-synth on PATH)"
if confirm "Run deploy/00_install_prereqs.sh now?"; then
  bash deploy/00_install_prereqs.sh
else
  warn "skipped -- make sure aws, python3, coder, and odoo-synth are on PATH."
fi
have aws     || die "aws CLI missing -- run deploy/00_install_prereqs.sh"
have python3 || die "python3 missing -- run deploy/00_install_prereqs.sh"
have odoo-synth || die "odoo-synth not on PATH -- run deploy/00_install_prereqs.sh"
ok "prerequisites present"

# ===========================================================================
# 2. AWS AUTHENTICATION
# ===========================================================================
step 2/7 "Configure AWS credentials"
echo "  odoo-synth provisions AWS resources (ECR, EC2, S3, Coder server). You"
echo "  need an AWS account + an IAM access key with permission to create those."
echo "  Create one in the AWS console: IAM -> Users -> your user -> Security"
echo "  credentials -> Create access key. The wizard writes it to the standard"
echo "  shared-credentials file (~/.aws/credentials + ~/.aws/config) used by the"
echo "  AWS CLI -- nothing is committed to this repo."
echo

aws_ok=false
aws_account=""
# Detect existing credentials first (shared file OR env vars).
if aws sts get-caller-identity >/dev/null 2>&1; then
  aws_account="$(aws sts get-caller-identity --query Account --output text 2>/dev/null || echo '')"
  aws_region_cur="$(aws configure get region 2>/dev/null || aws configure get region 2>/dev/null || echo '')"
  ok "AWS credentials already configured (account ${aws_account:-?}, region ${aws_region_cur:-?})."
  if confirm "Re-enter different credentials?"; then
    aws_ok=false        # fall through to the guided collection below
  else
    aws_ok=true
  fi
fi

if [ "$aws_ok" = false ]; then
  if ! have aws; then
    die "aws CLI missing -- run step 1 (deploy/00_install_prereqs.sh) first."
  fi
  echo
  say "Enter your AWS access key"
  echo "  ${DIM}Access key id  : 20-char, starts with AKIA (e.g. AKIAIOSFODNN7EXAMPLE)${OFF}"
  echo "  ${DIM}Secret access key: 40-char. Both are shown once when you create the key.${OFF}"
  echo "  ${DIM}Type is hidden; paste carefully. Press Ctrl-C to abort.${OFF}"
  echo
  AKID="$(prompt_required "AWS Access Key ID")"
  SAK="$(prompt_secret  "AWS Secret Access Key")"
  [ -n "$AKID" ] && [ -n "$SAK" ] || die "both access key id and secret are required"
  # Region: the wizard asks again in step 4 as the config.yaml region, but we
  # need it now for sts to pick the right partition. Default from any existing
  # shared config, else us-east-1.
  REGION_DEFAULT="$(aws configure get region 2>/dev/null || echo us-east-1)"
  AWS_REGION_Q="$(prompt_default "AWS region for these credentials" "$REGION_DEFAULT")"

  # Write to the shared credentials/config files non-interactively.
  # `aws configure set` is idempotent and writes the right file per key.
  aws configure set aws_access_key_id     "$AKID"       # -> ~/.aws/credentials
  aws configure set aws_secret_access_key "$SAK"        # -> ~/.aws/credentials
  aws configure set region                "$AWS_REGION_Q"  # -> ~/.aws/config
  # scrub the values from this shell's variables + history so they don't linger
  unset AKID SAK
  export AWS_REGION="$AWS_REGION_Q"

  echo
  say "Verifying credentials..."
  if cid="$(aws sts get-caller-identity --query Account --output text 2>/dev/null)"; then
    aws_ok=true
    aws_account="$cid"
    ok "authenticated -- AWS account $cid, region $AWS_REGION_Q"
    arn="$(aws sts get-caller-identity --query Arn --output text 2>/dev/null || true)"
    [ -n "$arn" ] && note "identity: $arn"
  else
    warn "AWS could not authenticate with those credentials. Common causes:"
    warn "  - wrong secret key (re-create the access key in IAM and re-run)"
    warn "  - key belongs to a different region/partition (check the region)"
    warn "  - the IAM user lacks sts:GetCallerIdentity (very unlikely)."
    warn "You can continue filling config.yaml now and fix AWS auth before the"
    warn "deploy step. The validate + smoke steps below will report 'AWS: not'."
    aws_ok=false
  fi
  REGION_DEFAULT="$AWS_REGION_Q"
fi

if [ "$aws_ok" = false ] && [ -z "${AWS_ACCESS_KEY_ID:-}${AWS_SECRET_ACCESS_KEY:-}" ]; then
  # only warn if we genuinely have nothing
  if ! aws sts get-caller-identity >/dev/null 2>&1; then
    warn "AWS auth not confirmed. You can continue filling config.yaml now and"
    warn "re-run this wizard (or 'aws configure') before the deploy step."
    warn "The validate + smoke steps below will report 'AWS: not authenticated'."
  fi
fi
[ -z "${REGION_DEFAULT:-}" ] && REGION_DEFAULT="$(aws configure get region 2>/dev/null || echo us-east-1)"

# ===========================================================================
# 3. EXISTING CONFIG?
# ===========================================================================
step 3/7 "config.yaml"
CFG="$HERE/config.yaml"
if [ -f "$CFG" ]; then
  ok "config.yaml already exists."
  if ! confirm "Overwrite it with a fresh guided config?"; then
    note "keeping your existing config.yaml -- skipping to validation."
    SKIP_CONFIG=1
  else
    SKIP_CONFIG=0
  fi
else
  SKIP_CONFIG=0
fi

if [ "${SKIP_CONFIG:-0}" = 0 ]; then
# ---- collect values -------------------------------------------------------
step 4/7 "Collect configuration values"
echo "  ${DIM}Press Enter to accept the [default].${OFF}"
echo

REGION="$(prompt_default "AWS region" "$REGION_DEFAULT")"
PROJECT="$(prompt_default "Project name (AWS resources are named <project>-*)" "odoo-synth")"

echo
say "Odoo core source"
echo "  ${DIM}Pin to the Odoo commit your production DB was taken at, so the running${OFF}"
echo "  ${DIM}code's schema matches the restored dump without a -u all migration.${OFF}"
echo "  ${DIM}Find a commit sha on https://github.com/odoo/odoo (e.g. 17.0 branch).${OFF}"
ODOO_SERIES="$(prompt_default "Odoo series" "17.0")"
ODOO_GIT_REF="$(prompt_required "Odoo core git ref (commit sha or branch, e.g. 17.0)")"

echo
say "Custom addons repo"
echo "  ${DIM}The addons repo baked into the Odoo image + live-mounted in dev workspaces.${OFF}"
echo "  ${DIM}Leave blank if you run core modules only (enter 'none' to skip).${OFF}"
CUSTOM_URL="$(prompt_default "Custom addons git URL" "none")"
if [ "$CUSTOM_URL" = "none" ] || [ -z "$CUSTOM_URL" ]; then
  CUSTOM_URL=""
  CUSTOM_REF=""
else
  CUSTOM_REF="$(prompt_required "Custom addons git ref (commit sha or branch)")"
fi

echo
say "S3 bucket for masked dumps"
echo "  ${DIM}A bucket in your AWS account to hold masked pg_dump artifacts + uploaded${OFF}"
echo "  ${DIM}source dumps. Must be globally unique. We'll create it if it doesn't exist.${OFF}"
BUCKET="$(prompt_required "Dumps S3 bucket name (e.g. <project>-dumps-<suffix>)")"

# ---- generate / collect secrets -------------------------------------------
step 5/7 "Secrets (DB + Odoo passwords)"
echo "  ${DIM}Secrets are written to deploy/secrets.env (gitignored, chmod 600) and${OFF}"
echo "  ${DIM}read via the ref:env: form -- they never go into config.yaml.${OFF}"
echo "  ${DIM}You can let the wizard generate strong random passwords, or supply your own.${OFF}"
echo
gen_pw(){ python3 -c 'import secrets,string as s; print("".join(secrets.choice(s.ascii_letters+s.digits) for _ in range(24)))'; }

if confirm "Generate all four passwords automatically?"; then
  ODOO_ADMIN_PW="$(gen_pw)"; ODOO_MASTER_PW="$(gen_pw)"
  TARGET_DB_PW="$(gen_pw)";  SOURCE_DB_MASTER_PW="$(gen_pw)"
  ok "generated 4 random passwords"
else
  ODOO_ADMIN_PW="$(prompt_secret "Odoo admin password")"
  ODOO_MASTER_PW="$(prompt_secret "Odoo master (database) password")"
  TARGET_DB_PW="$(prompt_secret "Masked DB (target) password")"
  SOURCE_DB_MASTER_PW="$(prompt_secret "Source DB master password")"
fi

# ---- write secrets.env ----------------------------------------------------
SECRETS="$HERE/deploy/secrets.env"
{
  echo "# odoo-synth secrets -- generated by deploy/00_setup.sh"
  echo "# gitignored. source this file before running the CLI or deploy scripts:"
  echo "#     set -a; . deploy/secrets.env; set +a"
  echo "# (the ref:env: form in config.yaml reads these as env vars)"
  echo "ODOO_ADMIN_PASSWORD='$ODOO_ADMIN_PW'"
  echo "ODOO_MASTER_PASSWORD='$ODOO_MASTER_PW'"
  echo "TARGET_DB_PASSWORD='$TARGET_DB_PW'"
  echo "SOURCE_DB_MASTER_PASSWORD='$SOURCE_DB_MASTER_PW'"
} > "$SECRETS"
chmod 600 "$SECRETS"
ok "wrote $SECRETS (chmod 600)"
note "source it in your shell before running odoo-synth:"
note "    ${DIM}set -a; . deploy/secrets.env; set +a${OFF}"

# ---- write config.yaml ----------------------------------------------------
# Build it from the example, substituting only the collected non-secret values.
# Secrets stay as ref:env: (already in the example).
EXAMPLE="$HERE/config.example.yaml"
python3 - "$EXAMPLE" "$CFG" "$REGION" "$PROJECT" "$ODOO_SERIES" "$ODOO_GIT_REF" "$CUSTOM_URL" "$CUSTOM_REF" "$BUCKET" <<'PY'
import sys, re
ex, out, region, project, series, ref, curl, cref, bucket = sys.argv[1:10]
s = open(ex).read()
# \g<1> preserves the leading whitespace captured in group 1.
s = re.sub(r'(?m)^(\s*region:\s*).*',         r'\g<1>'+region,  s, count=1)
s = re.sub(r'(?m)^(\s*project:\s*).*',        r'\g<1>'+project, s, count=1)
s = re.sub(r'(?m)^(\s*series:\s*").*(")',     r'\g<1>'+series+r'\g<2>', s, count=1)
s = re.sub(r'(?m)^(\s*git_ref:\s*").*(")',    r'\g<1>'+ref+r'\g<2>',   s, count=1)
# custom addons: raw URL/ref in quotes (no <> wrapping -- that triggers the
# validate placeholder check). Empty string = core modules only.
s = re.sub(r'(?m)^(\s*custom_git_url:\s*).*', r'\g<1>'+('"'+curl+'"' if curl else '""'), s, count=1)
s = re.sub(r'(?m)^(\s*custom_git_ref:\s*).*', r'\g<1>'+('"'+cref+'"' if cref else '""'), s, count=1)
s = re.sub(r'(?m)^(\s*dumps_bucket:\s*).*',   r'\g<1>'+bucket, s, count=1)
open(out,'w').write(s)
print("wrote", out)
PY
ok "wrote $CFG"

# ---- create the S3 bucket if it doesn't exist (best-effort) ---------------
if [ "$aws_ok" = true ]; then
  if ! aws s3api head-bucket --bucket "$BUCKET" --region "$REGION" >/dev/null 2>&1; then
    if confirm "S3 bucket '$BUCKET' doesn't exist. Create it now?"; then
      if aws s3api create-bucket --bucket "$BUCKET" --region "$REGION" \
           --create-bucket-configuration "LocationConstraint=$REGION" >/dev/null 2>&1 \
           2>/dev/null || aws s3api create-bucket --bucket "$BUCKET" --region "$REGION" >/dev/null 2>&1; then
        ok "created bucket s3://$BUCKET"
      else
        warn "could not create bucket (region $REGION) -- create it manually:"
        warn "    aws s3api create-bucket --bucket $BUCKET --region $REGION"
      fi
    else
      warn "skipped bucket creation -- create it before running run_all.sh."
    fi
  else
    ok "bucket s3://$BUCKET already exists"
  fi
else
  warn "AWS not authenticated -- skipping bucket creation. Create it later:"
  warn "    aws s3api create-bucket --bucket $BUCKET --region $REGION"
fi

else  # SKIP_CONFIG
  BUCKET="$(python3 -c "import sys;sys.path.insert(0,'lib');from backend import config;print(config.get('DUMP_S3_BUCKET',''))" 2>/dev/null || echo '')"
  REGION="$(python3 -c "import sys;sys.path.insert(0,'lib');from backend import config;print(config.get('AWS_REGION',''))" 2>/dev/null || echo '')"
  step 4/7 "Collect configuration values (skipped -- keeping existing config.yaml)"
  step 5/7 "Secrets (skipped -- keeping existing)"
fi

# ===========================================================================
# 6. VALIDATE + SMOKE
# ===========================================================================
step 6/7 "Validate config + smoke-test the CLI"
echo "  ${DIM}Sourcing secrets.env so the ref:env: passwords resolve...${OFF}"
if [ -f "$HERE/deploy/secrets.env" ]; then
  set -a; . "$HERE/deploy/secrets.env"; set +a
fi
if bash deploy/00_validate_config.sh; then
  ok "config valid"
else
  warn "config validation reported issues (see above). Fix them and re-run this script."
fi

echo
say "CLI smoke test:"
odoo-synth --help >/dev/null 2>&1 && ok "odoo-synth --help" || die "odoo-synth --help failed"
odoo-synth config >/dev/null 2>&1 && ok "odoo-synth config"  || warn "odoo-synth config emitted warnings (expected if AWS unconfigured)"
odoo-synth profile list >/dev/null 2>&1 && ok "odoo-synth profile list" || warn "profile list had issues"

# ===========================================================================
# 7. NEXT STEPS
# ===========================================================================
step 7/7 "What's left"
cat <<NEXT

${BOLD}Setup is complete.${OFF} Remaining steps before you can build/mask/env:

${BOLD}1. Source secrets in every shell${OFF} that runs odoo-synth (or add to ~/.bashrc):
    ${DIM}set -a; . deploy/secrets.env; set +a${OFF}

${BOLD}2. (If AWS wasn't confirmed above) re-run this wizard${OFF} to enter credentials:
    ${DIM}bash deploy/00_setup.sh${OFF}  ${DIM}# writes ~/.aws/credentials + ~/.aws/config${OFF}
    verify:  ${DIM}aws sts get-caller-identity${OFF}

${BOLD}3. Log into Coder${OFF} (needed for build/env/run commands):
    ${DIM}coder login <CODER_URL>${OFF}   ${DIM}# URL comes from step 4 after the Coder server is deployed${OFF}

${BOLD}4. (Optional) Enterprise addons${OFF} -- drop at odoo/enterprise.zip
    ${DIM}(only if a profile has needs_enterprise: 1)${OFF}

${BOLD}5. Deploy the stack to your AWS account${OFF} (first time only):
    ${DIM}bash deploy/run_all.sh${OFF}
    ${DIM}# provisions ECR, base images, Coder server, publishes templates -> deploy/state.env${OFF}

${BOLD}6. Then use the CLI${OFF}:
    ${DIM}odoo-synth profile create --label 'my-profile' --source-dsn 'postgresql://...' ${OFF}
    ${DIM}odoo-synth run mask --profile <id> ...${OFF}
    ${DIM}odoo-synth env create --profile <id> ...${OFF}

Re-run ${BOLD}bash deploy/00_setup.sh${OFF} anytime to reconfigure. See README.md
for the full CLI reference.
NEXT
ok "done."
