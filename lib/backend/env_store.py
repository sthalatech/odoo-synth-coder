"""YAML-backed environment linkage store.

A developer environment is a Coder workspace; Coder owns its lifecycle
(status, EC2 instance, apps, URLs). This file holds the *linkage* Coder cannot:
which profile + which mask run + which masked dump + which addons repo seeded
a given workspace, plus the ARN of the per-env code-server/Odoo password secret.

One file: ``lib/backend/envs.yaml`` (gitignored — contains secret
ARNs + source hostnames). It's a dict keyed by env_id (= the Coder workspace
name), so ``env list`` joins it with a live ``coder list -a`` to show status +
URLs without a database.

Replaces the SQLite ``environments`` table. Status/odoo_url/vscode_url are NOT
stored here — they're read live from Coder by ``environments.reconcile()``.
"""
from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any

import yaml

_ENVS_PATH = Path(__file__).resolve().parent / "envs.yaml"
_LOCK = threading.Lock()


class _LiteralDumper(yaml.SafeDumper):
    pass


def _str_representer(dumper, data):
    if "\n" in data:
        return dumper.represent_scalar("tag:yaml.org,2002:str", data, style="|")
    return dumper.represent_scalar("tag:yaml.org,2002:str", data)


_LiteralDumper.add_representer(str, _str_representer)


def _load_yaml(text: str) -> dict:
    return yaml.safe_load(text) or {}


def _dump_yaml(doc: dict, fh) -> None:
    yaml.dump(doc, fh, Dumper=_LiteralDumper, default_flow_style=False,
              width=1000, sort_keys=False, allow_unicode=True)


def _read() -> dict[str, dict[str, Any]]:
    if not _ENVS_PATH.exists():
        return {}
    doc = _load_yaml(_ENVS_PATH.read_text())
    return dict(doc)


def _write(envs: dict[str, dict[str, Any]]) -> None:
    with _ENVS_PATH.open("w") as fh:
        _dump_yaml(envs, fh)


def create_environment(env_id: str, source_run_id: str | None, issue: str | None,
                       dump_s3_uri: str | None, repo_url: str | None = None,
                       repo_branch: str | None = None,
                       odoo_image: str | None = None,
                       profile_id: str | None = None) -> None:
    with _LOCK:
        envs = _read()
        envs[env_id] = {
            "workspace_name": env_id,
            "source_run_id": source_run_id,
            "issue": issue,
            "dump_s3_uri": dump_s3_uri,
            "repo_url": repo_url,
            "repo_branch": repo_branch,
            "odoo_image": odoo_image,
            "profile_id": profile_id,
            "password_secret": None,
            "status": "pending",
            "created_at": time.time(),
            "updated_at": time.time(),
        }
        _write(envs)


def update_environment(env_id: str, **fields: Any) -> None:
    if not fields:
        return
    with _LOCK:
        envs = _read()
        if env_id not in envs:
            return
        envs[env_id].update(fields)
        envs[env_id]["updated_at"] = time.time()
        _write(envs)


def get_environment(env_id: str) -> dict[str, Any] | None:
    envs = _read()
    e = envs.get(env_id)
    if e:
        return {"id": env_id, **e}
    return None


def list_environments(limit: int = 100) -> list[dict[str, Any]]:
    envs = _read()
    out = [{"id": k, **v} for k, v in envs.items()]
    out.sort(key=lambda e: e.get("created_at", 0), reverse=True)
    return out[:limit]


def delete_environment(env_id: str) -> None:
    with _LOCK:
        envs = _read()
        envs.pop(env_id, None)
        _write(envs)


def environments_by_run() -> dict[str, dict[str, Any]]:
    """Map source_run_id -> latest non-terminated environment for that run."""
    out: dict[str, dict[str, Any]] = {}
    for e in list_environments():
        rid = e.get("source_run_id")
        if not rid or e.get("status") == "terminated":
            continue
        out.setdefault(rid, e)
    return out
