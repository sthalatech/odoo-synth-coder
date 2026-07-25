# Coder template: the odoo-synth BUILDER workspace (Option E, Phase 2).
#
# Replaces the panel's hand-rolled ephemeral builder EC2 (build.py:
# _launch_builder -> run_instances + self-terminating user-data) with a Coder
# workspace. The panel keeps its orchestration role: it packages the odoo/
# build context, uploads it to S3, presigns a result PUT URL, then launches this
# workspace with `coder create -t odoo-synth-builder` passing those URLs + the
# target image URI + provenance as parameters. The workspace's startup_script
# runs the image-build logic (download context -> docker
# build -> push to ECR -> PUT result JSON to S3), then powers off. The panel
# polls S3 for the result exactly as before (_poll_result), so provenance
# (image_uri, image_history, image_status) stays in the profile's YAML file --
# only the COMPUTE moves to Coder.
#
# Privilege wall preserved: this workspace uses the BUILDER instance profile
# (odoo-synth-builder-instance -> ECR push + S3 r/w on dumps bucket + Secrets
# Manager profile/env read + self-terminate), which is distinct from and never
# attached to developer environments. Dev envs keep the unprivileged env profile
# (ECR pull only, no source DB, no push). The builder workspace is short-lived
# (poweroff after the build) and has no inbound ports (egress-only; the Coder
# agent dials out).
#
# Infra defaults mirror the env template (same thin golden AMI, default-VPC
# subnet via data.aws_subnets, env SG by name) so `coder create` needs no infra
# params -- only the build-specific ones (image URI, context/result URLs,
# provenance refs, git token secret).

terraform {
  required_providers {
    coder = {
      source  = "coder/coder"
      version = ">= 2.0"
    }
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
}

# ---------- infra data sources (resolve without hardcoded IDs) ------------
# env SG by name (created by deploy/09_dev_env.sh; egress-only is fine -- the
# builder only dials out to ECR/S3/GitHub/Secrets Manager).
data "aws_security_groups" "env_sg" {
  filter {
    name   = "group-name"
    values = ["odoo-synth-env-sg"]
  }
}

data "aws_subnets" "default_vpc" {
  filter {
    name   = "default-for-az"
    values = ["true"]
  }
}

# Dynamically resolve the latest Ubuntu 22.04 AMI for the current region.
# Used as a fallback when no explicit ami_id is provided. This keeps the
# template portable across AWS accounts and regions without hardcoding
# account-specific AMI IDs.
data "aws_ami" "ubuntu_2204" {
  most_recent = true
  owners      = ["099720109477"] # Canonical

  filter {
    name   = "name"
    values = ["ubuntu/images/hvm-ssd/ubuntu-jammy-22.04-amd64-server-*"]
  }

  filter {
    name   = "virtualization-type"
    values = ["hvm"]
  }
}

locals {
  # builders are beefier (docker build of Odoo + addons clone); overridable.
  default_instance_type = "m5.xlarge"

  # Fallback AMI: dynamically resolved latest Ubuntu 22.04. Override per
  # launch via the ami_id parameter if a custom golden AMI exists.
  default_ami_id = data.aws_ami.ubuntu_2204.id

  # builder instance profile (ECR push + S3 + Secrets + self-terminate).
  # Falls back to the well-known name if the param is empty.
  ami_id = coalesce(
    data.coder_parameter.ami_id.value,
    local.default_ami_id,
  )

  instance_profile = coalesce(
    data.coder_parameter.instance_profile.value,
    "odoo-synth-builder-instance",
  )

  sg_id = coalesce(
    data.coder_parameter.security_group_id.value,
    try(data.aws_security_groups.env_sg.ids[0], null),
  )

  subnet_id = coalesce(
    data.coder_parameter.subnet_id.value,
    try(data.aws_subnets.default_vpc.ids[0], null),
  )
}

# ---------- parameters ------------------------------------------------------

data "coder_parameter" "instance_type" {
  name         = "instance_type"
  display_name = "EC2 instance type for the builder"
  default      = local.default_instance_type
  icon         = "/icon/aws.svg"
  order        = 1
}

data "coder_parameter" "region" {
  name         = "region"
  type         = "string"
  display_name = "AWS region"
  default      = "us-east-1"
  icon         = "/icon/aws.svg"
  order        = 2
}

data "coder_parameter" "ami_id" {
  name         = "ami_id"
  type         = "string"
  display_name = "AMI id (thin golden; leave empty for the default)"
  default      = local.default_ami_id
  icon         = "/icon/aws.svg"
  order        = 3
}

data "coder_parameter" "instance_profile" {
  name         = "instance_profile"
  type         = "string"
  display_name = "IAM instance profile (builder; leave empty for the default)"
  default      = ""
  icon         = "/icon/aws.svg"
  order        = 4
}

data "coder_parameter" "subnet_id" {
  name         = "subnet_id"
  type         = "string"
  display_name = "Subnet id (leave empty for a default-VPC subnet)"
  default      = ""
  icon         = "/icon/aws.svg"
  order        = 5
}

data "coder_parameter" "security_group_id" {
  name         = "security_group_id"
  type         = "string"
  display_name = "Security group id (leave empty for the env SG)"
  default      = ""
  icon         = "/icon/aws.svg"
  order        = 6
}

data "coder_parameter" "image_uri" {
  name         = "image_uri"
  type         = "string"
  display_name = "Target ECR image URI (registry/proj/odoo:<profile>-<hash>)"
  default      = ""
  icon         = "/icon/docker.svg"
  order        = 7
}

data "coder_parameter" "context_get_url" {
  name         = "context_get_url"
  type         = "string"
  display_name = "Presigned S3 GET URL for the odoo/ build-context tarball"
  default      = ""
  icon         = "/icon/aws.svg"
  order        = 8
}

data "coder_parameter" "result_put_url" {
  name         = "result_put_url"
  type         = "string"
  display_name = "Presigned S3 PUT URL for the build-result JSON"
  default      = ""
  icon         = "/icon/aws.svg"
  order        = 9
}

data "coder_parameter" "odoo_image_base" {
  name         = "odoo_image_base"
  type         = "string"
  display_name = "Base Odoo image (FROM) for the docker build"
  default      = "odoo:17"
  icon         = "/icon/docker.svg"
  order        = 10
}

data "coder_parameter" "odoo_git_url" {
  name         = "odoo_git_url"
  type         = "string"
  display_name = "Odoo core git URL"
  default      = "https://github.com/odoo/odoo"
  icon         = "/icon/github.svg"
  order        = 11
}

data "coder_parameter" "odoo_git_ref" {
  name         = "odoo_git_ref"
  type         = "string"
  display_name = "Odoo core git ref (branch/tag/commit)"
  default      = "17.0"
  icon         = "/icon/github.svg"
  order        = 12
}

data "coder_parameter" "custom_addons_git_url" {
  name         = "custom_addons_git_url"
  type         = "string"
  display_name = "Custom addons git URL (empty = core only)"
  default      = ""
  icon         = "/icon/github.svg"
  order        = 13
}

data "coder_parameter" "custom_addons_git_ref" {
  name         = "custom_addons_git_ref"
  type         = "string"
  display_name = "Custom addons git ref"
  default      = ""
  icon         = "/icon/github.svg"
  order        = 14
}

data "coder_parameter" "python_deps" {
  name         = "python_deps"
  type         = "string"
  display_name = "Discovered extra Python deps (pip-install line; empty = none)"
  default      = ""
  icon         = "/icon/python.svg"
  order        = 15
}

data "coder_parameter" "git_token_secret" {
  name         = "git_token_secret"
  type         = "string"
  display_name = "Secrets Manager secret id holding the GitHub token (private addons)"
  default      = ""
  icon         = "/icon/aws.svg"
  order        = 16
}

data "coder_parameter" "issue" {
  name         = "issue"
  type         = "string"
  display_name = "Free-form label (profile id / issue) for the workspace name tag"
  default      = ""
  icon         = "/icon/code.svg"
  order        = 17
}

# ---------- the workspace ---------------------------------------------------

data "coder_workspace" "me" {}

data "coder_workspace_owner" "me" {}

resource "coder_agent" "main" {
  os   = "linux"
  arch = "amd64"

  # blocking so the workspace stays "building" until the startup_script (the
  # build) finishes; the script powers off the instance on completion.
  startup_script_behavior = "blocking"
  startup_script          = <<-EOT
    #!/usr/bin/env bash
    set -uo pipefail
    cd /root
    exec > >(tee -a /var/log/odoo-synth-build.log) 2>&1
    echo "[build] $(date -u) starting build for ${data.coder_parameter.image_uri.value}"

    REGION="${data.coder_parameter.region.value}"
    CONTEXT_GET_URL="${data.coder_parameter.context_get_url.value}"
    RESULT_PUT_URL="${data.coder_parameter.result_put_url.value}"
    IMAGE_URI="${data.coder_parameter.image_uri.value}"
    ODOO_IMAGE_BASE="${data.coder_parameter.odoo_image_base.value}"
    ODOO_GIT_URL="${data.coder_parameter.odoo_git_url.value}"
    ODOO_GIT_REF="${data.coder_parameter.odoo_git_ref.value}"
    CUSTOM_ADDONS_GIT_URL="${data.coder_parameter.custom_addons_git_url.value}"
    CUSTOM_ADDONS_GIT_REF="${data.coder_parameter.custom_addons_git_ref.value}"
    PYTHON_DEPS="${data.coder_parameter.python_deps.value}"
    GIT_TOKEN_SECRET="${data.coder_parameter.git_token_secret.value}"

    LOG=/var/log/odoo-synth-build.log
    STATUS="failed"
    ERROR=""

    fail() { ERROR="$1"; echo "[build] ERROR: $1"; }

    finish() {
      TAIL="$(tail -c 12000 "$LOG" 2>/dev/null | python3 -c 'import sys,json;print(json.dumps(sys.stdin.read()))' 2>/dev/null || echo "")"
      printf '{"status":"%s","image_uri":"%s","error":%s,"log_tail":%s}\n' \
        "$STATUS" "$IMAGE_URI" \
        "$(printf '%s' "$ERROR" | python3 -c 'import sys,json;print(json.dumps(sys.stdin.read()))' 2>/dev/null || echo '""')" \
        "$TAIL" > /tmp/build-result.json
      curl -sS -X PUT -H "Content-Type: application/json" \
        --data-binary @/tmp/build-result.json "$RESULT_PUT_URL" || true
      echo "[build] result uploaded (status=$STATUS); powering off"
      # power off the workspace (Coder stops it). The panel polls S3 for the
      # result; the workspace can be deleted by the panel via `coder delete`.
      poweroff || true
    }
    trap finish EXIT

    # --- prerequisites -----------------------------------------------------
    # Docker (+ buildx) and the AWS CLI are baked into the golden AMI at install
    # time (deploy/09_dev_env.sh -> lib/environments/provision.sh), so a workspace
    # launched from that AMI needs zero provisioning here. We only verify they're
    # present (a non-golden AMI is a misconfiguration, not something to fix at
    # build time -- installing docker mid-build is slow and fragile).
    command -v docker >/dev/null 2>&1 || { ERROR="docker not found on the AMI; bake the golden AMI via deploy/09_dev_env.sh first"; exit 1; }
    command -v aws    >/dev/null 2>&1 || { ERROR="aws cli not found on the AMI; bake the golden AMI via deploy/09_dev_env.sh first"; exit 1; }
    export DOCKER_BUILDKIT=1

    # --- fetch the build context ------------------------------------------
    mkdir -p /root/ctx
    echo "[build] downloading build context ..."
    curl -fsSL "$CONTEXT_GET_URL" -o /root/ctx.tgz || { ERROR="could not download build context"; exit 1; }
    tar xzf /root/ctx.tgz -C /root/ctx || { ERROR="could not extract build context"; exit 1; }
    mkdir -p /root/ctx/enterprise /root/ctx/custom-addons
    touch /root/ctx/enterprise/.gitkeep /root/ctx/custom-addons/.gitkeep
    # Bake enterprise addons from the bundled odoo/enterprise.zip (included in
    # the context only when the profile needs enterprise). Flatten a single
    # top-level wrapper dir so module folders land directly under enterprise/.
    if [ -f /root/ctx/enterprise.zip ]; then
      echo "[build] unzipping enterprise.zip into enterprise/ ..."
      etmp="$(mktemp -d)"; unzip -q -o /root/ctx/enterprise.zip -d "$etmp"
      einner="$etmp"
      if [ "$(find "$etmp" -maxdepth 1 -mindepth 1 -type d | wc -l)" = "1" ]          && [ -z "$(find "$etmp" -maxdepth 1 -type f)" ]; then
        einner="$(find "$etmp" -maxdepth 1 -mindepth 1 -type d)"
      fi
      rm -rf /root/ctx/enterprise; mkdir -p /root/ctx/enterprise
      cp -a "$einner/." /root/ctx/enterprise/
      rm -rf "$etmp" /root/ctx/enterprise.zip
      echo "[build] enterprise modules staged: $(find /root/ctx/enterprise -maxdepth 1 -mindepth 1 -type d | wc -l)"
    fi

    # --- resolve git token for private addons clone ----------------------
    GH_TOKEN_FILE="$(mktemp)"; chmod 600 "$GH_TOKEN_FILE"
    if [ -n "$GIT_TOKEN_SECRET" ]; then
      aws secretsmanager get-secret-value --region "$REGION" --secret-id "$GIT_TOKEN_SECRET" \
        --query SecretString --output text > "$GH_TOKEN_FILE" 2>/dev/null || true
    fi
    [ -s "$GH_TOKEN_FILE" ] && echo "[build] git token resolved for custom-addons clone" \
      || echo "[build] no git token (public addons or none)"

    # --- ECR login ---------------------------------------------------------
    REGISTRY="$(echo "$IMAGE_URI" | cut -d/ -f1)"
    echo "[build] logging in to ECR $REGISTRY ..."
    aws ecr get-login-password --region "$REGION" \
      | docker login --username AWS --password-stdin "$REGISTRY" || { ERROR="ECR login failed"; exit 1; }

    # --- build -------------------------------------------------------------
    echo "[build] building image $IMAGE_URI (python_deps: $${PYTHON_DEPS:-none}) ..."
    docker build --platform linux/amd64 \
      --build-arg ODOO_IMAGE="$ODOO_IMAGE_BASE" \
      --build-arg ODOO_GIT_URL="$ODOO_GIT_URL" \
      --build-arg ODOO_GIT_REF="$ODOO_GIT_REF" \
      --build-arg CUSTOM_ADDONS_GIT_URL="$CUSTOM_ADDONS_GIT_URL" \
      --build-arg CUSTOM_ADDONS_GIT_REF="$CUSTOM_ADDONS_GIT_REF" \
      --build-arg PYTHON_DEPS="$PYTHON_DEPS" \
      --secret id=gh_token,src="$GH_TOKEN_FILE" \
      -t "$IMAGE_URI" /root/ctx || { ERROR="docker build failed"; exit 1; }

    echo "[build] pushing $IMAGE_URI ..."
    docker push "$IMAGE_URI" || { ERROR="docker push failed"; exit 1; }

    STATUS="succeeded"
    echo "[build] done: $IMAGE_URI"
  EOT
}

# workspace VM: builder profile (ECR push + S3 + Secrets + self-terminate),
# same thin golden AMI, default-VPC subnet, public IP for egress to ECR/S3.
resource "aws_instance" "workspace" {
  ami                         = local.ami_id
  instance_type               = data.coder_parameter.instance_type.value
  subnet_id                   = local.subnet_id
  vpc_security_group_ids      = [local.sg_id]
  iam_instance_profile        = local.instance_profile
  associate_public_ip_address = true
  user_data                   = <<-EOT
    #!/usr/bin/env sh
    set -eux
    export CODER_AGENT_TOKEN="${coder_agent.main.token}"
    export CODER_AGENT_URL="${data.coder_workspace.me.access_url}"
    export CODER_AGENT_AUTH=token
    B="$(mktemp -d -t coder.XXXXXX)"; cd "$B"
    curl -fsSL --compressed "$${CODER_AGENT_URL}/bin/coder-linux-amd64" -o coder || \
      wget -q "$${CODER_AGENT_URL}/bin/coder-linux-amd64" -O coder
    chmod +x coder
    exec ./coder agent
  EOT
  user_data_replace_on_change = true
  tags = {
    Name                 = "odoo-synth-builder-${data.coder_parameter.issue.value}"
    "odoo-synth:builder" = data.coder_parameter.issue.value
    "odoo-synth:managed" = "true"
  }
}

# start/stop the instance with the workspace lifecycle.
resource "aws_ec2_instance_state" "workspace" {
  instance_id = aws_instance.workspace.id
  state       = data.coder_workspace.me.transition == "start" ? "running" : "stopped"
}
