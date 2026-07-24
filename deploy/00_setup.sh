#!/usr/bin/env bash
# 00_setup.sh -- guided first-time setup for a community user.
#
# Walks you through the full first-run, in order:
#   1. install prerequisites (incl. the coder CLI)
#   2. collect + configure AWS credentials
#   3-5. build config.yaml + a gitignored secrets.env (passwords)
#   6. validate config + CLI smoke test
#   7. provision BASIC infrastructure (ECR, base Odoo image, builder IAM, Coder
#      server, Coder templates) -- with an interactive `coder login` seam in
#      the middle (the Coder server must be up before you can log in, and the
#      templates need the login token to publish)
#   8. hand off to profile creation (the on-demand CLI: per-source-DB profiles,
#      mask, dev envs)
#
# Safe to re-run: it offers to keep or overwrite your existing config, and every
# provisioning step is idempotent. It never commits anything (config.yaml +
# secrets.env are gitignored).
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
step 1/8 "Install prerequisites (AWS CLI, python deps, Coder CLI, odoo-synth on PATH)"
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
step 2/8 "Configure AWS credentials"
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
step 3/8 "config.yaml"
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
step 4/8 "Collect configuration values"
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
step 5/8 "Secrets (DB + Odoo passwords)"
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
  step 4/8 "Collect configuration values (skipped -- keeping existing config.yaml)"
  step 5/8 "Secrets (skipped -- keeping existing)"
fi

# ===========================================================================
# 6. VALIDATE CONFIG
# ===========================================================================
step 6/8 "Validate config"
echo "  ${DIM}Sourcing secrets.env so the ref:env: passwords resolve...${OFF}"
if [ -f "$HERE/deploy/secrets.env" ]; then
  set -a; . "$HERE/deploy/secrets.env"; set +a
fi
VALID_OK=0
if bash deploy/00_validate_config.sh; then
  ok "config valid"
  VALID_OK=1
else
  warn "config validation reported issues (see above)."
  warn "Fix them (edit config.yaml / source secrets) and re-run this wizard."
  warn "You can skip provisioning for now and come back to it."
fi

echo
say "CLI smoke test:"
odoo-synth --help >/dev/null 2>&1 && ok "odoo-synth --help" || die "odoo-synth --help failed"
odoo-synth config  >/dev/null 2>&1 && ok "odoo-synth config"  || warn "odoo-synth config emitted warnings"
odoo-synth profile list >/dev/null 2>&1 && ok "odoo-synth profile list" || warn "profile list had issues"

# ===========================================================================
# 7. PROVISION BASIC INFRASTRUCTURE
# ===========================================================================
# Basic infra = ECR + the provenance-baked base Odoo image + the builder IAM
# role + the Coder server + the Coder templates. This is the one-time AWS
# provisioning that must exist before you can build/mask/env. Profiles (source
# bindings) are created AFTER this, on demand via the CLI.
step 7/8 "Provision basic infrastructure (ECR, base Odoo image, builder IAM, Coder server, templates)"
PROVISIONED=0
if [ "$VALID_OK" = 0 ]; then
  warn "config not valid -- skipping provisioning. Fix config and re-run."
else
  echo "  ${DIM}This provisions real AWS resources in your account (costs a few cents for${OFF}"
  echo "  ${DIM}the build + a t3.large Coder server while it runs). It takes ~10-20 min for${OFF}"
  echo "  ${DIM}the base Odoo image build. You can re-run it safely -- each step is idempotent.${OFF}"
  echo
  if confirm "Provision the basic infrastructure now?"; then
    # Source state.env so CODER_URL etc. are visible if already deployed.
    [ -f "$HERE/deploy/state.env" ] && { set -a; . "$HERE/deploy/state.env"; set +a; }

    # --- 7a. ECR + base image + builder IAM + Coder server (no coder login yet) ---
    say "7a/7c: ECR, base Odoo image, builder IAM, Coder server ..."
    if bash deploy/01_ecr.sh       && bash deploy/02_build_push.sh       && bash deploy/10_builder.sh       && bash deploy/11_coder_server.sh; then
      ok "ECR + base image + builder IAM + Coder server provisioned"
      # reload state.env written by 11_coder_server.sh
      set -a; . "$HERE/deploy/state.env"; set +a
    else
      warn "one of ECR/base-image/builder/coder-server failed (see logs above)."
      warn "fix it and re-run this wizard -- the completed steps are idempotent."
      warn "skipping Coder login + template publish for now."
    fi

    # --- 7b. Coder login (interactive seam) ---
    if [ -n "${CODER_URL:-}" ]; then
      echo
      say "7b/7c: Log into Coder"
      echo "  The Coder server is up at ${BOLD}${CODER_URL}${OFF}."
      echo "  ${DIM}First-time setup: open that URL in a browser and create the first admin${OFF}"
      echo "  ${DIM}user (email + password). Then run coder login here to authenticate this${OFF}"
      echo "  ${DIM}shell -- it stores a session token the template-publish step needs.${OFF}"
      echo
      # If already logged in (CODER_SESSION_TOKEN set, or `coder` has a token),
      # skip the interactive login.
      ALREADY_IN=0
      if [ -n "${CODER_SESSION_TOKEN:-}" ]; then
        ALREADY_IN=1
      elif coder tokens 2>/dev/null | grep -q .; then
        ALREADY_IN=1
      fi
      if [ "$ALREADY_IN" = 1 ]; then
        ok "already logged into Coder"
      else
        if confirm "Run 'coder login $CODER_URL' now? (opens a browser/prints a URL)"; then
          coder login "$CODER_URL" || warn "coder login did not complete -- you can run it manually later."
          # export the token into this shell for the publish step
          if tok="$(coder tokens 2>/dev/null | tail -1)"; then
            [ -n "$tok" ] && export CODER_SESSION_TOKEN="$tok"
          fi
        else
          warn "skipped coder login -- template publish will be skipped (run it manually)."
        fi
      fi
    else
      warn "CODER_URL not in deploy/state.env -- did 11_coder_server.sh run? Skipping Coder login."
    fi

    # --- 7c. Publish the Coder templates (needs coder login) ---
    if [ -n "${CODER_URL:-}" ] && [ -n "${CODER_SESSION_TOKEN:-}" ]; then
      echo
      say "7c/7c: Publish Coder templates"
      if bash deploy/12_publish_template.sh; then
        ok "Coder templates published (odoo-synth-env, odoo-synth-builder)"
        PROVISIONED=1
      else
        warn "template publish failed (see above). Run 'coder login $CODER_URL' then"
        warn "    bash deploy/12_publish_template.sh"
      fi
    fi
  else
    warn "skipped provisioning. Run it later with:"
    warn "    bash deploy/run_all.sh   ${DIM}# or re-run this wizard${OFF}"
  fi
fi

# ===========================================================================
# 8. NEXT STEPS (profiles + on-demand ops)
# ===========================================================================
step 8/8 "What's left: create profiles and run"
cat <<NEXT

${BOLD}Basic infra is${OFF} $([ "$PROVISIONED" = 1 ] && echo "${GREEN}provisioned${OFF}" || echo "${YELLOW}not yet provisioned${OFF}").
The rest is on-demand via the CLI -- you create a profile per source Odoo DB,
then mask + launch dev environments from it.

${BOLD}1. Source secrets in every shell${OFF} that runs odoo-synth (or add to ~/.bashrc):
    ${DIM}set -a; . deploy/secrets.env; set +a${OFF}

${BOLD}2. Create a profile${OFF} (binds a source Odoo DB + its addons repo):
    ${DIM}odoo-synth profile create --label 'my-profile' \${OFF}
    ${DIM}    --source-dsn 'postgresql://user:pass@host:5432/db' \${OFF}
    ${DIM}    --odoo-series 17.0 --addons-git-url <url> --addons-git-ref <ref>${OFF}
    ${DIM}odoo-synth profile list   ${OFF}# see your profiles${OFF}

${BOLD}3. Mask${OFF} the source DB -> masked pg_dump in S3:
    ${DIM}odoo-synth run mask --profile <id> ...${OFF}

${BOLD}4. Launch a dev environment${OFF} (one Coder workspace per GitHub issue):
    ${DIM}odoo-synth env create --profile <id> --issue <num> --repo-url <url>${OFF}

${BOLD}If provisioning was skipped or failed${OFF}, re-run this wizard (or
${DIM}bash deploy/run_all.sh${OFF}) to finish the basic infra first.

${BOLD}(Optional)${OFF} Enterprise addons at odoo/enterprise.zip (only if a profile
has needs_enterprise: 1).

Re-run ${BOLD}bash deploy/00_setup.sh${OFF} anytime to reconfigure or re-provision.
See README.md for the full CLI reference.
NEXT
ok "done."
