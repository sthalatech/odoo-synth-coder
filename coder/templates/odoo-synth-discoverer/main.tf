# Coder template: the odoo-synth DISCOVERER workspace.
#
# Runs lib/backend/discovery.py's provenance discovery: connects to a live
# source Odoo DB + its addons repo, and produces a discovery.json describing
# exactly what an image must contain to run that dataset (installed modules,
# python/apt deps, an auto-generated masking plan). SINGLE-CONTAINER workload
# (the `discovery` image) that reads ~20 env vars, streams logs, exits with a
# code:
#
#   panel writes an env-file to S3 (presigned PUT+GET) + presigns a result URL
#   panel launches `coder create -t odoo-synth-discoverer` passing image URI +
#     the env-file GET URL + the result PUT URL
#   workspace startup_script: docker pull the image, download the env-file,
#     `docker run --env-file` the container, stream its stdout/stderr to the
#     Coder log (which the panel tails via `coder logs -f`), and on completion
#     PUT a result.json {status, exit_code, error, log_tail} to S3, then poweroff.
#
# The panel keeps its orchestration role: it resolves secrets (DB password, git
# token) and builds the env-file, then presigns S3 URLs and polls S3 for the
# result. Provenance (profile discovery_hash, installed_modules, etc.) stays in
# the profile's YAML file -- only the COMPUTE moves to Coder.
#
# Was previously folded into a shared "odoo-synth-runner" template (also used
# for masking); split out so each Coder template maps to one distinct job.
# See odoo-synth-masker for the mask counterpart -- identical infra/launch
# shape, different container image and no local-postgres-target step.
#
# Privilege wall: this workspace uses the UNPRIVILEGED env instance profile
# (odoo-synth-env-instance -> ECR pull only, NO source DB creds in the IAM,
# NO ECR push, NO Secrets Manager). All source DB / git secrets travel in the
# S3 env-file (presigned, short-lived), never as IAM perms and never baked
# into the image. The builder profile (ECR push + Secrets + self-terminate) is
# reserved for the odoo-synth-builder template and is never attached here.
# Short-lived (poweroff after the job) and has no inbound ports (egress-only;
# the Coder agent dials out).
#
# Infra defaults mirror the env/builder templates (thin golden AMI, default-VPC
# subnet, env SG by name) so `coder create` needs no infra params.

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
data "coder_workspace" "me" {}

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
  # discovery is light (reads the source DB + clones the addons repo). A
  # small instance is plenty; overridable per launch.
  default_instance_type = "m5.large"

  # Fallback AMI: dynamically resolved latest Ubuntu 22.04. Override per
  # launch via the ami_id parameter if a custom golden AMI exists.
  default_ami_id = data.aws_ami.ubuntu_2204.id

  # UNPRIVILEGED env profile (ECR pull only). Falls back to the well-known
  # name if the param is empty. Distinct from the builder profile.
  ami_id = coalesce(
    data.coder_parameter.ami_id.value,
    local.default_ami_id,
  )

  instance_profile = coalesce(
    data.coder_parameter.instance_profile.value,
    "odoo-synth-env-instance",
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

data "coder_parameter" "ami_id" {
  name         = "ami_id"
  display_name = "AMI id (thin golden; leave empty for the default)"
  type         = "string"
  default      = local.default_ami_id
  order        = 1
}

data "coder_parameter" "instance_profile" {
  name         = "instance_profile"
  display_name = "IAM instance profile (env/unprivileged; leave empty for the default)"
  type         = "string"
  default      = ""
  order        = 2
}

data "coder_parameter" "subnet_id" {
  name         = "subnet_id"
  display_name = "Subnet id (leave empty for a default-VPC subnet)"
  type         = "string"
  default      = ""
  order        = 3
}

data "coder_parameter" "security_group_id" {
  name         = "security_group_id"
  display_name = "Security group id (leave empty for the env SG)"
  type         = "string"
  default      = ""
  order        = 4
}

data "coder_parameter" "region" {
  name         = "region"
  display_name = "AWS region"
  type         = "string"
  default      = "us-east-1"
  order        = 5
}

data "coder_parameter" "instance_type" {
  name         = "instance_type"
  display_name = "EC2 instance type"
  type         = "string"
  default      = local.default_instance_type
  order        = 6
}

# the single container to run (e.g. <acct>.dkr.ecr.us-east-1.amazonaws.com/
# odoo-synth/discovery:latest).
data "coder_parameter" "image_uri" {
  name         = "image_uri"
  display_name = "Container image to run (ECR URI)"
  type         = "string"
  default      = ""
  order        = 7
}

# presigned GET for the env-setup script the panel wrote to S3 (a shell
# script that `export`s each var with single-quote escaping -- handles
# multi-line values like SSH private keys, which docker --env-file cannot).
data "coder_parameter" "env_file_get_url" {
  name         = "env_file_get_url"
  display_name = "Presigned S3 GET URL for the container env-setup script"
  type         = "string"
  default      = ""
  order        = 8
}

# semicolon-separated list of env var names to pass through to the container
# (`docker run -e KEY` for each). Semicolons (not commas) because the Coder CLI
# --parameter parser splits values on commas. The workspace sources the
# env-setup script then forwards these names.
data "coder_parameter" "env_keys" {
  name         = "env_keys"
  display_name = "Semicolon-separated env var names to pass to the container"
  type         = "string"
  default      = ""
  order        = 9
}

# presigned PUT for the result.json marker (status, exit_code, error, log_tail).
data "coder_parameter" "result_put_url" {
  name         = "result_put_url"
  display_name = "Presigned S3 PUT URL for the result.json marker"
  type         = "string"
  default      = ""
  order        = 10
}

# a short tag for logs (e.g. "discover" / the profile id).
data "coder_parameter" "phase" {
  name         = "phase"
  display_name = "Phase tag (for log identification)"
  type         = "string"
  default      = "discover"
  order        = 11
}

resource "coder_agent" "main" {
  os   = "linux"
  arch = "amd64"

  # blocking so the workspace stays "building" until the container finishes;
  # the script powers off the instance on completion.
  startup_script_behavior = "blocking"
  startup_script          = <<-EOT
    #!/usr/bin/env bash
    set -uo pipefail
    cd /root
    exec > >(tee -a /var/log/odoo-synth-discoverer.log) 2>&1
    echo "[discoverer] $(date -u) starting phase=${data.coder_parameter.phase.value} image=${data.coder_parameter.image_uri.value}"

    REGION="${data.coder_parameter.region.value}"
    IMAGE_URI="${data.coder_parameter.image_uri.value}"
    ENV_FILE_GET_URL="${data.coder_parameter.env_file_get_url.value}"
    ENV_KEYS="${data.coder_parameter.env_keys.value}"
    RESULT_PUT_URL="${data.coder_parameter.result_put_url.value}"
    PHASE="${data.coder_parameter.phase.value}"

    LOG=/var/log/odoo-synth-discoverer.log
    STATUS="failed"
    EXIT_CODE=1
    ERROR=""

    fail() { ERROR="$1"; echo "[discoverer] ERROR: $1"; }

    finish() {
      TAIL="$(tail -c 12000 "$LOG" 2>/dev/null | python3 -c 'import sys,json;print(json.dumps(sys.stdin.read()))' 2>/dev/null || echo "")"
      printf '{"status":"%s","exit_code":%s,"error":%s,"log_tail":%s}\n' \
        "$STATUS" "$EXIT_CODE" \
        "$(printf '%s' "$ERROR" | python3 -c 'import sys,json;print(json.dumps(sys.stdin.read()))' 2>/dev/null || echo '""')" \
        "$TAIL" > /tmp/discoverer-result.json
      if [ -n "$RESULT_PUT_URL" ]; then
        curl -sS -X PUT -H "Content-Type: application/json" \
          --data-binary @/tmp/discoverer-result.json "$RESULT_PUT_URL" || true
        echo "[discoverer] result uploaded (status=$STATUS exit=$EXIT_CODE); powering off"
      else
        echo "[discoverer] no result URL; powering off (status=$STATUS exit=$EXIT_CODE)"
      fi
      poweroff || true
    }
    trap finish EXIT

    # --- prerequisites -----------------------------------------------------
    # Docker + AWS CLI are baked into the golden AMI at install time
    # (deploy/09_dev_env.sh -> lib/environments/provision.sh). A workspace
    # launched from that AMI needs zero provisioning here; we only verify.
    command -v docker >/dev/null 2>&1 || { ERROR="docker not found on the AMI; bake the golden AMI via deploy/09_dev_env.sh first"; exit 1; }
    command -v aws    >/dev/null 2>&1 || { ERROR="aws cli not found on the AMI; bake the golden AMI via deploy/09_dev_env.sh first"; exit 1; }

    # --- ECR login (pull) --------------------------------------------------
    REGISTRY="$(echo "$IMAGE_URI" | cut -d/ -f1)"
    echo "[discoverer] logging in to ECR $REGISTRY ..."
    aws ecr get-login-password --region "$REGION" \
      | docker login --username AWS --password-stdin "$REGISTRY" || { ERROR="ECR login failed"; exit 1; }

    # --- pull the image ----------------------------------------------------
    echo "[discoverer] pulling $IMAGE_URI ..."
    docker pull "$IMAGE_URI" || { ERROR="docker pull failed"; exit 1; }

    # --- fetch the env-setup script + source it ---------------------------
    # The panel wrote a shell script that `export`s each var with single-quote
    # escaping (handles multi-line values like SSH private keys, which docker's
    # line-based --env-file cannot). We source it into the host env, then pass
    # each var through to the container with `docker run -e KEY` (no value ->
    # forwards the host env, newlines intact).
    ENV_SCRIPT=/tmp/container-env.sh
    : > "$ENV_SCRIPT"
    if [ -n "$ENV_FILE_GET_URL" ]; then
      echo "[discoverer] downloading env-setup script ..."
      curl -fsSL "$ENV_FILE_GET_URL" -o "$ENV_SCRIPT" || { ERROR="could not download env-setup script"; exit 1; }
      # shellcheck disable=SC1090
      . "$ENV_SCRIPT" || { ERROR="env-setup script failed to source"; exit 1; }
    else
      echo "[discoverer] no env-setup URL; running with no env"
    fi

    # --- resolve the profile's GitHub token (Coder user secret) -----------
    # The CLI passes GIT_TOKEN_ENV (the name of the Coder-injected env var for
    # this profile, e.g. GH_PAT_PROF_749C8A90). Coder injects the value into
    # the workspace agent env automatically; we re-export it as GIT_TOKEN for
    # the container (which reads GIT_TOKEN, needed to clone a private addons
    # repo). No AWS Secrets Manager round-trip.
    if [ -n "$${GIT_TOKEN_ENV:-}" ]; then
      _val="$(printf '%s' "$${!GIT_TOKEN_ENV:-}")"
      if [ -n "$_val" ]; then
        export GIT_TOKEN="$_val"
        echo "[discoverer] git token resolved from $${GIT_TOKEN_ENV}"
      else
        echo "[discoverer] WARN: $${GIT_TOKEN_ENV} is empty (secret not set in Coder?)"
      fi
    fi

    # --- build the -e KEY args --------------------------------------------
    ENV_ARGS=()
    if [ -n "$ENV_KEYS" ]; then
      IFS=';' read -ra _KEYS <<< "$ENV_KEYS"
      for _k in "$${_KEYS[@]}"; do
        [ -n "$_k" ] && ENV_ARGS+=("-e" "$_k")
      done
    fi
    # Forward the resolved GIT_TOKEN (from the Coder user secret) into the
    # container so the discovery image can clone a private addons repo.
    if [ -n "$${GIT_TOKEN:-}" ]; then
      ENV_ARGS+=("-e" "GIT_TOKEN")
    fi

    # --- run the container -------------------------------------------------
    # --network host so the container can reach the source DB. Stream
    # stdout+stderr to the Coder log (the panel tails `coder logs -f`).
    echo "[discoverer] running container (phase=$PHASE) ..."
    set +e
    docker run --rm --network host "$${ENV_ARGS[@]}" "$IMAGE_URI" 2>&1 \
      | sed -u 's/^/[container] /'
    EXIT_CODE=$${PIPESTATUS[0]}
    set -e

    if [ "$EXIT_CODE" -eq 0 ]; then
      STATUS="succeeded"
      echo "[discoverer] done: container exited 0"
    else
      ERROR="container exited $EXIT_CODE"
      echo "[discoverer] $ERROR"
    fi
    exit 0
  EOT
}

# workspace VM: UNPRIVILEGED env profile (ECR pull only), same thin golden AMI,
# default-VPC subnet, public IP for egress to ECR/S3/source DB.
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
    Name                     = "odoo-synth-discoverer-${data.coder_parameter.phase.value}"
    "odoo-synth:discoverer"  = data.coder_parameter.phase.value
    "odoo-synth:managed"     = "true"
  }
}

# start/stop the instance with the workspace lifecycle.
resource "aws_ec2_instance_state" "workspace" {
  instance_id = aws_instance.workspace.id
  state       = data.coder_workspace.me.transition == "start" ? "running" : "stopped"
}
