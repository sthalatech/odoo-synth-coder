#!/usr/bin/env bash
# Tear down ALL odoo-synth AWS resources. Safe to re-run (idempotent).
#
# Coder is the only compute path (no ECS/Fargate, no ALB). What remains to
# clean up: the Coder server EC2 instance + its SG, any stray workspace VMs
# (env/builder/runner -- tagged by the Coder templates), and optionally ECR.
#
# This script is self-contained: it does NOT load config.yaml (teardown shouldn't
# require the source-DB/Coder secrets just to delete infra). It needs only an AWS
# region and the project name. Region comes from $AWS_REGION / aws config / the
# default region; project defaults to "odoo-synth" and can be overridden with
# $PROJECT or --project <name>. ECR repos are kept unless --ecr is passed.
set -euo pipefail
HERE="$(cd "$(dirname "$0")/.." && pwd)"
export AWS_PAGER=""

PROJECT="odoo-synth"
ECR_DELETE=0
while [ $# -gt 0 ]; do
  case "$1" in
    --ecr) ECR_DELETE=1 ;;
    --project) PROJECT="$2"; shift ;;
    --project=*) PROJECT="${1#--project=}" ;;
    -h|--help)
      echo "usage: bash deploy/teardown.sh [--ecr] [--project <name>]"
      echo "  --ecr           also delete the ECR repos (masker/discovery/odoo)"
      echo "  --project <n>   project name (default: odoo-synth; used for SG + ECR names)"
      exit 0 ;;
    *) echo "unknown arg: $1" >&2; exit 1 ;;
  esac
  shift
done
# Respect an explicit $PROJECT from the env if --project wasn't passed.
[ -n "${PROJECT_OVERRIDE:-}" ] && PROJECT="$PROJECT_OVERRIDE"

# Region: explicit env > aws config default > error.
if [ -z "${AWS_REGION:-}" ]; then
  AWS_REGION="$(aws configure get region 2>/dev/null || true)"
fi
[ -n "$AWS_REGION" ] || { echo "== ERROR: no AWS region (export AWS_REGION or run \`aws configure\`) ==" >&2; exit 1; }
R="$AWS_REGION"

log(){ echo "== $* ==" >&2; }

log "tearing down odoo-synth in $R (project=$PROJECT) ..."

log "terminating Coder server + odoo-synth workspace VMs ..."
# Every odoo-synth-launched instance (the workspacer/builder/discoverer/masker
# templates' workspace VMs, and the Coder server itself) carries
# odoo-synth:managed=true. The per-role tags (odoo-synth:workspacer/builder/
# discoverer/masker) exist too, but their VALUE is the workspace id/issue/phase
# -- never the literal string "true" -- so filtering on those directly never
# matches anything; odoo-synth:managed is the one tag actually set to "true".
for iid in $(aws ec2 describe-instances --region "$R" \
    --filters "Name=tag:odoo-synth:managed,Values=true" \
              "Name=instance-state-name,Values=running,pending,stopping,stopped" \
    --query 'Reservations[].Instances[].InstanceId' --output text 2>/dev/null); do
  aws ec2 terminate-instances --region "$R" --instance-ids "$iid" >/dev/null 2>&1 || true
done
# Redundant with the loop above (the Coder server also carries
# odoo-synth:managed=true), but kept as a defense-in-depth guarantee the
# server is terminated even if that tagging convention ever changes.
# Re-terminating an already-terminating instance is a harmless no-op.
for iid in $(aws ec2 describe-instances --region "$R" \
    --filters "Name=tag:odoo-synth:control-plane,Values=true" \
              "Name=instance-state-name,Values=running,pending,stopping,stopped" \
    --query 'Reservations[].Instances[].InstanceId' --output text 2>/dev/null); do
  aws ec2 terminate-instances --region "$R" --instance-ids "$iid" >/dev/null 2>&1 || true
done

log "deleting Coder server SG ..."
CSG="$(aws ec2 describe-security-groups --region "$R" --filters "Name=group-name,Values=$PROJECT-coder-sg" --query 'SecurityGroups[0].GroupId' --output text 2>/dev/null || true)"
[ -n "$CSG" ] && [ "$CSG" != "None" ] && \
  aws ec2 delete-security-group --group-id "$CSG" --region "$R" >/dev/null 2>&1 || true

# Keep ECR repos + images (re-push is cheap; delete if --ecr passed).
if [ "$ECR_DELETE" = "1" ]; then
  log "deleting ECR repos ..."
  for name in masker discovery odoo; do
    aws ecr delete-repository --repository-name "$PROJECT/$name" --force --region "$R" >/dev/null 2>&1 || true
  done
fi

# Reset state if it exists (teardown may be run from a checkout that never
# provisioned -- don't require deploy/state.env).
STATE="$HERE/deploy/state.env"
[ -f "$STATE" ] && : > "$STATE"
log "TEARDOWN COMPLETE"
