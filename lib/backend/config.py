"""Configuration loader: reads the repo's config.yaml (single source of truth)
and deploy/state.env so the backend uses the
exact same infra values as the shell pipeline. No values are duplicated here.

config.yaml also carries the structured sections the old lib/config.yml
held (destination, mask_profiles, neutralize_defaults, environments) — now
consolidated into the one file. ``panel()`` returns those sections as a dict.
"""
from __future__ import annotations
import os
from pathlib import Path
from functools import lru_cache

import yaml

# lib/backend/config.py -> repo root is two levels up
REPO_ROOT = Path(__file__).resolve().parents[2]
PANEL_DIR = Path(__file__).resolve().parents[1]
YAML_CONFIG = REPO_ROOT / "config.yaml"
LEGACY_PANEL_CONFIG = PANEL_DIR / "config.yml"


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
    """Load config.yaml into a flat {ENV_VAR: value} dict in-process.

    Shares one loader with deploy/_yaml_to_env.py (lib/backend/yamlconfig.py)
    so the Python backend and the shell pipeline read identical values.
    Resolves ref:env: / ref:ssm: secret forms. Raises FileNotFoundError if
    *path* is missing -- load() treats a missing config.yaml as a hard error,
    which is what we want (the old subprocess path silently returned {} and
    produced an empty config that failed opaquely downstream).
    """
    from . import yamlconfig
    return yamlconfig.load_env_dict(path)


@lru_cache(maxsize=1)
def load() -> dict[str, str]:
    """config.yaml is the base; state.env (created by the deploy scripts)
    overlays the resolved AWS resource ids/endpoints. Real OS env wins over
    both so the container can be reconfigured without editing files."""
    cfg: dict[str, str] = {}
    yaml_path = REPO_ROOT / "config.yaml"
    if not yaml_path.exists():
        raise RuntimeError(
            "config.yaml not found at repo root. Copy config.example.yaml to "
            "config.yaml and fill it in (see the README).")
    cfg.update(_parse_yaml_env(yaml_path))
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
    """Same as load() but WITHOUT the lru_cache — re-reads config.yaml +
    state.env from disk on every call. Used for secrets so a value rotated on
    disk takes effect on the next run without restarting the panel."""
    cfg: dict[str, str] = {}
    yaml_path = REPO_ROOT / "config.yaml"
    if not yaml_path.exists():
        raise RuntimeError(
            "config.yaml not found at repo root. Copy config.example.yaml to "
            "config.yaml and fill it in (see the README).")
    cfg.update(_parse_yaml_env(yaml_path))
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
# structured panel sections (destination, mask profiles, neutralize defaults,
# environments) — now consolidated into config.yaml. Falls back to the legacy
# lib/config.yml for back-compat if config.yaml has no such section.
# ---------------------------------------------------------------------------

@lru_cache(maxsize=1)
def _yaml_doc() -> dict:
    if YAML_CONFIG.exists():
        try:
            return yaml.safe_load(YAML_CONFIG.read_text()) or {}
        except Exception:  # noqa: BLE001 — malformed yaml shouldn't crash config
            return {}
    return {}


@lru_cache(maxsize=1)
def panel() -> dict:
    """The structured config sections. config.yaml is the source of truth; the
    legacy lib/config.yml overlays only keys not present in config.yaml
    (so an old config.yml still works, but config.yaml wins).

    Normalizes the mask-related keys to a flat top-level shape
    (``mask_profiles``, ``neutralize_defaults``, ``reset_admin_login``) whether
    they are authored nested under ``mask:`` (the documented example form) or at
    the top level (the older config.yml form)."""
    doc = _yaml_doc()
    out: dict = {}
    for k in ("destination", "mask_profiles", "neutralize_defaults",
              "reset_admin_login", "environments", "aws"):
        if k in doc:
            out[k] = doc[k]
    # nested-under-mask normalization (config.example.yaml form)
    mask = doc.get("mask", {}) or {}
    if "profiles" in mask:
        out.setdefault("mask_profiles", mask["profiles"])
    if "neutralize_defaults" in mask:
        out.setdefault("neutralize_defaults", mask["neutralize_defaults"])
    if "reset_admin_login" in mask:
        out.setdefault("reset_admin_login", mask["reset_admin_login"])
    if "gm_jobs" in mask and isinstance(out.get("neutralize_defaults"), dict):
        out["neutralize_defaults"].setdefault("gm_jobs", mask["gm_jobs"])
    # legacy overlay (only keys missing from config.yaml)
    if LEGACY_PANEL_CONFIG.exists():
        try:
            legacy = yaml.safe_load(LEGACY_PANEL_CONFIG.read_text()) or {}
        except Exception:  # noqa: BLE001
            legacy = {}
        for k, v in legacy.items():
            out.setdefault(k, v)
    return out


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
    """The masked destination DB creds (user/password/dbname). RDS-free (Phase
    B): the masker runs a throwaway in-task postgres and the host is supplied
    by the task (127.0.0.1), so `host` is typically empty here. Returns an
    empty host when no host env var is set (resilient)."""
    d = panel().get("destination", {}) or {}
    return _resolve_conn(d)


def mask_profiles() -> list[dict]:
    return panel().get("mask_profiles", []) or []


def neutralize_defaults() -> dict:
    return panel().get("neutralize_defaults", {}) or {}


def dump_s3_bucket() -> str | None:
    """The S3 bucket for masked dumps + build/discovery artifacts.

    config.yaml is the source of truth: ``mask.dumps_bucket`` (a direct value).
    Falls back to the legacy ``aws.dump_s3_bucket[_env]`` panel keys, then the
    resolved ``DUMP_S3_BUCKET`` env var (set by the YAML loader's alias).
    """
    doc = _yaml_doc()
    mask = doc.get("mask", {}) or {}
    if mask.get("dumps_bucket"):
        return str(mask["dumps_bucket"])
    aws = panel().get("aws", {}) or {}
    if aws.get("dump_s3_bucket"):
        return str(aws["dump_s3_bucket"])
    env_name = aws.get("dump_s3_bucket_env")
    if env_name:
        return get(env_name)
    return get("DUMP_S3_BUCKET")


def dump_s3_prefix() -> str:
    doc = _yaml_doc()
    mask = doc.get("mask", {}) or {}
    if mask.get("dumps_prefix"):
        return str(mask["dumps_prefix"]).strip("/")
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

