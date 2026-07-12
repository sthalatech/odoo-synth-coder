"""Configuration loader: reads the repo's config.env and deploy/state.env so the
control panel uses the exact same infra values as the shell pipeline. No values
are duplicated here — this is the single source of truth for both.

Also loads controlpanel/config.yml (connection profiles, mask profiles, restore
source types, neutralize defaults) — the community-release, no-hardcoding config.
"""
from __future__ import annotations
import os
from pathlib import Path
from functools import lru_cache

import yaml

# controlpanel/backend/config.py -> repo root is two levels up
REPO_ROOT = Path(__file__).resolve().parents[2]
PANEL_DIR = Path(__file__).resolve().parents[1]
PANEL_CONFIG = PANEL_DIR / "config.yml"


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


# ---------------------------------------------------------------------------
# panel config.yml (connection profiles, mask profiles, restore source types)
# ---------------------------------------------------------------------------

@lru_cache(maxsize=1)
def panel() -> dict:
    if not PANEL_CONFIG.exists():
        return {}
    return yaml.safe_load(PANEL_CONFIG.read_text()) or {}


def connection_profiles() -> list[dict]:
    return panel().get("connections", []) or []


def get_connection(conn_id: str) -> dict | None:
    """Return a connection profile with secrets resolved from env, or None."""
    for c in connection_profiles():
        if c.get("id") == conn_id:
            return _resolve_connection(c)
    return None


def _resolve_connection(c: dict) -> dict:
    """Resolve *_env references into concrete host/dbname/user/password values."""
    def val(direct_key: str, env_key: str, default: str | None = None) -> str | None:
        if c.get(direct_key) is not None:
            return str(c[direct_key])
        env_name = c.get(env_key)
        if env_name:
            return get(env_name, default)
        return default

    return {
        "id": c.get("id"),
        "label": c.get("label", c.get("id")),
        "host": val("host", "host_env"),
        "port": str(c.get("port", 5432)),
        "dbname": val("dbname", "dbname_env"),
        "user": val("user", "user_env"),
        "password": val("password", "password_env") or "",
    }


def mask_profiles() -> list[dict]:
    return panel().get("mask_profiles", []) or []


def restore_source_types() -> list[dict]:
    return panel().get("restore_source_types", []) or []


def neutralize_defaults() -> dict:
    return panel().get("neutralize_defaults", {}) or {}


def dump_s3_bucket() -> str | None:
    aws = panel().get("aws", {}) or {}
    if aws.get("dump_s3_bucket"):
        return aws["dump_s3_bucket"]
    env_name = aws.get("dump_s3_bucket_env")
    return get(env_name) if env_name else None


def dump_s3_prefix() -> str:
    aws = panel().get("aws", {}) or {}
    return aws.get("dump_s3_prefix", "controlpanel-uploads")
