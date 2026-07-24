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

# required values (from either source)
# Required for BASIC infrastructure. ODOO_GIT_REF + custom addons are NOT
# here -- they're profile-level (tied to a source DB) and only needed when you
# build a profile's provenance image (`odoo-synth profile build`). The Odoo
# image is never built at install time; it's baked per-profile.
for v in AWS_REGION PROJECT ODOO_SERIES ODOO_GIT_URL \
         PG_MAJOR SOURCE_DB_NAME TARGET_DB_NAME TARGET_DB_USER DUMP_S3_BUCKET \
         GREENMASK_VERSION; do
  req_cfg "$v"
done

# placeholders left from the example? DUMP_S3_BUCKET is required and must be
# filled. ODOO_GIT_REF + custom addons are profile-level/optional (blank = set
# them per-profile via `odoo-synth profile create`), so they're not checked here.
for v in DUMP_S3_BUCKET; do
  val="${!v:-}"
  case "$val" in
    \<*\>) echo "PLACEHOLDER not filled: $v=$val" >&2; err=1;;
  esac
done

# Custom addons are OPTIONAL (empty git_url = core modules only, per
# config.example.yaml). But if one of url/ref is set, the other must be too,
# and neither may be left as the <...> example placeholder.
for v in CUSTOM_ADDONS_GIT_URL CUSTOM_ADDONS_GIT_REF; do
  val="${!v:-}"
  case "$val" in \<*\>) echo "PLACEHOLDER not filled: $v=$val" >&2; err=1;; esac
done
cu="${CUSTOM_ADDONS_GIT_URL:-}"; cr="${CUSTOM_ADDONS_GIT_REF:-}"
if [ -n "$cu" ] && [ -z "$cr" ]; then echo "MISSING config: CUSTOM_ADDONS_GIT_REF (url is set)" >&2; err=1; fi
if [ -z "$cu" ] && [ -n "$cr" ]; then echo "MISSING config: CUSTOM_ADDONS_GIT_URL (ref is set)" >&2; err=1; fi

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

if [ "$err" -ne 0 ]; then
  echo "== config INVALID -- fix the above then retry ==" >&2
  exit 1
fi
echo "== config OK (region=$AWS_REGION project=$PROJECT) =="
