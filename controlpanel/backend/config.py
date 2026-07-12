"""Configuration loader: reads the repo's config.env and deploy/state.env so the
control panel uses the exact same infra values as the shell pipeline. No values
are duplicated here — this is the single source of truth for both.
"""
from __future__ import annotations
import os
from pathlib import Path
from functools import lru_cache

# controlpanel/backend/config.py -> repo root is two levels up
REPO_ROOT = Path(__file__).resolve().parents[2]


def _parse_env_file(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if not path.exists():
        return out
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip().strip('"').strip("'")
        out[key] = val
    return out


@lru_cache(maxsize=1)
def load() -> dict[str, str]:
    """config.env is the base; state.env (created by the deploy scripts) overlays
    the resolved AWS resource ids/endpoints. Real OS env wins over both so the
    container can be reconfigured without editing files."""
    cfg: dict[str, str] = {}
    cfg.update(_parse_env_file(REPO_ROOT / "config.env"))
    cfg.update(_parse_env_file(REPO_ROOT / "deploy" / "state.env"))
    # allow override / injection from the real environment
    for k in list(cfg.keys()):
        if k in os.environ:
            cfg[k] = os.environ[k]
    # a few extra passthroughs that may only exist in the environment
    for k in ("AWS_REGION", "AWS_ACCESS_KEY_ID", "AWS_PROFILE"):
        if k in os.environ:
            cfg[k] = os.environ[k]
    return cfg


def get(key: str, default: str | None = None) -> str | None:
    return load().get(key, default)


def require(key: str) -> str:
    val = get(key)
    if not val:
        raise RuntimeError(f"missing required config value: {key}")
    return val
