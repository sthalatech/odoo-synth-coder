"""SQLite-backed persistence for masking runs and their log lines. Keeps history
across restarts and lets the SSE endpoint replay a run's log from any cursor.
"""
from __future__ import annotations
import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Iterable

DB_PATH = Path(__file__).resolve().parent / "controlpanel.db"

_local = threading.local()
_write_lock = threading.Lock()


def _conn() -> sqlite3.Connection:
    c = getattr(_local, "conn", None)
    if c is None:
        c = sqlite3.connect(DB_PATH, check_same_thread=False)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        _local.conn = c
    return c


def init() -> None:
    with _write_lock:
        c = _conn()
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS runs (
              id           TEXT PRIMARY KEY,
              operation    TEXT NOT NULL,
              status       TEXT NOT NULL,        -- queued|running|succeeded|failed
              params       TEXT NOT NULL,        -- json of user inputs (secrets redacted)
              task_arn     TEXT,
              exit_code    INTEGER,
              result       TEXT,                 -- json (urls, counts, error)
              created_at   REAL NOT NULL,
              started_at   REAL,
              finished_at  REAL
            );
            CREATE TABLE IF NOT EXISTS logs (
              run_id   TEXT NOT NULL,
              seq      INTEGER NOT NULL,
              ts       REAL NOT NULL,
              line     TEXT NOT NULL,
              PRIMARY KEY (run_id, seq)
            );
            CREATE TABLE IF NOT EXISTS environments (
              id            TEXT PRIMARY KEY,
              source_run_id TEXT,               -- mask run whose dump seeds this env
              issue         TEXT,                -- github issue ref (optional)
              dump_s3_uri   TEXT,                -- s3://bucket/key of the masked dump
              repo_url      TEXT,                -- addons repo cloned + live-mounted
              repo_branch   TEXT,
              odoo_image    TEXT,                -- provenance-baked odoo image the env runs
              instance_id   TEXT,
              public_ip     TEXT,
              status        TEXT NOT NULL,       -- pending|provisioning|running|terminated|failed
              vscode_url    TEXT,
              odoo_url      TEXT,                -- masked odoo (http://<ip>:<odoo_port>)
              secret_arn    TEXT,                -- Secrets Manager arn of the code-server password
              error         TEXT,
              created_at    REAL NOT NULL,
              updated_at    REAL
            );
            CREATE TABLE IF NOT EXISTS profiles (
              id                     TEXT PRIMARY KEY,
              label                  TEXT NOT NULL,
              description            TEXT,
              -- source connection (non-secret parts) as json: host/port/dbname/
              -- user/ssh_enabled/ssh_bastion. Secrets live in Secrets Manager.
              source_conn            TEXT,
              source_password_secret TEXT,       -- ARN
              ssh_key_secret         TEXT,       -- ARN (optional bastion key)
              git_token_secret       TEXT,       -- ARN (private addons clone)
              -- saved mask inputs as json (mask_profile, neutralize_*, gm_jobs,
              -- reset_admin_login) so they are not re-entered per run.
              mask_inputs            TEXT,
              -- provenance (2a: odoo_git_ref is a manual field; discovery only
              -- suggests series + dump date).
              odoo_series            TEXT,
              odoo_git_url           TEXT,
              odoo_git_ref           TEXT,
              addons_git_url         TEXT,
              addons_git_ref         TEXT,
              needs_enterprise       INTEGER DEFAULT 0,   -- indicator
              enterprise_source      TEXT,                -- per-profile enterprise ref
              -- discovered artifacts (json lists)
              python_deps            TEXT,
              apt_deps               TEXT,
              installed_modules      TEXT,
              required_config_keys   TEXT,       -- json list of odoo.conf keys addons read
              odoo_conf_extra        TEXT,       -- extra odoo.conf lines injected into dev envs
              masking_rules          TEXT,       -- editable per-source masking plan (yaml)
              discovery_yaml_uri     TEXT,       -- s3://.../discovery.yaml
              -- built image + lifecycle
              image_uri              TEXT,       -- current immutable ECR tag
              image_status           TEXT,       -- draft|discovering|discovered|building|ready|failed
              image_history          TEXT,       -- json list of prior {uri,hash,created_at}
              error                  TEXT,
              created_at             REAL NOT NULL,
              updated_at             REAL
            );
            """
        )
        _migrate(c)
        c.commit()


def _migrate(c: sqlite3.Connection) -> None:
    """Additive, idempotent schema migrations (SQLite has no ADD COLUMN IF NOT
    EXISTS, so we check pragma first)."""
    def cols(table: str) -> set[str]:
        return {r["name"] for r in c.execute(f"PRAGMA table_info({table})").fetchall()}

    run_cols = cols("runs")
    if "profile_id" not in run_cols:
        c.execute("ALTER TABLE runs ADD COLUMN profile_id TEXT")
    env_cols = cols("environments")
    if "profile_id" not in env_cols:
        c.execute("ALTER TABLE environments ADD COLUMN profile_id TEXT")
    prof_cols = cols("profiles")
    if "discovery_hash" not in prof_cols:
        c.execute("ALTER TABLE profiles ADD COLUMN discovery_hash TEXT")
    if "required_config_keys" not in prof_cols:
        c.execute("ALTER TABLE profiles ADD COLUMN required_config_keys TEXT")
    if "odoo_conf_extra" not in prof_cols:
        c.execute("ALTER TABLE profiles ADD COLUMN odoo_conf_extra TEXT")
    if "masking_rules" not in prof_cols:
        c.execute("ALTER TABLE profiles ADD COLUMN masking_rules TEXT")
    if "vscode_remote_url" not in env_cols:
        c.execute("ALTER TABLE environments ADD COLUMN vscode_remote_url TEXT")
    if "allow_ip" not in env_cols:
        c.execute("ALTER TABLE environments ADD COLUMN allow_ip TEXT")
    if "workspace_name" not in env_cols:
        c.execute("ALTER TABLE environments ADD COLUMN workspace_name TEXT")
    if "password" not in env_cols:
        c.execute("ALTER TABLE environments ADD COLUMN password TEXT")



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


# ---------------------------------------------------------------------------
# environments (developer VS Code environments seeded from a masked dump)
# ---------------------------------------------------------------------------

def create_environment(env_id: str, source_run_id: str | None, issue: str | None,
                       dump_s3_uri: str | None, repo_url: str | None = None,
                       repo_branch: str | None = None,
                       odoo_image: str | None = None,
                       profile_id: str | None = None) -> None:
    with _write_lock:
        c = _conn()
        c.execute(
            "INSERT INTO environments (id, source_run_id, issue, dump_s3_uri, "
            "repo_url, repo_branch, odoo_image, profile_id, status, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (env_id, source_run_id, issue, dump_s3_uri, repo_url, repo_branch,
             odoo_image, profile_id, "pending", time.time(), time.time()),
        )
        c.commit()


def update_environment(env_id: str, **fields: Any) -> None:
    if not fields:
        return
    fields["updated_at"] = time.time()
    cols = ", ".join(f"{k}=?" for k in fields)
    vals = list(fields.values())
    with _write_lock:
        c = _conn()
        c.execute(f"UPDATE environments SET {cols} WHERE id=?", (*vals, env_id))
        c.commit()


def get_environment(env_id: str) -> dict[str, Any] | None:
    row = _conn().execute("SELECT * FROM environments WHERE id=?", (env_id,)).fetchone()
    return dict(row) if row else None


def list_environments(limit: int = 100) -> list[dict[str, Any]]:
    rows = _conn().execute(
        "SELECT * FROM environments ORDER BY created_at DESC LIMIT ?", (limit,)
    ).fetchall()
    return [dict(r) for r in rows]


def environments_by_run() -> dict[str, dict[str, Any]]:
    """Map source_run_id -> latest non-terminated environment for that run."""
    out: dict[str, dict[str, Any]] = {}
    for e in list_environments():
        rid = e.get("source_run_id")
        if not rid or e.get("status") == "terminated":
            continue
        out.setdefault(rid, e)
    return out


# ---------------------------------------------------------------------------
# profiles (a source system bound to its matching provenance code + image)
# ---------------------------------------------------------------------------
#
# Profiles are now stored as one YAML file per profile under profiles/
# (see profile_store.py). These functions are thin delegates so callers
# (profiles.py, build.py, discovery.py, the CLI) keep using store.* unchanged.
# The JSON-column handling that lived here under SQLite is gone — the YAML
# store reads/writes native dict/list/string fields directly.

from . import profile_store as _profile_store  # noqa: E402
from . import run_store as _run_store  # noqa: E402


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

