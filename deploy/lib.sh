#!/usr/bin/env bash
# Shared helpers for deploy scripts. `source` this.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# Config: config.yaml is the single source of truth (see config.example.yaml).
# deploy/_yaml_to_env.py loads it and exports the KEY=VALUE env vars the
# pipeline scripts expect (with `ref:` secret resolution for env/SSM). The
# python3 dependency is installed by deploy/00_install_prereqs.sh.
_load_config() {
  if [ ! -f "$HERE/config.yaml" ]; then
    echo "== ERROR: $HERE/config.yaml not found ==" >&2
    echo "       copy config.example.yaml to config.yaml and fill it in, then" >&2
    echo "       run bash deploy/00_validate_config.sh." >&2
    return 1
  fi
  if ! command -v python3 >/dev/null 2>&1; then
    echo "== ERROR: python3 not found on PATH (needed to load config.yaml) ==" >&2
    echo "       run bash deploy/00_install_prereqs.sh first." >&2
    return 1
  fi
  eval "$(python3 "$HERE/deploy/_yaml_to_env.py" "$HERE/config.yaml")"
  return $?
}
_load_config

# ACCOUNT_ID requires live AWS auth. Resolve it best-effort so that sourcing
# lib.sh does not abort (under `set -e`) before a caller can report a friendly
# "not authenticated" error -- e.g. 00_validate_config.sh sources this to read
# config values but must not die on missing creds. Scripts that actually need
# ACCOUNT_ID (09_dev_env/10_builder/11_coder_server) re-check it explicitly.
ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text 2>/dev/null || true)"
ECR="${ACCOUNT_ID:+${ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com}"
export HERE ACCOUNT_ID ECR
export AWS_PAGER=""

log(){ echo "== $* ==" >&2; }

STATE="$HERE/deploy/state.env"
touch "$STATE"
set -a; source "$STATE"; set +a
put_state(){ # key value
  # ensure the file ends with a newline so appends never glue onto the last line
  [ -s "$STATE" ] && [ -n "$(tail -c1 "$STATE")" ] && echo >> "$STATE"
  grep -v "^$1=" "$STATE" > "$STATE.tmp" 2>/dev/null || true
  echo "$1=$2" >> "$STATE.tmp"; mv "$STATE.tmp" "$STATE"
  export "$1=$2"
}

vpc_id(){ aws ec2 describe-vpcs --filters Name=isDefault,Values=true \
    --query 'Vpcs[0].VpcId' --output text --region "$AWS_REGION"; }

subnet_ids(){ aws ec2 describe-subnets --filters Name=vpc-id,Values=$(vpc_id) \
    Name=default-for-az,Values=true --query 'Subnets[].SubnetId' \
    --output text --region "$AWS_REGION"; }

sg_id(){ aws ec2 describe-security-groups \
    --filters Name=group-name,Values="$1" Name=vpc-id,Values=$(vpc_id) \
    --query 'SecurityGroups[0].GroupId' --output text --region "$AWS_REGION" 2>/dev/null; }

