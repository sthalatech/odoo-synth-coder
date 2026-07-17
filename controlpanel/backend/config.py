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


def _parse_yaml_env(path: Path) -> dict[str, str]:
    """Run the shared YAML loader and parse its KEY='value' output into a dict.
    Resolves ref:env: / ref:ssm: forms via deploy/_yaml_to_env.py."""
    import subprocess
    try:
        out = subprocess.check_output(
            ["python3", str(REPO_ROOT / "deploy" / "_yaml_to_env.py"), str(path)],
            stderr=subprocess.DEVNULL, text=True)
    except Exception:
        return {}
    d: dict[str, str] = {}
    for raw in out.splitlines():
        if "=" not in raw:
            continue
        k, _, v = raw.partition("=")
        d[k.strip()] = v.strip().strip("'")
    return d


@lru_cache(maxsize=1)
def load() -> dict[str, str]:
    """config.yaml (preferred) or legacy config.env is the base; state.env
    (created by the deploy scripts) overlays the resolved AWS resource
    ids/endpoints. Real OS env wins over both so the container can be
    reconfigured without editing files."""
    cfg: dict[str, str] = {}
    yaml_path = REPO_ROOT / "config.yaml"
    if yaml_path.exists():
        cfg.update(_parse_yaml_env(yaml_path))
    else:
        cfg.update(_parse_env_file(REPO_ROOT / "config.env"))
    cfg.update(_parse_env_file(REPO_ROOT / "deploy" / "state.env"))
    # allow override / injection from the real environment
    for k in list(cfg.keys()):
        if k in os.environ:
            cfg[k] = os.environ[k]
    for k in ("AWS_REGION", "AWS_ACCESS_KEY_ID", "AWS_PROFILE"):
        if k in os.environ:
            cfg[k] = os.environ[k]
    return cfg


def get(key: str, default: str | None = None) -> str | None:
    return load().get(key, default)


def _load_fresh() -> dict[str, str]:
    """Same as load() but WITHOUT the lru_cache — re-reads config.yaml/env +
    state.env from disk on every call. Used for secrets so a value rotated on
    disk takes effect on the next run without restarting the panel."""
    cfg: dict[str, str] = {}
    yaml_path = REPO_ROOT / "config.yaml"
    if yaml_path.exists():
        cfg.update(_parse_yaml_env(yaml_path))
    else:
        cfg.update(_parse_env_file(REPO_ROOT / "config.env"))
    cfg.update(_parse_env_file(REPO_ROOT / "deploy" / "state.env"))
    for k in list(cfg.keys()):
        if k in os.environ:
            cfg[k] = os.environ[k]
    return cfg


def get_fresh(key: str, default: str | None = None) -> str | None:
    """Read a config value bypassing the cache (for secrets that may rotate)."""
    val = os.environ.get(key)
    if val is not None:
        return val
    return _load_fresh().get(key, default)


def require(key: str) -> str:
    val = get(key)
    if not val:
        raise RuntimeError(f"missing required config value: {key}")
    return val


# ---------------------------------------------------------------------------
# panel config.yml (destination, mask profiles, neutralize defaults)
# ---------------------------------------------------------------------------

@lru_cache(maxsize=1)
def panel() -> dict:
    if not PANEL_CONFIG.exists():
        return {}
    return yaml.safe_load(PANEL_CONFIG.read_text()) or {}


def _resolve_conn(c: dict) -> dict:
    """Resolve *_env references in a connection block into concrete values."""
    def val(direct_key: str, env_key: str, default: str | None = None) -> str | None:
        if c.get(direct_key) is not None:
            return str(c[direct_key])
        env_name = c.get(env_key)
        if env_name:
            return get(env_name, default)
        return default

    # secrets are read fresh (bypass cache) so a rotated password on disk takes
    # effect on the next run without a panel restart.
    def secret(direct_key: str, env_key: str) -> str:
        if c.get(direct_key) is not None:
            return str(c[direct_key])
        env_name = c.get(env_key)
        if env_name:
            return get_fresh(env_name, "") or ""
        return ""

    return {
        "label": c.get("label"),
        "host": val("host", "host_env"),
        "port": str(c.get("port", 5432)),
        "dbname": val("dbname", "dbname_env"),
        "user": val("user", "user_env"),
        "password": secret("password", "password_env"),
    }


def destination() -> dict:
    """The managed masked destination DB (created on RDS), resolved from env."""
    d = panel().get("destination", {}) or {}
    return _resolve_conn(d)


def mask_profiles() -> list[dict]:
    return panel().get("mask_profiles", []) or []


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
    return aws.get("dump_s3_prefix", "masked-dumps")


# ---------------------------------------------------------------------------
# developer environments (EC2 + code-server, seeded from a masked dump)
# ---------------------------------------------------------------------------

def environments_cfg() -> dict:
    return panel().get("environments", {}) or {}


def _env_val(key: str, env_key: str, default: str | None = None) -> str | None:
    """Resolve environments.<key> or environments.<env_key> (an env-var name)."""
    e = environments_cfg()
    if e.get(key) is not None:
        return str(e[key])
    name = e.get(env_key)
    if name:
        return get(name, default)
    return default


def environments_settings() -> dict:
    """Concrete launch settings for developer environments, resolved from env."""
    e = environments_cfg()
    return {
        "enabled": bool(e.get("enabled", True)),
        # Coder control plane: the server URL + a session token drive the `coder`
        # CLI shim in environments.py. The Coder server (deployed by
        # deploy/11_coder_server.sh) launches workspace VMs from the template.
        "coder_url": _env_val("coder_url", "coder_url_env") or get("CODER_URL", ""),
        "coder_session_token": _env_val("coder_session_token", "coder_session_token_env")
                                or get_fresh("CODER_SESSION_TOKEN", ""),
        # Workspace VM inputs (passed as Coder template parameters). These reuse
        # the existing thin golden AMI + env instance profile + env SG + subnet
        # baked by deploy/09_dev_env.sh -- no new AWS artifacts per environment.
        "ami_id": _env_val("ami_id", "ami_id_env"),
        "instance_type": e.get("instance_type", "t3.large"),
        "subnet_id": _env_val("subnet_id", "subnet_id_env"),
        "security_group_id": _env_val("security_group_id", "security_group_id_env"),
        "instance_profile": _env_val("instance_profile", "instance_profile_env"),
        "db_name": e.get("db_name", "odoo"),
        # The provenance-baked Odoo image the env runs against the masked DB. A
        # run may override this (result.odoo_image); this is the fallback.
        "odoo_image": odoo_image(),
        # Developer addons repo cloned into the workspace + bind-mounted into the
        # odoo container as live-dev addons. Defaults to the pipeline's custom repo.
        "repo_url": _env_val("repo_url", "repo_url_env") or get("CUSTOM_ADDONS_GIT_URL"),
        "repo_branch": _env_val("repo_branch", "repo_branch_env") or get("CUSTOM_ADDONS_GIT_REF"),
        # Optional Secrets Manager secret holding a GitHub token for cloning a
        # private addons repo on the instance (read by the workspace agent).
        "git_token_secret": _env_val("git_token_secret", "git_token_secret_env"),
    }


def odoo_image() -> str | None:
    """The provenance-baked Odoo image (ECR odoo:<tag>) the dev env runs."""
    e = environments_cfg()
    if e.get("odoo_image"):
        return str(e["odoo_image"])
    name = e.get("odoo_image_env")
    if name and get(name):
        return get(name)
    proj = get("PROJECT")
    region = get("AWS_REGION")
    acct = get("AWS_ACCOUNT_ID")
    tag = e.get("odoo_image_tag", "latest")
    if proj and region and acct:
        return f"{acct}.dkr.ecr.{region}.amazonaws.com/{proj}/odoo:{tag}"
    return None



def environments_configured() -> bool:
    s = environments_settings()
    # The Coder control plane (URL + session token) is required; the AMI/SG/
    # profile/subnet are required to pass to the Coder template as parameters.
    return bool(s["enabled"] and s["coder_url"] and s["coder_session_token"]
                and s["ami_id"] and s["security_group_id"] and s["instance_profile"])

