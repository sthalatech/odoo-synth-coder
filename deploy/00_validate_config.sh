#!/usr/bin/env bash
# 00: validate config.yaml before installing.
# Checks: config present + parses, required keys set, AWS CLI authed, required
# CLI tools present. Run this first; `run_all.sh` calls it automatically.
set -euo pipefail
HERE="$(cd "$(dirname "$0")/.." && pwd)"
source "$HERE/deploy/lib.sh"

err=0
req_cfg() { # varname
  if [ -z "${!1:-}" ]; then echo "MISSING config: $1" >&2; err=1; fi
}

echo "== config source: config.yaml =="

# required values -- ONLY install-time / control-plane settings.
# Odoo provenance (series/git_url/git_ref/image), custom addons, and ALL
# database credentials (source/masked names + users + passwords, admin/master
# passwords) are NOT checked here: they are per-PROFILE and per-MASK/run
# concerns, supplied downstream via `odoo-synth profile create` and the
# mask/env CLI commands. The Odoo image is never built at install time.
for v in AWS_REGION PROJECT DUMP_S3_BUCKET GREENMASK_VERSION; do
  req_cfg "$v"
done

# placeholders left from the example? DUMP_S3_BUCKET is required and must be
# filled (not the <your-dumps-bucket> placeholder).
for v in DUMP_S3_BUCKET; do
  val="${!v:-}"
  case "$val" in
    \<*\>) echo "PLACEHOLDER not filled: $v=$val" >&2; err=1;;
  esac
done

# CLI tools
for c in aws python3; do
  if ! command -v "$c" >/dev/null 2>&1; then echo "MISSING CLI: $c" >&2; err=1; fi
done
python3 -c "import yaml" 2>/dev/null || { echo "MISSING: python pyyaml (pip3 install --user pyyaml)" >&2; err=1; }

# AWS auth + region match
if command -v aws >/dev/null 2>&1; then
  if ! aws sts get-caller-identity >/dev/null 2>&1; then
    echo "AWS: not authenticated (run 'aws configure' / set creds)" >&2; err=1
  fi
fi

# DUMP_S3_BUCKET must exist (profile discover/build/mask PutObject into it).
# Auto-create if missing (us-east-1 rejects LocationConstraint) -- catches a
# hand-edited config.yaml that points at a bucket that was never created.
# head-bucket returns 0 only for a bucket you own; 403 = exists but owned by
# another account (name collision, can't create); 404 = truly absent (creatable).
if [ "$err" -eq 0 ] && command -v aws >/dev/null 2>&1    && aws sts get-caller-identity >/dev/null 2>&1; then
  B="${DUMP_S3_BUCKET:-}"
  if [ -n "$B" ]; then
    HC="$(aws s3api head-bucket --bucket "$B" --region "${AWS_REGION:-us-east-1}" 2>&1 >/dev/null; echo $? || true)"
    if [ "$HC" = "0" ]; then
      : # exists and you own it -- fine
    elif [ "$HC" = "254" ] || echo "$HC" | grep -qi "403\|Forbidden"; then
      echo "S3 bucket '$B' exists but is owned by another AWS account (S3 names are global)." >&2
      echo "Pick a unique name, edit dumps_bucket in config.yaml, and re-run." >&2
      err=1
    else
      # 404 / truly absent -> create it
      echo "S3 bucket '$B' does not exist -- creating it." >&2
      if [ "${AWS_REGION:-us-east-1}" = "us-east-1" ]; then
        CERR="$(aws s3api create-bucket --bucket "$B" --region "${AWS_REGION:-us-east-1}" 2>&1 >/dev/null || true)"
      else
        CERR="$(aws s3api create-bucket --bucket "$B" --region "${AWS_REGION:-us-east-1}" \
          --create-bucket-configuration "LocationConstraint=${AWS_REGION:-us-east-1}" 2>&1 >/dev/null || true)"
      fi
      if [ -n "$CERR" ]; then
        echo "could not create bucket $B:" >&2
        printf '  %s\n' "$(echo "$CERR" | sed -e '/^$/d' -e 's/^[[:space:]]*//')" >&2
        err=1
      fi
    fi
  fi
fi

if [ "$err" -ne 0 ]; then
  echo "== config INVALID -- fix the above then retry ==" >&2
  exit 1
fi
echo "== config OK (region=$AWS_REGION project=$PROJECT) =="
