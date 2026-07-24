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
QUIET=0
for _a in "$@"; do case "$_a" in --quiet) QUIET=1;; *) ;; esac; done

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
  # Coder publishes static binaries per-arch, but the release assets are
  # version-named (e.g. coder_2.34.6_linux_amd64.tar.gz), so the
  # .../releases/latest/download/<name> stable path 404s. Resolve the real
  # asset URL from the GitHub releases API instead.
  case "$(uname -s)" in
    Linux)  os="linux"   ;;
    Darwin) os="darwin"  ;;
    *)      os="linux"; log "WARN: untested OS $(uname -s), trying linux" ;;
  esac
  case "$(uname -m)" in
    x86_64)  arch="amd64" ;;
    aarch64|arm64) arch="arm64" ;;
    *)       arch="amd64"; log "WARN: untested arch $(uname -m), trying amd64" ;;
  esac
  # linux assets are .tar.gz; darwin assets are .zip.
  if [ "$os" = "darwin" ]; then ext="zip"; else ext="tar.gz"; fi
  asset_re="^coder_[0-9].*_${os}_${arch}\.${ext}$"
  url=$(curl -fsSL https://api.github.com/repos/coder/coder/releases/latest \
        | python3 -c "import sys,json,re; 
assets=json.load(sys.stdin).get('assets',[]);
pat=r'$asset_re';
print(next((a['browser_download_url'] for a in assets if re.match(pat,a['name'])), ''))")
  if [ -n "$url" ] && curl -fsSL "$url" -o "$tmp/coder-archive"; then
    case "$ext" in
      tar.gz) tar -xzf "$tmp/coder-archive" -C "$tmp" ;;
      zip)    (cd "$tmp" && unzip -o -q coder-archive) ;;
    esac
    # the archive extracts a single `coder` binary at its root
    $need_sudo mv "$tmp/coder" /usr/local/bin/coder
    $need_sudo chmod +x /usr/local/bin/coder
    log "coder CLI installed: $(/usr/local/bin/coder version 2>&1 | head -1)"
  else
    log "ERROR: could not download coder for ${os}/${arch}"
    log "       queried release assets at https://api.github.com/repos/coder/coder/releases/latest"
    log "       install manually from https://coder.com/docs/install, then re-run this script."
    rm -rf "$tmp"
    exit 1
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
# 6. next steps (skipped in --quiet mode, used by the guided installer)
# ----------------------------------------------------------------------------
if [ "$QUIET" = 0 ]; then
cat <<'NEXT'

== Prerequisites installed. Next steps ==

Run the guided installer to provision + configure everything:
    bash deploy/00_setup.sh

(Or, for a non-interactive full deploy: bash deploy/run_all.sh -- but you'll
still need AWS creds + a config.yaml first. See README.md.)
NEXT
fi

# ----------------------------------------------------------------------------
# 7. put the CLI on PATH (symlink) -- formerly cli/install.sh
# ----------------------------------------------------------------------------
log "linking odoo-synth onto PATH ..."
CLI_BIN="$HERE/cli/odoo-synth"
LINK="${ODOO_SYNTH_LINK:-/usr/local/bin/odoo-synth}"
if [ -x "$CLI_BIN" ]; then
  if [ -w "$(dirname "$LINK")" ]; then
    ln -sf "$CLI_BIN" "$LINK"
  else
    sudo ln -sf "$CLI_BIN" "$LINK"
  fi
  log "linked $LINK -> $CLI_BIN"
else
  log "ERROR: $CLI_BIN not executable; cannot put CLI on PATH" >&2
  exit 1
fi

# ----------------------------------------------------------------------------
# 8. blocking verification -- every required tool is actually on PATH.
#    A soft WARN earlier is fine for optional tools (docker), but aws/python3/
#    coder/odoo-synth are hard prerequisites for the CLI; if any is missing
#    we stop here rather than letting the user hit a cryptic FileNotFoundError
#    from subprocess later.
# ----------------------------------------------------------------------------
log "verifying prerequisites ..."
missing=""
for tool in aws python3 coder odoo-synth; do
  if ! have "$tool"; then missing="$missing $tool"; fi
done
if [ -n "$missing" ]; then
  log "ERROR: required tool(s) missing:$missing"
  log "       the odoo-synth CLI needs aws, python3, and coder on PATH."
  log "       install them (or re-run this script as a user that can write /usr/local/bin) and try again."
  exit 1
fi
log "all prerequisites present: aws, python3, coder, odoo-synth (+ docker optional)"
