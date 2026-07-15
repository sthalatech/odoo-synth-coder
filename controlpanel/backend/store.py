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
    with _write_lock:
        c = _conn()
        c.execute(
            "INSERT INTO runs (id, operation, status, params, profile_id, created_at) "
            "VALUES (?,?,?,?,?,?)",
            (run_id, operation, "queued", json.dumps(params), profile_id, time.time()),
        )
        c.commit()


def update_run(run_id: str, **fields: Any) -> None:
    if not fields:
        return
    cols = ", ".join(f"{k}=?" for k in fields)
    vals = [json.dumps(v) if k == "result" else v for k, v in fields.items()]
    with _write_lock:
        c = _conn()
        c.execute(f"UPDATE runs SET {cols} WHERE id=?", (*vals, run_id))
        c.commit()


def append_logs(run_id: str, lines: Iterable[str]) -> int:
    """Append lines; returns the last seq written."""
    with _write_lock:
        c = _conn()
        row = c.execute(
            "SELECT COALESCE(MAX(seq), 0) AS m FROM logs WHERE run_id=?", (run_id,)
        ).fetchone()
        seq = row["m"]
        now = time.time()
        batch = []
        for ln in lines:
            seq += 1
            batch.append((run_id, seq, now, ln))
        if batch:
            c.executemany(
                "INSERT OR IGNORE INTO logs (run_id, seq, ts, line) VALUES (?,?,?,?)",
                batch,
            )
            c.commit()
        return seq


def get_run(run_id: str) -> dict[str, Any] | None:
    row = _conn().execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
    if not row:
        return None
    d = dict(row)
    d["params"] = json.loads(d["params"]) if d["params"] else {}
    d["result"] = json.loads(d["result"]) if d["result"] else None
    return d


def list_runs(limit: int = 50) -> list[dict[str, Any]]:
    rows = _conn().execute(
        "SELECT id, operation, status, exit_code, created_at, started_at, finished_at, result "
        "FROM runs ORDER BY created_at DESC LIMIT ?",
        (limit,),
    ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["result"] = json.loads(d["result"]) if d.get("result") else None
        out.append(d)
    return out


def get_logs(run_id: str, after_seq: int = 0) -> list[dict[str, Any]]:
    rows = _conn().execute(
        "SELECT seq, ts, line FROM logs WHERE run_id=? AND seq>? ORDER BY seq",
        (run_id, after_seq),
    ).fetchall()
    return [dict(r) for r in rows]


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

# JSON-encoded columns on the profiles table.
_PROFILE_JSON_COLS = {
    "source_conn", "mask_inputs", "python_deps", "apt_deps",
    "installed_modules", "image_history", "required_config_keys",
}


def _profile_row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    d = dict(row)
    for k in _PROFILE_JSON_COLS:
        if d.get(k):
            try:
                d[k] = json.loads(d[k])
            except (ValueError, TypeError):
                d[k] = None
    return d


def create_profile(profile_id: str, label: str, **fields: Any) -> None:
    fields.setdefault("image_status", "draft")
    for k in list(fields):
        if k in _PROFILE_JSON_COLS and fields[k] is not None:
            fields[k] = json.dumps(fields[k])
    keys = ["id", "label", "created_at", "updated_at", *fields.keys()]
    vals = [profile_id, label, time.time(), time.time(), *fields.values()]
    placeholders = ",".join("?" for _ in keys)
    with _write_lock:
        c = _conn()
        c.execute(
            f"INSERT INTO profiles ({','.join(keys)}) VALUES ({placeholders})",
            vals,
        )
        c.commit()


def update_profile(profile_id: str, **fields: Any) -> None:
    if not fields:
        return
    for k in list(fields):
        if k in _PROFILE_JSON_COLS and fields[k] is not None:
            fields[k] = json.dumps(fields[k])
    fields["updated_at"] = time.time()
    cols = ", ".join(f"{k}=?" for k in fields)
    vals = list(fields.values())
    with _write_lock:
        c = _conn()
        c.execute(f"UPDATE profiles SET {cols} WHERE id=?", (*vals, profile_id))
        c.commit()


def get_profile(profile_id: str) -> dict[str, Any] | None:
    row = _conn().execute(
        "SELECT * FROM profiles WHERE id=?", (profile_id,)
    ).fetchone()
    return _profile_row_to_dict(row) if row else None


def list_profiles(limit: int = 100) -> list[dict[str, Any]]:
    rows = _conn().execute(
        "SELECT * FROM profiles ORDER BY created_at DESC LIMIT ?", (limit,)
    ).fetchall()
    return [_profile_row_to_dict(r) for r in rows]


def delete_profile(profile_id: str) -> None:
    with _write_lock:
        c = _conn()
        c.execute("DELETE FROM profiles WHERE id=?", (profile_id,))
        c.commit()

