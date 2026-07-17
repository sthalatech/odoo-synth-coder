"""YAML-backed profile store.

Each profile is one file under ``profiles/<id>.yaml`` at the repo root. The
file is the single source of truth for a profile: source connection (minus
secrets), provenance, mask inputs, the editable masking plan, the built image,
and S3 refs to the discovery output. Secret *values* stay in AWS Secrets
Manager and are referenced by ARN, exactly as they were under SQLite.

This module exposes the same field names and dict shapes the rest of the
backend expects (``source_conn``, ``mask_inputs``, ``masking_rules``, …), so
``store.py``'s profile functions delegate here and no caller changes.

Field -> YAML layout (the nested form is for human readability):

    source.*            <- source_conn            (host/port/dbname/user/ssh*)
    source.*_secret     <- *_secret               (ARNs)
    mask.*              <- mask_inputs            (mask_profile, neutralize_*, …)
    odoo.*              <- odoo_series/git_url/git_ref
    addons.*            <- addons_git_url/git_ref + needs_enterprise/enterprise_source
    discovery.*         <- discovery_yaml_uri/installed_modules/python_deps/
                            apt_deps/required_config_keys/discovery_hash
    masking_rules       <- masking_rules           (literal block, greenmask YAML)
    odoo_conf_extra     <- odoo_conf_extra
    image.*             <- image_uri/image_status/image_history
    error               <- error
"""
from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any

import yaml

# profiles/ lives at the repo root (one level above controlpanel/).
_PROFILES_DIR = Path(__file__).resolve().parents[2] / "profiles"

_WRITE_LOCK = threading.Lock()

# Fields stored as nested dicts in YAML, for readability. Everything else is a
# top-level scalar/list. The mapping is yaml-key -> store-field-name.
# (We keep the *store* field names as the canonical keys in code, and only
# re-group a handful into nested sections for the file layout.)

# Nested-grouping rules: field -> (section, key). Fields not listed here are
# written as top-level keys with their store name.
_NESTED = {
    # source_conn -> source.*
    "host": ("source", "host"),
    "port": ("source", "port"),
    "dbname": ("source", "dbname"),
    "user": ("source", "user"),
    "ssh_enabled": ("source", "ssh_enabled"),
    "ssh_bastion": ("source", "ssh_bastion"),
    "source_password_secret": ("source", "password_secret"),
    "ssh_key_secret": ("source", "ssh_key_secret"),
    "git_token_secret": ("source", "git_token_secret"),
    # mask_inputs -> mask.*
    "mask_profile": ("mask", "profile"),
    "neutralize_mail": ("mask", "neutralize_mail"),
    "neutralize_fetchmail": ("mask", "neutralize_fetchmail"),
    "neutralize_payment": ("mask", "neutralize_payment"),
    "neutralize_smtp_param": ("mask", "neutralize_smtp_param"),
    "reset_admin_login": ("mask", "reset_admin_login"),
    "gm_jobs": ("mask", "gm_jobs"),
    "produce_dump": ("mask", "produce_dump"),
    "subset_days": ("mask", "subset_days"),
    "exclude_table_data": ("mask", "exclude_table_data"),
    "admin_password": ("mask", "admin_password"),
    # odoo.*
    "odoo_series": ("odoo", "series"),
    "odoo_git_url": ("odoo", "git_url"),
    "odoo_git_ref": ("odoo", "git_ref"),
    # addons.*
    "addons_git_url": ("addons", "git_url"),
    "addons_git_ref": ("addons", "git_ref"),
    "needs_enterprise": ("addons", "needs_enterprise"),
    "enterprise_source": ("addons", "enterprise_source"),
    # discovery.*
    "discovery_yaml_uri": ("discovery", "yaml_uri"),
    "installed_modules": ("discovery", "installed_modules"),
    "python_deps": ("discovery", "python_deps"),
    "apt_deps": ("discovery", "apt_deps"),
    "required_config_keys": ("discovery", "required_config_keys"),
    "discovery_hash": ("discovery", "hash"),
    # image.*
    "image_uri": ("image", "uri"),
    "image_status": ("image", "status"),
    "image_history": ("image", "history"),
}

# Reverse: (section, key) -> store field name.
_NESTED_REV = {v: k for k, v in _NESTED.items()}


class _LiteralDumper(yaml.SafeDumper):
    """SafeDumper that emits multi-line strings as literal block scalars
    (``|``) so masking_rules / odoo_conf_extra stay human-readable, and
    keeps long strings on one line (no folding)."""


def _str_representer(dumper, data):
    if "\n" in data:
        return dumper.represent_scalar("tag:yaml.org,2002:str", data, style="|")
    return dumper.represent_scalar("tag:yaml.org,2002:str", data)


_LiteralDumper.add_representer(str, _str_representer)


def _dump_yaml(doc: dict, fh) -> None:
    yaml.dump(doc, fh, Dumper=_LiteralDumper, default_flow_style=False,
              width=1000, sort_keys=False, allow_unicode=True)


def _load_yaml(text: str) -> dict:
    return yaml.safe_load(text) or {}


def _path(profile_id: str) -> Path:
    return _PROFILES_DIR / f"{profile_id}.yaml"


def _flatten(d: dict[str, Any]) -> dict[str, Any]:
    """source_conn dict -> flat fields for the YAML nested layout."""
    out: dict[str, Any] = {}
    conn = d.pop("source_conn", None)
    if conn:
        for k, v in (conn or {}).items():
            out[k] = v
    mask = d.pop("mask_inputs", None)
    if mask:
        for k, v in (mask or {}).items():
            out[k] = v
    out.update(d)  # remaining scalar/list fields, incl. *_secret ARNs
    return out


def _to_yaml_doc(flat: dict[str, Any]) -> dict[str, Any]:
    """Flat store-fields dict -> nested YAML-ready dict with literal-block
    masking_rules."""
    doc: dict[str, Any] = {}
    for k, v in flat.items():
        if v is None:
            continue
        if k in _NESTED:
            sec, key = _NESTED[k]
            doc.setdefault(sec, {})[key] = v
        else:
            doc[k] = v
    return doc


def _from_yaml_doc(doc: dict[str, Any]) -> dict[str, Any]:
    """Nested YAML dict -> flat store-fields dict (rebuilds source_conn +
    mask_inputs)."""
    out: dict[str, Any] = {}
    source: dict[str, Any] = {}
    mask: dict[str, Any] = {}
    for sec, body in (doc or {}).items():
        if isinstance(body, dict):
            for key, v in body.items():
                field = _NESTED_REV.get((sec, key))
                if field:
                    if sec == "source" and key in ("host", "port", "dbname",
                                                    "user", "ssh_enabled",
                                                    "ssh_bastion"):
                        source[key] = v
                    elif sec == "mask":
                        mask[key] = v
                    else:
                        out[field] = v
                else:
                    # unknown nested key — preserve under a dotted name
                    out[f"{sec}.{key}"] = v
        else:
            out[sec] = body
    if source:
        out["source_conn"] = source
    if mask:
        out["mask_inputs"] = mask
    return out


def _read_raw(profile_id: str) -> dict[str, Any] | None:
    p = _path(profile_id)
    if not p.exists():
        return None
    doc = _load_yaml(p.read_text())
    if doc is None:
        return None
    return _from_yaml_doc(dict(doc))


def _write(profile_id: str, fields: dict[str, Any]) -> None:
    """Merge ``fields`` into the existing profile file (or create it),
    preserving untouched keys + comments. ``masking_rules`` is emitted as a
    literal block scalar."""
    p = _path(profile_id)
    if p.exists():
        doc = _load_yaml(p.read_text()) or {}
    else:
        doc = {}
    # apply updates to the nested structure in-place
    for k, v in fields.items():
        if v is None:
            continue
        if k == "source_conn":
            for ck, cv in (v or {}).items():
                sec, key = _NESTED.get(ck, ("source", ck))
                doc.setdefault("source", {})[key] = cv
        elif k == "mask_inputs":
            for mk, mv in (v or {}).items():
                sec, key = _NESTED.get(mk, ("mask", mk))
                doc.setdefault("mask", {})[key] = mv
        elif k in _NESTED:
            sec, key = _NESTED[k]
            doc.setdefault(sec, {})[key] = v
        else:
            # pyyaml's _LiteralDumper emits multi-line strings (masking_rules,
            # odoo_conf_extra) as literal block scalars automatically.
            doc[k] = v
    _PROFILES_DIR.mkdir(parents=True, exist_ok=True)
    with p.open("w") as fh:
        _dump_yaml(doc, fh)


def create_profile(profile_id: str, label: str, **fields: Any) -> None:
    with _WRITE_LOCK:
        fields = dict(fields)
        fields.setdefault("image_status", "draft")
        now = time.time()
        _write(profile_id, {"id": profile_id, "label": label,
                            "created_at": now, "updated_at": now, **fields})


def update_profile(profile_id: str, **fields: Any) -> None:
    if not fields:
        return
    fields = dict(fields)
    fields["updated_at"] = time.time()
    with _WRITE_LOCK:
        existing = _read_raw(profile_id)
        if existing is None:
            raise KeyError(profile_id)
        _write(profile_id, fields)


def get_profile(profile_id: str) -> dict[str, Any] | None:
    d = _read_raw(profile_id)
    if d is None:
        return None
    d.setdefault("id", profile_id)
    return d


def list_profiles(limit: int = 100) -> list[dict[str, Any]]:
    if not _PROFILES_DIR.exists():
        return []
    out: list[dict[str, Any]] = []
    for p in sorted(_PROFILES_DIR.glob("*.yaml"), key=lambda x: x.stat().st_mtime,
                    reverse=True):
        d = _read_raw(p.stem)
        if d is not None:
            d.setdefault("id", p.stem)
            out.append(d)
        if len(out) >= limit:
            break
    return out


def delete_profile(profile_id: str) -> None:
    with _WRITE_LOCK:
        p = _path(profile_id)
        if p.exists():
            p.unlink()


def profiles_dir() -> Path:
    """Return the profiles directory (created if missing)."""
    _PROFILES_DIR.mkdir(parents=True, exist_ok=True)
    return _PROFILES_DIR
