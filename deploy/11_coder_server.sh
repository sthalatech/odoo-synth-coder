#!/usr/bin/env bash
# 11: Coder server (control plane for developer environments).
#
# Replaces the hand-rolled EC2/Secrets-Manager/SG-ingress lifecycle that used to
# live in controlpanel/backend/environments.py. The Coder server is the only NEW
# long-running AWS artifact this step creates: one t3.small EC2 instance in the
# default VPC running `coder server` + its bundled PostgreSQL. Workspace VMs (the
# actual per-issue dev environments) are launched later by Coder from the existing
# thin golden AMI (ENV_AMI_ID) + the existing env instance profile.
#
# Why one EC2 box and not ECS+ALB+RDS: Coder is a single Go binary with a bundled
# Postgres; running it on one instance reuses the default VPC + a single SG and
# adds zero managed services. Workspace agents reach it over the public internet
# on one port (8943); developers reach the dashboard at http://<ip>:8943.
#
# Writes CODER_URL / CODER_SERVER_IP / CODER_INSTANCE_ID / CODER_SG_ID to
# deploy/state.env. The control panel reads CODER_URL + a CODER_SESSION_TOKEN
# (set by `coder login` once, stored in config.env) to drive `coder create`/
# `coder delete`/the API.
#
# Idempotent: re-running reuses the instance + SG. Pass --rebuild to terminate
# and recreate (loses the server's DB). Pass --login to print the login URL.
#
# Usage:
#   deploy/11_coder_server.sh                # create if missing, else no-op
#   deploy/11_coder_server.sh --rebuild       # terminate + recreate
#   deploy/11_coder_server.sh --login        # print the admin login URL
source "$(dirname "$0")/lib.sh"

CODER_NAME="${CODER_NAME:-$PROJECT-coder}"
CODER_PORT="${CODER_PORT:-8943}"
CODER_INSTANCE_TYPE="${CODER_INSTANCE_TYPE:-t3.small}"
CODER_VOLUME_GB="${CODER_VOLUME_GB:-20}"
CODER_VERSION="${CODER_VERSION:-v2.34.6}"
REBUILD=0; DO_LOGIN=0
for a in "$@"; do
  case "$a" in
    --rebuild) REBUILD=1 ;;
    --login)   DO_LOGIN=1 ;;
    *) log "unknown arg: $a"; exit 2 ;;
  esac
done

VPC="$(vpc_id)"
SUBNET="$(subnet_ids | awk '{print $1}')"

# ---------------------------------------------------------------------------
# 1. SG: inbound 8943 (workspace agents + dashboard) from anywhere.
# ---------------------------------------------------------------------------
CODER_SG_NAME="$CODER_NAME-sg"
CODER_SG_ID="$(sg_id "$CODER_SG_NAME")"
if [ -z "$CODER_SG_ID" ] || [ "$CODER_SG_ID" = "None" ]; then
  CODER_SG_ID="$(aws ec2 create-security-group --group-name "$CODER_SG_NAME" \
    --description "odoo-synth Coder server (dashboard + workspace agent ingress)" \
    --vpc-id "$VPC" --region "$AWS_REGION" --query GroupId --output text)"
  log "created SG $CODER_SG_NAME = $CODER_SG_ID"
fi
aws ec2 authorize-security-group-ingress --region "$AWS_REGION" \
  --group-id "$CODER_SG_ID" --protocol tcp --port "$CODER_PORT" --cidr 0.0.0.0/0 \
  >/dev/null 2>&1 || true
# 22 for ops/first-login (limited to the caller's IP, best-effort).
MYIP="$(curl -fsS https://checkip.amazonaws.com 2>/dev/null | tr -d '[:space:]' || true)"
if [ -n "$MYIP" ]; then
  aws ec2 authorize-security-group-ingress --region "$AWS_REGION" \
    --group-id "$CODER_SG_ID" --protocol tcp --port 22 --cidr "$MYIP/32" \
    >/dev/null 2>&1 || true
fi
put_state CODER_SG_ID "$CODER_SG_ID"

# ---------------------------------------------------------------------------
# 1b. IAM role + instance profile so the Coder server can run Terraform that
#     launches workspace VMs. Minimum perms: ec2 run/stop/start/terminate/
#     describe + create-tags, iam PassRole on the env instance profile. This is
#     the one new IAM artifact (reuses the existing env instance profile for the
#     workspace VMs themselves, created by deploy/09_dev_env.sh).
# ---------------------------------------------------------------------------
CODER_ROLE="$CODER_NAME-role"
CODER_PROFILE="$CODER_NAME-profile"
if ! aws iam get-role --role-name "$CODER_ROLE" >/dev/null 2>&1; then
  aws iam create-role --role-name "$CODER_ROLE"     --assume-role-policy-document '{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Service":"ec2.amazonaws.com"},"Action":"sts:AssumeRole"}]}' >/dev/null
fi
ENV_ROLE_ARN="$(aws iam get-role --role-name "$ENV_INSTANCE_PROFILE" \
  --query 'Role.Arn' --output text 2>/dev/null || true)"
# Option E: the Coder server also launches BUILDER workspaces, which assume
# the odoo-synth-builder role (distinct from the env role -- ECR push + S3 +
# Secrets + self-terminate). The server needs iam:PassRole on it too.
BUILDER_ROLE="${BUILDER_ROLE:-$PROJECT-builder}"
BUILDER_ROLE_ARN="$(aws iam get-role --role-name "$BUILDER_ROLE" \
  --query 'Role.Arn' --output text 2>/dev/null || true)"
POLICY_DOC="$(python3 - "$AWS_REGION" "$ACCOUNT_ID" "$ENV_ROLE_ARN" "$BUILDER_ROLE_ARN" <<'PYDOC'
import json, sys
region, acct, env_role_arn, builder_role_arn = sys.argv[1:5]
pass_roles = [r for r in (env_role_arn, builder_role_arn) if r] or ["arn:aws:iam::*:role/*"]
print(json.dumps({
  "Version": "2012-10-17",
  "Statement": [
    {"Sid": "Ec2WorkspaceLifecycle", "Effect": "Allow",
     "Action": ["ec2:RunInstances","ec2:TerminateInstances","ec2:StartInstances",
                "ec2:StopInstances","ec2:Describe*",
                "ec2:CreateTags","ec2:DeleteTags"],
     "Resource": "*"},
    # PassRole targets the ROLE the workspace VM assumes (not its instance profile).
    # Covers BOTH the dev-env role and the builder role so the Coder server can
    # provision workspaces from the odoo-synth-env AND odoo-synth-builder templates.
    {"Sid": "PassWorkspaceRoles", "Effect": "Allow",
     "Action": ["iam:PassRole"],
     "Resource": pass_roles},
    {"Sid": "SsmAmiLookup", "Effect": "Allow",
     "Action": ["ssm:GetParameters"],
     "Resource": ["arn:aws:ssm:*:*:parameter/aws/service/canonical/*"]},
  ],
}))
PYDOC
)"
aws iam put-role-policy --role-name "$CODER_ROLE" \
  --policy-name "$CODER_NAME-policy" --policy-document "$POLICY_DOC" >/dev/null 2>&1 || true
if ! aws iam get-instance-profile --instance-profile-name "$CODER_PROFILE" >/dev/null 2>&1; then
  aws iam create-instance-profile --instance-profile-name "$CODER_PROFILE" >/dev/null
fi
aws iam add-role-to-instance-profile --instance-profile-name "$CODER_PROFILE"   --role-name "$CODER_ROLE" >/dev/null 2>&1 || true
put_state CODER_INSTANCE_PROFILE "$CODER_PROFILE"
log "Coder server IAM role: $CODER_ROLE (profile $CODER_PROFILE)"

# ---------------------------------------------------------------------------
# 2. instance: reuse if present (unless --rebuild). The instance reads its own
#    public IP from IMDS at boot and bakes CODER_ACCESS_URL, so no SSH needed.
# ---------------------------------------------------------------------------
get_coder_instance(){
  aws ec2 describe-instances --region "$AWS_REGION" \
    --filters "Name=tag:Name,Values=$CODER_NAME" \
             "Name=instance-state-name,Values=running,pending,stopping,stopped" \
    --query 'Reservations[].Instances[0].[InstanceId,State.Name,PublicIpAddress]' \
    --output text 2>/dev/null | head -1
}
EXISTING="$(get_coder_instance)"
if [ -n "$EXISTING" ] && [ "$EXISTING" != "None" ]; then
  I_ID="$(awk '{print $1}' <<<"$EXISTING")"
  if [ "$REBUILD" = 1 ]; then
    log "--rebuild: terminating $I_ID"
    aws ec2 terminate-instances --region "$AWS_REGION" --instance-ids "$I_ID" >/dev/null
    aws ec2 wait instance-terminated --region "$AWS_REGION" --instance-ids "$I_ID" 2>/dev/null || true
    EXISTING=""
  fi
fi

if [ -z "$EXISTING" ] || [ "$EXISTING" = "None" ]; then
  log "launching Coder server ($CODER_INSTANCE_TYPE) ..."
  UD="$(mktemp)"; trap 'rm -f "$UD"' EXIT
  cat > "$UD" <<'UD_EOF'
#!/usr/bin/env bash
set -euo pipefail
export HOME=/root
if ! command -v coder >/dev/null 2>&1; then
  cd /tmp
  curl -fsSL -o coder.tar.gz "https://github.com/coder/coder/releases/download/v2.34.6/coder_2.34.6_linux_amd64.tar.gz"
  tar xzf coder.tar.gz && install -m 0755 coder /usr/local/bin/coder && rm -f coder coder.tar.gz
fi
# Fetch our own public IP from IMDSv2 and bake CODER_ACCESS_URL so workspace
# agents phone home to the right address (needed before `coder server` starts).
TOK="$(curl -s -X PUT 'http://169.254.169.254/latest/api/token' -H 'X-aws-ec2-metadata-token-ttl-seconds: 300')"
MYIP="$(curl -s -H "X-aws-ec2-metadata-token: $TOK" http://169.254.169.254/latest/meta-data/public-ipv4)"
install -d -m 700 /etc/coder
# Coder's built-in PostgreSQL refuses to run as root, so create a dedicated
# user and run the systemd unit as it.
if ! id coder >/dev/null 2>&1; then useradd -m -s /bin/bash coder; fi
install -d -m 700 -o coder -g coder /home/coder/.config/coderv2
install -d -m 700 -o coder -g coder /etc/coder
# Subdomain app hosting: each coder_app gets its own origin
# (<app>--<ws>--<owner>.<wildcard>). REQUIRED for Odoo, whose login form/assets
# use absolute server-root paths (/web/login, /web/session/authenticate,
# /web/static/...) that would otherwise resolve against the Coder dashboard
# origin and 404. The Coder flag is --wildcard-access-url /
# CODER_WILDCARD_ACCESS_URL (NOT CODER_APP_HOSTNAME, which is ignored).
# nip.io gives wildcard DNS without a real domain: *.A.B.C.D.nip.io -> A.B.C.D.
cat > /etc/coder/coder.env <<EENV
CODER_ACCESS_URL=http://${MYIP}:8943
CODER_HTTP_ADDRESS=0.0.0.0:8943
# NOTE: the wildcard host MUST include the port (:8943); without it, Coder
# builds app subdomain URLs on the default port 80, which the SG blocks
# (only 8943 + 22 are open). The auth-redirect Location header then sends
# browsers to port 80 -> connection timeout.
CODER_WILDCARD_ACCESS_URL=*.${MYIP}.nip.io:${CODER_PORT}
CODER_LOG_FILTER=debug
EENV
cat > /etc/systemd/system/coder-server.service <<'UNIT'
[Unit]
Description=Coder server (odoo-synth dev-env control plane)
After=network-online.target
Wants=network-online.target
[Service]
Type=simple
User=coder
Group=coder
Environment=HOME=/home/coder
EnvironmentFile=/etc/coder/coder.env
ExecStart=/usr/local/bin/coder server
Restart=always
RestartSec=5
[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload
systemctl enable --now coder-server
echo "coder-server started; access_url=${MYIP}:8943"
UD_EOF
  I_ID="$(aws ec2 run-instances --region "$AWS_REGION" \
    --image-id "$(aws ssm get-parameters --region "$AWS_REGION" \
      --names /aws/service/canonical/ubuntu/server/24.04/stable/current/amd64/hvm/ebs-gp3/ami-id \
      --query 'Parameters[0].Value' --output text)" \
    --instance-type "$CODER_INSTANCE_TYPE" \
    --subnet-id "$SUBNET" --associate-public-ip-address \
    --security-group-ids "$CODER_SG_ID" \
    --iam-instance-profile "Name=$CODER_PROFILE" \
    --block-device-mappings "DeviceName=/dev/sda1,Ebs={VolumeSize=$CODER_VOLUME_GB,VolumeType=gp3}" \
    --user-data "file://$UD" \
    --tag-specifications "ResourceType=instance,Tags=[{Key=Name,Value=$CODER_NAME},{Key=odoo-synth:managed,Value=true}]" \
    --query 'Instances[0].InstanceId' --output text)"
  log "instance $I_ID launching; waiting for running + public IP ..."
  aws ec2 wait instance-running --region "$AWS_REGION" --instance-ids "$I_ID" 2>/dev/null || true
fi

# fetch the public IP (may take a few seconds after running)
CODER_IP=""
for i in $(seq 1 30); do
  CODER_IP="$(aws ec2 describe-instances --region "$AWS_REGION" --instance-ids "$I_ID" \
    --query 'Reservations[0].Instances[0].PublicIpAddress' --output text 2>/dev/null || true)"
  [ -n "$CODER_IP" ] && [ "$CODER_IP" != "None" ] && break
  sleep 3
done
[ -n "$CODER_IP" ] && [ "$CODER_IP" != "None" ] || { log "could not get Coder server public IP"; exit 1; }

CODER_URL="http://$CODER_IP:$CODER_PORT"
put_state CODER_SERVER_IP "$CODER_IP"
put_state CODER_INSTANCE_ID "$I_ID"
put_state CODER_URL "$CODER_URL"
log "Coder server: id=$I_ID  url=$CODER_URL"

# wait for the HTTP endpoint to answer (user-data + service start ~1-2 min)
log "waiting for Coder dashboard at $CODER_URL ..."
for i in $(seq 1 60); do
  code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 "$CODER_URL" 2>/dev/null || echo 000)"
  case "$code" in 200|301|302|303) log "Coder is up (http $code)"; break ;; esac
  sleep 5
done

if [ "$DO_LOGIN" = 1 ]; then
  echo "  open the dashboard and create the first admin: $CODER_URL"
  echo "  then on a host with the coder CLI:"
  echo "    coder login $CODER_URL"
  echo "    echo \"CODER_SESSION_TOKEN=\$(coder tokens create)\" >> config.env"
fi
log "done. Developer-environment control plane is ready at $CODER_URL"
