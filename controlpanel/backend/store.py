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
            """
        )
        c.commit()


def create_run(run_id: str, operation: str, params: dict[str, Any]) -> None:
    with _write_lock:
        c = _conn()
        c.execute(
            "INSERT INTO runs (id, operation, status, params, created_at) VALUES (?,?,?,?,?)",
            (run_id, operation, "queued", json.dumps(params), time.time()),
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
