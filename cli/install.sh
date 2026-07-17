#!/usr/bin/env bash
# Install the odoo-synth CLI onto PATH as `odoo-synth` (symlink).
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TARGET="$HERE/odoo-synth"
LINK="${1:-/usr/local/bin/odoo-synth}"
if [ ! -x "$TARGET" ]; then
    echo "error: $TARGET is not executable" >&2
    exit 1
fi
if [ -w "$(dirname "$LINK")" ]; then
    ln -sf "$TARGET" "$LINK"
    echo "linked $LINK -> $TARGET"
else
    sudo ln -sf "$TARGET" "$LINK"
    echo "linked $LINK -> $TARGET (used sudo)"
fi
echo "Run \`odoo-synth --help\` to get started."
