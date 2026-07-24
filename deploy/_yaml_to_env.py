#!/usr/bin/env python3
"""Load config.yaml and emit KEY='value' lines for shell sourcing.

Thin CLI wrapper around the shared loader in ``lib/backend/yamlconfig.py`` so
``deploy/lib.sh`` can do ``eval "$(python3 deploy/_yaml_to_env.py)"`` and
export the same flat env-var set the Python backend reads in-process.

Resolves ``ref:`` secret forms (``env:VAR`` / ``ssm:/name``). Default path:
repo-root ``config.yaml`` (falls back to ``config.example.yaml``).

Usage:  python3 deploy/_yaml_to_env.py [path/to/config.yaml]
"""
from __future__ import annotations

import sys
from pathlib import Path

try:
    import yaml  # noqa: F401  (sanity check — yamlconfig also needs it)
except ImportError:
    sys.stderr.write("PyYAML is required: pip3 install --user pyyaml\n")
    sys.exit(2)

REPO = Path(__file__).resolve().parents[1]

# Import the shared loader from lib/backend. We add lib/ to sys.path rather than
# depending on an installed package so this works from a raw checkout (the
# deploy scripts run before any `pip install -e .`).
sys.path.insert(0, str(REPO / "lib"))
from backend import yamlconfig  # noqa: E402


def main() -> None:
    path = Path(sys.argv[1]) if len(sys.argv) > 1 else (REPO / "config.yaml")
    if not path.exists():
        alt = REPO / "config.example.yaml"
        if alt.exists():
            sys.stderr.write(f"WARN: {path} not found; using {alt}\n")
            path = alt
        else:
            sys.stderr.write("ERROR: no config.yaml and no config.example.yaml\n")
            sys.exit(2)
    print(yamlconfig.emit_shell_env(path))


if __name__ == "__main__":
    main()
