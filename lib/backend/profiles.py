"""Profile lifecycle: a *profile* binds a specific source system to its matching
provenance (Odoo core ref + addons repo/ref + discovered deps) and the immutable
Odoo image built from that provenance. Mask runs and developer environments are
launched *from* a profile, so the data and the code always match.

Secrets:
  * source DB password + SSH bastion key -> AWS Secrets Manager (referenced by
    ARN; only non-secret metadata lives in the profile's YAML file).
  * GitHub token (for cloning the private addons repo) -> a Coder *user secret*
    named ``git-token-<profile_id>`` with a per-profile env-var target
    ``GH_PAT_<UPPER_ID>``. Coder injects it into every workspace the owner
    launches, so discover / build / env-launch all read it from the workspace
    env with no AWS Secrets Manager round-trip and no plaintext in the profile
    or in template parameters. Only the secret *name* is persisted in the
    profile (``git_token_secret`` field); the value is write-only in Coder.
"""
from __future__ import annotations

import subprocess
import uuid
from typing import Any, Optional
from urllib.parse import quote

import boto3

from . import config, store
from .pipeline import parse_dsn


def _region() -> str:
    return config.require("AWS_REGION")


def _secret_prefix() -> str:
    e = config.environments_cfg() if hasattr(config, "environments_cfg") else {}
    return (e.get("secret_prefix") or "odoo-synth/env").rsplit("/", 1)[0] + "/profile"


def _is_profile_scoped_secret(arn: Optional[str]) -> bool:
    """True if a secret ARN/name was minted by this profile store (and is thus
    safe for `profile delete` to delete). Secrets outside the profile prefix
    -- e.g. a reused `odoo-synth/env/git-token` passed via `--git-token-secret`
    -- are owned elsewhere and must NOT be deleted here."""
    if not arn:
        return False
    name = arn.split(":", 6)[-1] if arn.startswith("arn:") else arn
    return name.startswith(_secret_prefix() + "/")


def _put_secret(name: str, value: str) -> str:
    """Create-or-update a Secrets Manager secret; return its ARN."""
    sm = boto3.client("secretsmanager", region_name=_region())
    try:
        resp = sm.create_secret(Name=name, SecretString=value,
                                Description="odoo-synth profile secret")
        return resp["ARN"]
    except sm.exceptions.ResourceExistsException:
        sm.put_secret_value(SecretId=name, SecretString=value)
        return sm.describe_secret(SecretId=name)["ARN"]


def _coder_git_token_name(profile_id: str) -> str:
    """The Coder user-secret name for a profile's GitHub token."""
    return f"git-token-{profile_id}"


def _coder_git_token_env(profile_id: str) -> str:
    """The per-profile env-var target Coder injects the token under.

    One unique env var per profile avoids collisions, since Coder user secrets
    are per-user global (a single user owns every workspace, so two profiles
    can't both own the same var -- last write would win). The name must NOT
    start with ``GIT_`` (Coder reserves ``GIT_*`` env vars), so we use
    ``GH_PAT_<UPPER_ID>``. Workspaces re-export it as ``GIT_TOKEN`` for the
    container / git clone (via ``${!GIT_TOKEN_ENV}`` indirection)."""
    return "GH_PAT_" + profile_id.upper().replace("-", "_")


def _coder_env() -> dict[str, str]:
    """Env for shelling out to the coder CLI (CODER_URL + session token)."""
    import os
    from .pipeline import _coder_env as _pipeline_coder_env
    return {**os.environ, **_pipeline_coder_env()}


def _put_coder_git_token(profile_id: str, token: str) -> str:
    """Create-or-update the profile's Coder user secret holding the GitHub PAT.

    The token is injected into every workspace the owner launches as
    ``$GH_PAT_<UPPER_ID>``. Returns the secret *name* (not an ARN) so it can
    be stored in the profile and deleted later. The value is write-only in
    Coder -- it cannot be read back via the CLI/API."""
    name = _coder_git_token_name(profile_id)
    env_target = _coder_git_token_env(profile_id)
    # create; if it already exists, fall back to update.
    create = subprocess.run(
        ["coder", "secret", "create", name,
         "--description", f"odoo-synth git token for profile {profile_id}",
         "--env", env_target, "--value", token],
        env=_coder_env(), capture_output=True, text=True, timeout=30)
    if create.returncode == 0:
        return name
    # exists -> update the value + env target
    update = subprocess.run(
        ["coder", "secret", "update", name, "--env", env_target, "--value", token],
        env=_coder_env(), capture_output=True, text=True, timeout=30)
    if update.returncode != 0:
        raise RuntimeError(
            f"could not create/update Coder secret {name!r}: "
            f"create rc={create.returncode} ({create.stderr.strip()}); "
            f"update rc={update.returncode} ({update.stderr.strip()})")
    return name


def _resolve_git_token_secret(payload: dict[str, Any], profile_id: str) -> str | None:
    """Mint the profile's GitHub token into a Coder user secret.

    ``git_token`` -- a raw PAT; stored as the Coder user secret
    ``git-token-<profile_id>`` (injected into workspaces as
    ``$GH_PAT_<UPPER_ID>``). Returns the secret *name* (persisted in the
    profile's ``git_token_secret`` field). If no token is supplied, returns
    None (caller leaves the field as-is on update, or unset on create)."""
    token = payload.get("git_token")
    if token:
        return _put_coder_git_token(profile_id, token)
    return None


def _delete_secret(arn: Optional[str]) -> None:
    if not arn:
        return
    # Only delete secrets this profile store created. A secret passed in via
    # --git-token-secret (e.g. the shared odoo-synth/env/git-token) is owned by
    # the env/deploy layer and must survive a profile delete.
    if not _is_profile_scoped_secret(arn):
        return
    try:
        boto3.client("secretsmanager", region_name=_region()).delete_secret(
            SecretId=arn, ForceDeleteWithoutRecovery=True)
    except Exception:  # noqa: BLE001
        pass


def _get_secret(arn: Optional[str]) -> str:
    if not arn:
        return ""
    sm = boto3.client("secretsmanager", region_name=_region())
    return sm.get_secret_value(SecretId=arn).get("SecretString", "")


# ---------------------------------------------------------------------------
# CRUD orchestration
# ---------------------------------------------------------------------------

def create(payload: dict[str, Any]) -> str:
    """Create a profile from a form payload. Secrets are extracted from the
    payload, written to Secrets Manager, and only their ARNs are persisted."""
    profile_id = payload.get("id") or ("prof_" + uuid.uuid4().hex[:8])
    label = payload.get("label") or profile_id

    fields: dict[str, Any] = {
        "description": payload.get("description"),
        "odoo_series": payload.get("odoo_series"),
        "odoo_git_url": payload.get("odoo_git_url") or "https://github.com/odoo/odoo",
        "odoo_git_ref": payload.get("odoo_git_ref"),          # manual (decision 2a)
        "addons_git_url": payload.get("addons_git_url"),
        "addons_git_ref": payload.get("addons_git_ref"),
        "needs_enterprise": 1 if payload.get("needs_enterprise") else 0,
        "enterprise_source": payload.get("enterprise_source"),
        "pr_base": payload.get("pr_base"),
        "mask_inputs": _mask_inputs(payload),
        "image_status": "draft",
    }

    # source connection: split DSN into non-secret conn + password secret
    dsn = payload.get("source_dsn")
    if dsn:
        p = parse_dsn(dsn)
        fields["source_conn"] = {
            "host": p["host"], "port": p["port"], "dbname": p["dbname"],
            "user": p["user"],
            "ssh_enabled": bool(payload.get("ssh_enabled")),
            "ssh_bastion": payload.get("ssh_bastion"),
        }
        if p["password"]:
            fields["source_password_secret"] = _put_secret(
                f"{_secret_prefix()}/{profile_id}/source-password", p["password"])

    if payload.get("ssh_key"):
        fields["ssh_key_secret"] = _put_secret(
            f"{_secret_prefix()}/{profile_id}/ssh-key", payload["ssh_key"])
    fields["git_token_secret"] = _resolve_git_token_secret(payload, profile_id)

    store.create_profile(profile_id, label, **fields)
    return profile_id


def update(profile_id: str, payload: dict[str, Any]) -> None:
    existing = store.get_profile(profile_id)
    if not existing:
        raise KeyError(profile_id)

    fields: dict[str, Any] = {}
    for k in ("label", "description", "odoo_series", "odoo_git_url",
              "odoo_git_ref", "addons_git_url", "addons_git_ref",
              "enterprise_source", "odoo_conf_extra", "masking_rules",
              "agent_name", "agent_system_prompt", "pr_base"):
        if k in payload:
            fields[k] = payload[k]
    if "needs_enterprise" in payload:
        fields["needs_enterprise"] = 1 if payload["needs_enterprise"] else 0
    if any(k in payload for k in _MASK_KEYS):
        merged = dict(existing.get("mask_inputs") or {})
        merged.update(_mask_inputs(payload))
        fields["mask_inputs"] = merged

    dsn = payload.get("source_dsn")
    if dsn:
        p = parse_dsn(dsn)
        fields["source_conn"] = {
            "host": p["host"], "port": p["port"], "dbname": p["dbname"],
            "user": p["user"],
            "ssh_enabled": bool(payload.get("ssh_enabled",
                                            (existing.get("source_conn") or {}).get("ssh_enabled"))),
            "ssh_bastion": payload.get("ssh_bastion",
                                       (existing.get("source_conn") or {}).get("ssh_bastion")),
        }
        if p["password"]:
            fields["source_password_secret"] = _put_secret(
                f"{_secret_prefix()}/{profile_id}/source-password", p["password"])
    if payload.get("ssh_key"):
        fields["ssh_key_secret"] = _put_secret(
            f"{_secret_prefix()}/{profile_id}/ssh-key", payload["ssh_key"])
    fields["git_token_secret"] = _resolve_git_token_secret(payload, profile_id)

    store.update_profile(profile_id, **fields)


def delete(profile_id: str) -> None:
    p = store.get_profile(profile_id)
    if not p:
        return
    # AWS Secrets Manager: source DB password + SSH key.
    for k in ("source_password_secret", "ssh_key_secret"):
        _delete_secret(p.get(k))
    # The profile's Coder user secret (git-token-<id>) is left in place --
    # it is write-only, harmless, and deletable manually if desired
    # (`coder secret delete git-token-<profile_id>`).
    store.delete_profile(profile_id)


def public_view(p: dict[str, Any]) -> dict[str, Any]:
    """A profile dict safe to return to the UI: secret ARNs replaced by booleans."""
    d = dict(p)
    for k in ("source_password_secret", "ssh_key_secret", "git_token_secret"):
        d[k + "_set"] = bool(d.pop(k, None))
    return d


def run_params(profile_id: str, overrides: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """Reconstruct the pipeline params for a mask run from a saved profile.

    Fetches the source password + SSH key from Secrets Manager and rebuilds the
    source DSN and mask inputs so the pipeline can run unchanged. ``overrides``
    may carry per-run knobs (e.g. ``produce_dump``) supplied at launch time.
    """
    p = store.get_profile(profile_id)
    if not p:
        raise KeyError(profile_id)

    conn = p.get("source_conn") or {}
    if not conn.get("host"):
        raise ValueError("profile has no source connection configured")
    password = _get_secret(p.get("source_password_secret"))
    user = quote(conn.get("user") or "postgres", safe="")
    auth = f"{user}:{quote(password, safe='')}@" if password else f"{user}@"
    source_dsn = (f"postgresql://{auth}{conn['host']}:{conn.get('port', 5432)}"
                  f"/{conn.get('dbname', '')}")

    params: dict[str, Any] = {
        "operation": "mask",
        "source_dsn": source_dsn,
        "ssh_enabled": bool(conn.get("ssh_enabled")),
        "ssh_bastion": conn.get("ssh_bastion"),
        "ssh_key": _get_secret(p.get("ssh_key_secret")) if conn.get("ssh_enabled") else None,
        # Provenance: the profile's immutable build. The pipeline records this on
        # the run result so an environment seeded from the run runs identical
        # code to the masked data.
        "odoo_image": p.get("image_uri"),
        # Editable per-source greenmask profile (generated during discovery).
        # When present the pipeline uploads it to S3 and points the masker at it
        # instead of the baked profile.
        "mask_rules": p.get("masking_rules") or "",
    }
    params.update(p.get("mask_inputs") or {})
    if overrides:
        params.update({k: v for k, v in overrides.items() if v is not None})
    return params


# ---------------------------------------------------------------------------

_MASK_KEYS = (
    "mask_profile", "admin_password", "gm_jobs", "neutralize_mail",
    "neutralize_fetchmail", "neutralize_payment", "neutralize_smtp_param",
    "reset_admin_login", "produce_dump", "subset_days", "exclude_table_data",
)


def _mask_inputs(payload: dict[str, Any]) -> dict[str, Any]:
    return {k: payload[k] for k in _MASK_KEYS if k in payload and payload[k] is not None}


def git_token_env_name(profile_id: str) -> str:
    """The env var Coder injects the profile's git token under (GH_PAT_<ID>).

    Used by the discover/build/env-launch paths to tell each workspace which
    Coder-injected env var holds this profile's token.
    """
    return _coder_git_token_env(profile_id)
