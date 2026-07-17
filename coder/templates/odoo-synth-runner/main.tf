# Coder template: the odoo-synth RUNNER workspace (Option E, Phase 3).
#
# Replaces the panel's ECS/Fargate mask + discovery tasks (pipeline.run_operation
# and discovery.run_discovery) with a Coder workspace. Both are SINGLE-CONTAINER
# workloads (the `masker` image and the `discovery` image) that read ~20 env
# vars, stream logs, and exit with a code. This template runs ANY of them:
#
#   panel writes an env-file to S3 (presigned PUT+GET) + presigns a result URL
#   panel launches `coder create -t odoo-synth-runner` passing image URI + the
#     env-file GET URL + the result PUT URL + a phase tag
#   workspace startup_script: docker pull the image, download the env-file,
#     `docker run --env-file` the container, stream its stdout/stderr to the
#     Coder log (which the panel tails via `coder logs -f`), and on completion
#     PUT a result.json {status, exit_code, error, log_tail} to S3, then poweroff.
#
# The panel keeps its orchestration role: it resolves secrets (DB passwords, SSH
# keys, git tokens) and builds the env-file exactly as it built the ECS task
# env before, then presigns S3 URLs and polls S3 for the result (just like the
# build phase). Provenance (profile discovery_hash, installed_modules, etc.)
# stays in the profile's YAML file -- only the COMPUTE moves to Coder.
#
# Privilege wall: this workspace uses the UNPRIVILEGED env instance profile
# (odoo-synth-env-instance -> ECR pull only, NO source DB creds in the IAM,
# NO ECR push, NO Secrets Manager). All source DB / SSH / git secrets travel in
# the S3 env-file (presigned, short-lived), never as IAM perms and never baked
# into the image. The builder profile (ECR push + Secrets + self-terminate) is
# reserved for the odoo-synth-builder template and is never attached here.
# Like the builder, the runner is short-lived (poweroff after the phase) and
# has no inbound ports (egress-only; the Coder agent dials out).
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

locals {
  # mask + discovery are light (greenmask/pg_dump or python discovery). A
  # small instance is plenty; overridable per launch.
  default_instance_type = "m5.large"

  # the same thin golden AMI the dev env uses (ubuntu + docker +
  # code-server, baked by deploy/09_dev_env.sh). Override per launch via the
  # ami_id parameter if a newer golden AMI exists.
  default_ami_id = "ami-0e94ad593421c5023"

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
# odoo-synth/masker:latest or .../discovery:latest).
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
# --parameter parser splits values on commas. The runner sources the env-setup
# script then forwards these names.
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

# a short tag for logs (e.g. "mask" / "discover" / the profile id).
data "coder_parameter" "phase" {
  name         = "phase"
  display_name = "Phase tag (for log identification)"
  type         = "string"
  default      = "run"
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
    exec > >(tee -a /var/log/odoo-synth-runner.log) 2>&1
    echo "[runner] $(date -u) starting phase=${data.coder_parameter.phase.value} image=${data.coder_parameter.image_uri.value}"

    REGION="${data.coder_parameter.region.value}"
    IMAGE_URI="${data.coder_parameter.image_uri.value}"
    ENV_FILE_GET_URL="${data.coder_parameter.env_file_get_url.value}"
    ENV_KEYS="${data.coder_parameter.env_keys.value}"
    RESULT_PUT_URL="${data.coder_parameter.result_put_url.value}"
    PHASE="${data.coder_parameter.phase.value}"

    LOG=/var/log/odoo-synth-runner.log
    STATUS="failed"
    EXIT_CODE=1
    ERROR=""

    fail() { ERROR="$1"; echo "[runner] ERROR: $1"; }

    finish() {
      TAIL="$(tail -c 12000 "$LOG" 2>/dev/null | python3 -c 'import sys,json;print(json.dumps(sys.stdin.read()))' 2>/dev/null || echo "")"
      printf '{"status":"%s","exit_code":%s,"error":%s,"log_tail":%s}\n' \
        "$STATUS" "$EXIT_CODE" \
        "$(printf '%s' "$ERROR" | python3 -c 'import sys,json;print(json.dumps(sys.stdin.read()))' 2>/dev/null || echo '""')" \
        "$TAIL" > /tmp/runner-result.json
      if [ -n "$RESULT_PUT_URL" ]; then
        curl -sS -X PUT -H "Content-Type: application/json" \
          --data-binary @/tmp/runner-result.json "$RESULT_PUT_URL" || true
        echo "[runner] result uploaded (status=$STATUS exit=$EXIT_CODE); powering off"
      else
        echo "[runner] no result URL; powering off (status=$STATUS exit=$EXIT_CODE)"
      fi
      poweroff || true
    }
    trap finish EXIT

    # --- prerequisites (docker, awscli, curl) ------------------------------
    if ! command -v docker >/dev/null 2>&1; then
      echo "[runner] installing docker ..."
      apt-get update && apt-get install -y docker.io || { ERROR="docker install failed"; exit 1; }
    fi
    systemctl enable --now docker 2>/dev/null || service docker start 2>/dev/null || true
    command -v aws >/dev/null 2>&1 || apt-get install -y awscli || true

    # --- ECR login (pull) --------------------------------------------------
    REGISTRY="$(echo "$IMAGE_URI" | cut -d/ -f1)"
    echo "[runner] logging in to ECR $REGISTRY ..."
    aws ecr get-login-password --region "$REGION" \
      | docker login --username AWS --password-stdin "$REGISTRY" || { ERROR="ECR login failed"; exit 1; }

    # --- pull the image ----------------------------------------------------
    echo "[runner] pulling $IMAGE_URI ..."
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
      echo "[runner] downloading env-setup script ..."
      curl -fsSL "$ENV_FILE_GET_URL" -o "$ENV_SCRIPT" || { ERROR="could not download env-setup script"; exit 1; }
      # shellcheck disable=SC1090
      . "$ENV_SCRIPT" || { ERROR="env-setup script failed to source"; exit 1; }
    else
      echo "[runner] no env-setup URL; running with no env"
    fi

    # --- build the -e KEY args --------------------------------------------
    ENV_ARGS=()
    if [ -n "$ENV_KEYS" ]; then
      IFS=';' read -ra _KEYS <<< "$ENV_KEYS"
      for _k in "$${_KEYS[@]}"; do
        [ -n "$_k" ] && ENV_ARGS+=("-e" "$_k")
      done
    fi

    # --- mask phase: a local postgres target (no shared DB) ---------------
    # Each mask run restores into a THROWAWAY local postgres container on this
    # workspace VM. The masker neutralizes + prunes there, then pg_dumps it out
    # as the artifact envs hydrate from (MASKED_DUMP_PUT_URL). So no two envs
    # share a DB, and re-masking never clobbers another environment. The
    # TARGET_DB_* from the panel are overridden here to point at this local
    # container and ignored for mask.
    if [ "$PHASE" = "mask" ]; then
      echo "[runner] starting local postgres target for mask ..."
      docker rm -f runner-db >/dev/null 2>&1 || true
      docker run -d --name runner-db --network host \
        -e POSTGRES_PASSWORD=runner -e POSTGRES_USER=runner -e POSTGRES_DB=postgres \
        -v /var/lib/runner-db:/var/lib/postgresql/data postgres:16 \
        >/dev/null 2>&1 || { ERROR="local postgres start failed"; exit 1; }
      for _ in $(seq 1 60); do
        docker exec runner-db pg_isready -U runner >/dev/null 2>&1 && break
        sleep 2
      done
      export TARGET_DB_HOST=127.0.0.1 TARGET_DB_PORT=5432
      export TARGET_DB_USER=runner TARGET_DB_PASSWORD=runner TARGET_DB_NAME=masked
      ENV_ARGS+=("-e" "TARGET_DB_HOST" "-e" "TARGET_DB_PORT" "-e" "TARGET_DB_USER" \
                 "-e" "TARGET_DB_PASSWORD" "-e" "TARGET_DB_NAME")
      echo "[runner] local postgres target ready (127.0.0.1:5432/masked as runner)."
    fi

    # --- run the container -------------------------------------------------
    # --network host so the container can reach the source DB, the SSH tunnel
    # the entrypoint opens, and the local runner-db sibling (mask phase).
    # Stream stdout+stderr to the Coder log (the panel tails `coder logs -f`).
    echo "[runner] running container (phase=$PHASE) ..."
    set +e
    docker run --rm --network host "$${ENV_ARGS[@]}" "$IMAGE_URI" 2>&1 \
      | sed -u 's/^/[container] /'
    EXIT_CODE=$${PIPESTATUS[0]}
    set -e

    if [ "$EXIT_CODE" -eq 0 ]; then
      STATUS="succeeded"
      echo "[runner] done: container exited 0"
    else
      ERROR="container exited $EXIT_CODE"
      echo "[runner] $ERROR"
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
    Name                 = "odoo-synth-runner-${data.coder_parameter.phase.value}"
    "odoo-synth:runner"  = data.coder_parameter.phase.value
    "odoo-synth:managed" = "true"
  }
}

# start/stop the instance with the workspace lifecycle.
resource "aws_ec2_instance_state" "workspace" {
  instance_id = aws_instance.workspace.id
  state       = data.coder_workspace.me.transition == "start" ? "running" : "stopped"
}
