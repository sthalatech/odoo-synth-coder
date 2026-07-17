#!/usr/bin/env bash
# 00: install local prerequisites for the odoo-synth CLI and deploy pipeline.
#
# Idempotent. Safe to re-run. Installs only what's missing. Does NOT touch
# config.yaml / deploy/state.env (those carry secrets) and does NOT log you
# into AWS or Coder -- it tells you how, then validates.
#
# What it installs:
#   - aws CLI v2        (if `aws` not on PATH)
#   - python3 + pip     (if missing)
#   - python deps: pyyaml, boto3   (for the backend / config loader)
#   - coder CLI         (if `coder` not on PATH; needed for build/env/run)
#   - docker            (optional, only needed to pull/inspect built images
#                        locally; the build itself runs on a remote builder)
#
# Run this first, then `bash deploy/00_validate_config.sh`.
set -euo pipefail
HERE="$(cd "$(dirname "$0")/.." && pwd)"

log(){ echo "== $* ==" >&2; }
have(){ command -v "$1" >/dev/null 2>&1; }

need_sudo=""
[ "$(id -u)" -ne 0 ] && need_sudo="sudo"

# ----------------------------------------------------------------------------
# 1. AWS CLI v2
# ----------------------------------------------------------------------------
if ! have aws; then
  log "installing AWS CLI v2 ..."
  if have apt-get; then
    # awscli v2 is not in apt; use the official bundler.
    tmp="$(mktemp -d)"
    curl -fsSL "https://awscli.amazonaws.com/awscli-exe-linux-$(uname -m).zip" -o "$tmp/awscliv2.zip"
    ( cd "$tmp" && unzip -q awscliv2.zip && $need_sudo ./aws/install )
    rm -rf "$tmp"
  elif have yum; then
    tmp="$(mktemp -d)"
    curl -fsSL "https://awscli.amazonaws.com/awscli-exe-linux-$(uname -m).zip" -o "$tmp/awscliv2.zip"
    ( cd "$tmp" && unzip -q awscliv2.zip && $need_sudo ./aws/install )
    rm -rf "$tmp"
  else
    log "WARN: no apt-get/yum; install AWS CLI v2 manually from https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html"
  fi
else
  log "aws CLI present: $(aws --version 2>&1 | head -1)"
fi

# ----------------------------------------------------------------------------
# 2. python3 + pip
# ----------------------------------------------------------------------------
if ! have python3; then
  log "installing python3 + pip ..."
  if have apt-get; then
    $need_sudo apt-get update -y && $need_sudo apt-get install -y python3 python3-pip
  elif have yum; then
    $need_sudo yum install -y python3 python3-pip
  else
    log "ERROR: no apt-get/yum; install python3 + pip manually" >&2; exit 1
  fi
else
  log "python3 present: $(python3 --version 2>&1)"
fi

# Ensure pip is usable (Debian 12 ships a stripped pip that needs --break-system-packages
# or a venv; prefer a venv-free user install via the standard user scheme).
PIP="python3 -m pip"
if ! $PIP --version >/dev/null 2>&1; then
  log "bootstrapping pip ..."
  $need_sudo apt-get install -y python3-pip 2>/dev/null || $need_sudo yum install -y python3-pip || true
fi

# ----------------------------------------------------------------------------
# 3. python deps: pyyaml, boto3 (used by the backend + config loader)
# ----------------------------------------------------------------------------
pip_install() { # pkg
  if python3 -c "import $1" 2>/dev/null; then
    log "python: $1 present"
  else
    log "installing python: $1 ..."
    $PIP install --user --quiet "$1" || $PIP install --quiet "$1" || {
      log "WARN: pip install $1 failed; you may need --break-system-packages or a venv" >&2
    }
  fi
}
pip_install yaml      # imports as `yaml` (pyyaml)
pip_install boto3

# ensure user pip bin dir is on PATH
user_base="$(python3 -m site --user-base 2>/dev/null || true)"
if [ -n "$user_base" ] && [ -d "$user_base/bin" ]; then
  case ":$PATH:" in
    *":$user_base/bin:"*) ;;
    *) log "adding $user_base/bin to PATH for this session"; export PATH="$user_base/bin:$PATH" ;;
  esac
fi

# ----------------------------------------------------------------------------
# 4. coder CLI (needed for profile build / env / run mask via Coder runner)
# ----------------------------------------------------------------------------
if ! have coder; then
  log "installing Coder CLI ..."
  tmp="$(mktemp -d)"
  # Coder publishes static binaries per-arch.
  case "$(uname -m)" in
    x86_64)  arch="amd64" ;;
    aarch64) arch="arm64" ;;
    *)       arch="amd64"; log "WARN: untested arch $(uname -m), trying amd64" ;;
  esac
  url="https://github.com/coder/coder/releases/latest/download/coder_$(uname -s | tr '[:upper:]' '[:lower:]')_${arch}.tar.gz"
  if curl -fsSL "$url" -o "$tmp/coder.tgz"; then
    tar -xzf "$tmp/coder.tgz" -C "$tmp"
    $need_sudo mv "$tmp/coder" /usr/local/bin/coder
    $need_sudo chmod +x /usr/local/bin/coder
  else
    log "WARN: could not download coder from $url; install manually from https://coder.com/docs/install"
  fi
  rm -rf "$tmp"
else
  log "coder CLI present: $(coder version 2>&1 | head -1)"
fi

# ----------------------------------------------------------------------------
# 5. docker (optional; only to pull/inspect built images locally)
# ----------------------------------------------------------------------------
if ! have docker; then
  log "docker NOT found (optional -- only needed to pull/inspect images locally)"
  log "  install from https://docs.docker.com/engine/install/ if you need it"
else
  log "docker present: $(docker --version 2>&1)"
fi

# ----------------------------------------------------------------------------
# 6. next steps (things this script deliberately does NOT do)
# ----------------------------------------------------------------------------
cat <<'NEXT'

== Prerequisites installed. Next steps ==

1. Authenticate to AWS (one of):
     aws configure                       # interactive; writes ~/.aws/credentials
   OR export AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_REGION

   Verify:  aws sts get-caller-identity

2. Create your config (secrets -- never committed):
     cp config.example.yaml config.yaml
     $EDITOR config.yaml                 # fill region, git refs, passwords, bucket
     $EDITOR deploy/state.env            # if connecting to an existing deployment,
                                         # copy state.env from the deploy host

3. Log into Coder (needed for build/env/run commands):
     coder login <CODER_URL>             # sets CODER_URL + CODER_SESSION_TOKEN

4. (Optional) Drop enterprise addons at odoo/enterprise.zip
   (only if a profile has needs_enterprise=1)

5. Validate + run:
     bash deploy/00_validate_config.sh
     ./cli/odoo-synth config
     ./cli/odoo-synth profile list
NEXT
