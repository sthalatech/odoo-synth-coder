"""Persistence facade.

Profiles, runs/logs, and environments each have their own backing store now:

- profiles      -> one YAML file per profile (profile_store.py, profiles/*.yaml)
- runs + logs   -> S3 (run_store.py, s3://bucket/.../runs/<id>/)
- environments  -> one YAML file (env_store.py, lib/backend/envs.yaml),
                   with status/URLs read live from Coder at list time

This module keeps the ``store.*`` names the rest of the backend was written
against (profiles.py, build.py, discovery.py, environments.py, the CLI) and
delegates to the three stores. SQLite is gone.
"""
from __future__ import annotations

from typing import Any, Iterable

from . import env_store as _env_store
from . import profile_store as _profile_store
from . import run_store as _run_store


def init() -> None:
    """No-op. Kept so the CLI's startup call (store.init()) still works; the
    YAML/S3 stores create their backing locations lazily."""
    return None


# --- profiles ---------------------------------------------------------------

def create_profile(profile_id: str, label: str, **fields: Any) -> None:
    _profile_store.create_profile(profile_id, label, **fields)


def update_profile(profile_id: str, **fields: Any) -> None:
    _profile_store.update_profile(profile_id, **fields)


def get_profile(profile_id: str) -> dict[str, Any] | None:
    return _profile_store.get_profile(profile_id)


def list_profiles(limit: int = 100) -> list[dict[str, Any]]:
    return _profile_store.list_profiles(limit=limit)


def delete_profile(profile_id: str) -> None:
    _profile_store.delete_profile(profile_id)


# --- runs + logs ------------------------------------------------------------

def create_run(run_id: str, operation: str, params: dict[str, Any],
               profile_id: str | None = None) -> None:
    _run_store.create_run(run_id, operation, params, profile_id=profile_id)


def update_run(run_id: str, **fields: Any) -> None:
    _run_store.update_run(run_id, **fields)


def append_logs(run_id: str, lines: Iterable[str]) -> int:
    """Append lines; returns the last seq written."""
    return _run_store.append_logs(run_id, lines)


def flush_logs(run_id: str) -> None:
    """Force-flush buffered log lines to S3 (call on run finalization)."""
    _run_store.flush_logs(run_id)


def get_run(run_id: str) -> dict[str, Any] | None:
    return _run_store.get_run(run_id)


def list_runs(limit: int = 50) -> list[dict[str, Any]]:
    return _run_store.list_runs(limit=limit)


def get_logs(run_id: str, after_seq: int = 0) -> list[dict[str, Any]]:
    return _run_store.get_logs(run_id, after_seq=after_seq)


# --- environments -----------------------------------------------------------

def create_environment(env_id: str, source_run_id: str | None, issue: str | None,
                       dump_s3_uri: str | None, repo_url: str | None = None,
                       repo_branch: str | None = None,
                       odoo_image: str | None = None,
                       profile_id: str | None = None) -> None:
    _env_store.create_environment(env_id, source_run_id, issue, dump_s3_uri,
                                  repo_url=repo_url, repo_branch=repo_branch,
                                  odoo_image=odoo_image, profile_id=profile_id)


def update_environment(env_id: str, **fields: Any) -> None:
    _env_store.update_environment(env_id, **fields)


def get_environment(env_id: str) -> dict[str, Any] | None:
    return _env_store.get_environment(env_id)


def list_environments(limit: int = 100) -> list[dict[str, Any]]:
    return _env_store.list_environments(limit=limit)


def delete_environment(env_id: str) -> None:
    _env_store.delete_environment(env_id)


def environments_by_run() -> dict[str, dict[str, Any]]:
    """Map source_run_id -> latest non-terminated environment for that run."""
    return _env_store.environments_by_run()
