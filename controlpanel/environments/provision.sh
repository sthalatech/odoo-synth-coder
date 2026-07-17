#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# Golden-AMI provisioner for odoo-synth developer environments.
#
# Run this ONCE on a fresh Ubuntu 22.04/24.04 instance, then bake an AMI from
# it (aws ec2 create-image). The AMI id goes into config.yaml (environments.
# ami_id / ami_id_env). Per-environment boot work (seed DB, start code-server)
# is done by user-data.sh.tmpl at launch time, so this only installs the
# static toolchain that every environment shares.
# ---------------------------------------------------------------------------
set -euo pipefail

export DEBIAN_FRONTEND=noninteractive
apt-get update -y
apt-get install -y --no-install-recommends \
    ca-certificates curl gnupg git jq unzip \
    postgresql-client python3 python3-venv python3-pip \
    build-essential

# --- Docker (for the per-env local postgres that holds the masked data) -----
install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg \
    | gpg --dearmor -o /etc/apt/keyrings/docker.gpg
chmod a+r /etc/apt/keyrings/docker.gpg
. /etc/os-release
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] \
https://download.docker.com/linux/ubuntu ${VERSION_CODENAME} stable" \
    > /etc/apt/sources.list.d/docker.list
apt-get update -y
apt-get install -y docker-ce docker-ce-cli containerd.io docker-compose-plugin
systemctl enable --now docker

# --- AWS CLI v2 (used by user-data to pull the dump + secret) ---------------
if ! command -v aws >/dev/null 2>&1; then
  curl -fsSL "https://awscli.amazonaws.com/awscli-exe-linux-$(uname -m).zip" -o /tmp/awscli.zip
  unzip -q /tmp/awscli.zip -d /tmp
  /tmp/aws/install
  rm -rf /tmp/awscli.zip /tmp/aws
fi

# --- ttyd (web terminal that serves Claude Code via the Coder app) -------
apt-get install -y --no-install-recommends ttyd

# --- Claude Code (prebuilt binary; no node required) -----------------------
# Installed into /opt/claude-code so it's available to every user (the env
# startup script symlinks /home/dev/.local/bin/claude -> here). Pinning the
# version keeps the AMI deterministic; bump VERSION to refresh.
CLAUDE_VERSION="2.1.211"
CLAUDE_DIR="/opt/claude-code"
mkdir -p "$CLAUDE_DIR"
ARCH="$(uname -m)"
case "$ARCH" in
  x86_64)  CLAUDE_ARCH="x64" ;;
  aarch64) CLAUDE_ARCH="arm64" ;;
  *) echo "unsupported arch $ARCH" >&2; exit 1 ;;
esac
curl -fsSL "https://github.com/anthropics/claude-code/releases/download/v${CLAUDE_VERSION}/claude-linux-${CLAUDE_ARCH}.tar.gz" \
  -o /tmp/claude.tar.gz
tar -xzf /tmp/claude.tar.gz -C "$CLAUDE_DIR"
rm -f /tmp/claude.tar.gz
# the tarball ships a single `claude` binary at its root
ln -sf "$CLAUDE_DIR/claude" /usr/local/bin/claude
chmod +x "$CLAUDE_DIR/claude" /usr/local/bin/claude

# --- Bun (runtime for ralph-wiggum) ----------------------------------------
# Installed system-wide: binary at /opt/bun/bin/bun, symlinked on PATH.
BUN_DIR="/opt/bun"
curl -fsSL https://bun.sh/install | BUN_INSTALL="$BUN_DIR" bash
ln -sf "$BUN_DIR/bin/bun" /usr/local/bin/bun
ln -sf "$BUN_DIR/bin/bunx" /usr/local/bin/bunx 2>/dev/null || true

# --- OpenCode (open-source coding agent; TUI + web + headless) -------------
# Its installer hardcodes $HOME/.opencode/bin, so install under HOME=/opt to
# get a system-wide binary, then symlink. --no-modify-path avoids editing
# shell rc files during the bake.
mkdir -p /opt
# HOME=/opt must be on the bash (the consumer of the pipe), not curl,
# so the installer writes to /opt/.opencode/bin rather than /root/.opencode/bin.
curl -fsSL https://opencode.ai/install | HOME=/opt bash -s -- --no-modify-path
ln -sf /opt/.opencode/bin/opencode /usr/local/bin/opencode

# --- Ralph Wiggum (agentic loop over claude/opencode/codex/...) ------------
# A Bun/TypeScript CLI installed as a global npm package via Bun. Requires at
# least one agent CLI -- we have claude (+ opencode above) so the install
# succeeds. `ralph "task" --agent claude-code` loops the agent autonomously.
BUN_INSTALL="/opt/bun" PATH="/opt/bun/bin:$PATH"   bun add --global @th0rgal/ralph-wiggum
ln -sf /opt/bun/bin/ralph /usr/local/bin/ralph

# A dedicated unprivileged developer user owns the workspace and runs Odoo.
if ! id dev >/dev/null 2>&1; then
  useradd -m -s /bin/bash dev
  usermod -aG docker dev
fi
# Make the agent CLIs discoverable for the dev user's login shells.
install -d -o dev -g dev /home/dev/.local/bin
ln -sf /usr/local/bin/claude   /home/dev/.local/bin/claude
ln -sf /usr/local/bin/opencode /home/dev/.local/bin/opencode
ln -sf /usr/local/bin/ralph    /home/dev/.local/bin/ralph
ln -sf /usr/local/bin/bun      /home/dev/.local/bin/bun
printf 'export PATH="$HOME/.local/bin:$PATH"\n' >> /home/dev/.bashrc

# Pre-pull the postgres image so first boot is fast. The provenance-baked odoo
# image is pulled per-environment from ECR at boot (the instance profile needs
# ecr:GetAuthorizationToken + pull) so the AMI stays thin and never goes stale.
docker pull postgres:16 || true

mkdir -p /opt/odoo-synth-env
echo "provisioned $(date -u +%FT%TZ)" > /opt/odoo-synth-env/PROVISIONED
echo "[provision] golden AMI toolchain installed."
