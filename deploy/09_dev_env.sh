#!/usr/bin/env bash
# 09: developer-environment bootstrap.
#
# Provisions everything the control panel needs to launch per-issue VS Code
# environments, and flips `environments` to "configured":
#   * a security group (inbound code-server + odoo from your CIDR)
#   * an IAM role + instance profile (S3 masked-dump read, Secrets Manager read,
#     ECR pull) attached to every environment instance
#   * a THIN golden AMI (ubuntu + docker + code-server) baked from
#     controlpanel/environments/provision.sh  --  Odoo is NOT baked in
#
# Writes ENV_AMI_ID / ENV_SG_ID / ENV_SUBNET_ID / ENV_INSTANCE_PROFILE (and
# AWS_ACCOUNT_ID) to deploy/state.env, which the panel reads via config.yaml.
#
# Idempotent: re-running reuses existing SG / role / profile. The AMI is only
# baked when ENV_AMI_ID is unset or you pass --rebake.
#
# Usage:
#   deploy/09_dev_env.sh                 # infra + bake AMI if missing
#   deploy/09_dev_env.sh --infra-only    # SG + IAM only, skip the AMI bake
#   deploy/09_dev_env.sh --rebake        # force a fresh AMI bake
#
# Optional config.yaml knobs:
#   ENV_INGRESS_CIDR    CIDR allowed to reach code-server/odoo (default: your
#                       current public IP /32; set 0.0.0.0/0 at your own risk)
#   ENV_INSTANCE_TYPE   builder instance type for the bake (default t3.large)
#   ENV_GIT_TOKEN_SECRET  Secrets Manager arn/name of a GitHub token (private
#                       addons repo) -- granted to the instance profile if set
#   DUMP_S3_PREFIX      masked-dump key prefix (default masked-dumps)
source "$(dirname "$0")/lib.sh"

INFRA_ONLY=0; REBAKE=0
for a in "$@"; do
  case "$a" in
    --infra-only) INFRA_ONLY=1 ;;
    --rebake)     REBAKE=1 ;;
    *) log "unknown arg: $a"; exit 2 ;;
  esac
done

VPC="$(vpc_id)"
DUMP_S3_PREFIX="${DUMP_S3_PREFIX:-masked-dumps}"
: "${DUMP_S3_BUCKET:?DUMP_S3_BUCKET must be set in config.yaml}"

put_state AWS_ACCOUNT_ID "$ACCOUNT_ID"

# ---------------------------------------------------------------------------
# 1. security group
# ---------------------------------------------------------------------------
ENV_SG_NAME="$PROJECT-env-sg"
ENV_SG_ID="$(sg_id "$ENV_SG_NAME")"
if [ -z "$ENV_SG_ID" ] || [ "$ENV_SG_ID" = "None" ]; then
  ENV_SG_ID="$(aws ec2 create-security-group --group-name "$ENV_SG_NAME" \
    --description "odoo-synth developer environments (code-server + odoo)" \
    --vpc-id "$VPC" --region "$AWS_REGION" --query GroupId --output text)"
fi

# No inbound rules: the Coder agent dials OUT to the Coder server, and the
# developer reaches the workspace via Coder's Wireguard tunnel. The SG is
# egress-only (AWS default egress), so workspace VMs need no public IP and no
# per-env ingress rules. (The Coder server's own SG, opened on 8943, is
# created separately by deploy/11_coder_server.sh.)
put_state ENV_SG_ID "$ENV_SG_ID"
log "env SG=$ENV_SG_ID (egress-only; Coder tunnel brokers access)"

# RDS-free (Phase B): no managed RDS, so no DB ingress is opened here. Runner
# workspaces (mask + discovery) restore into a throwaway local postgres
# container on the workspace VM itself.

# a subnet for launches (first default subnet)
ENV_SUBNET_ID="$(subnet_ids | awk '{print $1}')"
put_state ENV_SUBNET_ID "$ENV_SUBNET_ID"
log "env SG=$ENV_SG_ID subnet=$ENV_SUBNET_ID"

# ---------------------------------------------------------------------------
# 2. IAM role + instance profile for environment instances
# ---------------------------------------------------------------------------
ENV_ROLE="$PROJECT-env-instance"
ENV_PROFILE="$PROJECT-env-instance"

if ! aws iam get-role --role-name "$ENV_ROLE" >/dev/null 2>&1; then
  aws iam create-role --role-name "$ENV_ROLE" \
    --assume-role-policy-document '{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Service":"ec2.amazonaws.com"},"Action":"sts:AssumeRole"}]}' >/dev/null
fi

# inline policy: S3 masked-dump read + Secrets Manager (env passwords + optional
# git token) + ECR pull. Scoped to this project's resources.
TOKEN_SECRET_ARN=""
if [ -n "${ENV_GIT_TOKEN_SECRET:-}" ]; then
  case "$ENV_GIT_TOKEN_SECRET" in
    arn:aws:*) TOKEN_SECRET_ARN="$ENV_GIT_TOKEN_SECRET" ;;
    *) TOKEN_SECRET_ARN="arn:aws:secretsmanager:${AWS_REGION}:${ACCOUNT_ID}:secret:${ENV_GIT_TOKEN_SECRET}*" ;;
  esac
fi

POLICY_JSON="$(python3 - "$AWS_REGION" "$ACCOUNT_ID" "$DUMP_S3_BUCKET" "$DUMP_S3_PREFIX" "$PROJECT" "$TOKEN_SECRET_ARN" <<'PY'
import json, sys
region, acct, bucket, prefix, project, token_arn = sys.argv[1:7]
secret_arns = [f"arn:aws:secretsmanager:{region}:{acct}:secret:{project}/env/*",
                  f"arn:aws:secretsmanager:{region}:{acct}:secret:{project}/profile/*"]
if token_arn:
    secret_arns.append(token_arn)
doc = {
  "Version": "2012-10-17",
  "Statement": [
    {"Sid": "MaskedDumpRead", "Effect": "Allow",
     "Action": ["s3:GetObject"],
     "Resource": [f"arn:aws:s3:::{bucket}/{prefix}/*"]},
    {"Sid": "MaskedDumpList", "Effect": "Allow",
     "Action": ["s3:ListBucket"],
     "Resource": [f"arn:aws:s3:::{bucket}"],
     "Condition": {"StringLike": {"s3:prefix": [f"{prefix}/*"]}}},
    {"Sid": "EnvSecrets", "Effect": "Allow",
     "Action": ["secretsmanager:GetSecretValue"],
     "Resource": secret_arns},
    {"Sid": "EcrAuth", "Effect": "Allow",
     "Action": ["ecr:GetAuthorizationToken"], "Resource": "*"},
    {"Sid": "EcrPull", "Effect": "Allow",
     "Action": ["ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer",
                "ecr:BatchCheckLayerAvailability"],
     "Resource": [f"arn:aws:ecr:{region}:{acct}:repository/{project}/odoo",
                  # Option E Phase 3: the runner workspaces (mask + discovery)
                  # also assume this unprivileged profile and pull the masker /
                  # discovery images. Pull-only -- no ECR push, no source DB.
                  f"arn:aws:ecr:{region}:{acct}:repository/{project}/masker",
                  f"arn:aws:ecr:{region}:{acct}:repository/{project}/discovery"]},
  ],
}
print(json.dumps(doc))
PY
)"
aws iam put-role-policy --role-name "$ENV_ROLE" \
  --policy-name "$PROJECT-env-instance-policy" \
  --policy-document "$POLICY_JSON" >/dev/null

if ! aws iam get-instance-profile --instance-profile-name "$ENV_PROFILE" >/dev/null 2>&1; then
  aws iam create-instance-profile --instance-profile-name "$ENV_PROFILE" >/dev/null
fi
# attach role to profile (ignore if already attached)
aws iam add-role-to-instance-profile --instance-profile-name "$ENV_PROFILE" \
  --role-name "$ENV_ROLE" >/dev/null 2>&1 || true
put_state ENV_INSTANCE_PROFILE "$ENV_PROFILE"
log "env instance profile: $ENV_PROFILE (role $ENV_ROLE)"

# repo defaults come from the pipeline's custom addons unless overridden.
[ -n "${CUSTOM_ADDONS_GIT_URL:-}" ] && put_state ENV_REPO_URL "$CUSTOM_ADDONS_GIT_URL"
[ -n "${CUSTOM_ADDONS_GIT_REF:-}" ] && put_state ENV_REPO_BRANCH "$CUSTOM_ADDONS_GIT_REF"
[ -n "${ENV_GIT_TOKEN_SECRET:-}" ] && put_state ENV_GIT_TOKEN_SECRET "$ENV_GIT_TOKEN_SECRET"

if [ "$INFRA_ONLY" = 1 ]; then
  log "infra-only: skipping AMI bake"
  log "done. SG=$ENV_SG_ID profile=$ENV_PROFILE (set ENV_AMI_ID to finish, or re-run without --infra-only)"
  exit 0
fi

# ---------------------------------------------------------------------------
# 3. bake the thin golden AMI
# ---------------------------------------------------------------------------
if [ -n "${ENV_AMI_ID:-}" ] && [ "$REBAKE" = 0 ]; then
  log "ENV_AMI_ID already set ($ENV_AMI_ID); pass --rebake to rebuild. done."
  exit 0
fi

ENV_INSTANCE_TYPE="${ENV_INSTANCE_TYPE:-t3.large}"
PROVISION="$HERE/controlpanel/environments/provision.sh"
[ -f "$PROVISION" ] || { log "missing $PROVISION"; exit 1; }

# latest Ubuntu 22.04 LTS AMI (Canonical's SSM public parameter)
BASE_AMI="$(aws ssm get-parameters --region "$AWS_REGION" \
  --names /aws/service/canonical/ubuntu/server/22.04/stable/current/amd64/hvm/ebs-gp2/ami-id \
  --query 'Parameters[0].Value' --output text)"
log "base AMI (ubuntu 22.04): $BASE_AMI"

# user-data = provision.sh, then mark done and power off so we can image a
# stopped instance (no in-flight writes).
UD="$(mktemp)"; trap 'rm -f "$UD"' EXIT
{
  echo '#!/usr/bin/env bash'
  echo 'set -euo pipefail'
  # cloud-init runs user-data as root with no HOME; the code-server installer
  # (and apt/docker helpers) need it. Export before provisioning.
  echo 'export HOME=/root'
  cat "$PROVISION"
  echo 'touch /opt/odoo-synth-env/BAKE_OK'
  echo 'poweroff'
} > "$UD"

log "launching builder ($ENV_INSTANCE_TYPE) to bake the AMI ..."
BUILDER_ID="$(aws ec2 run-instances --region "$AWS_REGION" \
  --image-id "$BASE_AMI" --instance-type "$ENV_INSTANCE_TYPE" \
  --subnet-id "$ENV_SUBNET_ID" --associate-public-ip-address \
  --instance-initiated-shutdown-behavior stop \
  --block-device-mappings 'DeviceName=/dev/sda1,Ebs={VolumeSize=30,VolumeType=gp3}' \
  --user-data "file://$UD" \
  --tag-specifications 'ResourceType=instance,Tags=[{Key=Name,Value='"$PROJECT"'-env-builder},{Key=odoo-synth:managed,Value=true}]' \
  --query 'Instances[0].InstanceId' --output text)"
log "builder: $BUILDER_ID -- provisioning (docker + code-server + awscli); this takes a few minutes"

# Poll for the builder to power itself off once provision.sh completes. We can't
# use `aws ec2 wait instance-stopped` -- that waiter treats the initial "pending"
# state as a terminal failure. Poll manually instead (up to ~15 min).
STATE=""
for i in $(seq 1 90); do
  sleep 10
  STATE="$(aws ec2 describe-instances --region "$AWS_REGION" --instance-ids "$BUILDER_ID" \
    --query 'Reservations[0].Instances[0].State.Name' --output text 2>/dev/null || echo unknown)"
  case "$STATE" in
    stopped) break ;;
    terminated|shutting-down) log "builder entered $STATE unexpectedly"; break ;;
  esac
  [ $((i % 6)) -eq 0 ] && log "  ... builder $STATE (${i}0s elapsed)"
done

if [ "$STATE" != "stopped" ]; then
  log "builder did not reach 'stopped' (last state: $STATE); check the console log:"
  log "  aws ec2 get-console-output --instance-id $BUILDER_ID --region $AWS_REGION"
  exit 1
fi
log "builder provisioned + stopped; creating image ..."

STAMP="$(date -u +%Y%m%d-%H%M%S)"
ENV_AMI_ID="$(aws ec2 create-image --region "$AWS_REGION" \
  --instance-id "$BUILDER_ID" --name "$PROJECT-devenv-$STAMP" \
  --description "odoo-synth thin dev-env AMI (ubuntu+docker+code-server)" \
  --query 'ImageId' --output text)"
log "AMI $ENV_AMI_ID creating; waiting until available ..."
aws ec2 wait image-available --region "$AWS_REGION" --image-ids "$ENV_AMI_ID"
put_state ENV_AMI_ID "$ENV_AMI_ID"

log "terminating builder $BUILDER_ID ..."
aws ec2 terminate-instances --region "$AWS_REGION" --instance-ids "$BUILDER_ID" >/dev/null || true

log "done. Developer environments are configured:"
log "  ENV_AMI_ID=$ENV_AMI_ID"
log "  ENV_SG_ID=$ENV_SG_ID  ENV_SUBNET_ID=$ENV_SUBNET_ID"
log "  ENV_INSTANCE_PROFILE=$ENV_PROFILE"
log ""
log "NOTE: the control panel's own AWS credentials also need, to launch/tear"
log "down environments: ec2:RunInstances/TerminateInstances/DescribeInstances,"
log "iam:PassRole (on $ENV_ROLE), secretsmanager:CreateSecret/DeleteSecret"
log "(on $PROJECT/env/*). The instance profile above is separate from those."
