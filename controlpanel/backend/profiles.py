"""Profile lifecycle: a *profile* binds a specific source system to its matching
provenance (Odoo core ref + addons repo/ref + discovered deps) and the immutable
Odoo image built from that provenance. Mask runs and developer environments are
launched *from* a profile, so the data and the code always match.

Secrets (source DB password, SSH bastion key, git token) are stored in AWS
Secrets Manager and referenced by ARN; only non-secret metadata lives in the
profile's YAML file (profiles/<id>.yaml).
"""
from __future__ import annotations

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


def _resolve_git_token_secret(payload: dict[str, Any], profile_id: str) -> str | None:
    """Resolve the GitHub token secret for a profile.

    Two ways to supply it:
      * ``git_token_secret`` -- an existing Secrets Manager ARN (or secret
        name) to reuse directly, e.g. from another profile or the env secret.
        Avoids minting a duplicate secret.
      * ``git_token`` -- a raw PAT; minted into a new secret under
        ``<prefix>/profile/<id>/git-token``.

    If neither is supplied, returns None (caller should leave the field as-is
    on update, or unset on create)."""
    arn = payload.get("git_token_secret")
    if arn:
        # Accept either a full ARN or a bare secret name.
        return arn
    token = payload.get("git_token")
    if token:
        return _put_secret(f"{_secret_prefix()}/{profile_id}/git-token", token)
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
              "enterprise_source", "odoo_conf_extra", "masking_rules"):
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
    for k in ("source_password_secret", "ssh_key_secret", "git_token_secret"):
        _delete_secret(p.get(k))
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
