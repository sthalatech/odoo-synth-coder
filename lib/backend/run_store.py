"""S3-backed run + log store.

Replaces the SQLite ``runs``/``logs`` tables. Each run is one S3 prefix:

    s3://<dumps_bucket>/<dumps_prefix>/runs/<run_id>/
        index.json   # manifest entry (also appended to runs/index.json)
        params.json   # run record: id, operation, status, params, profile_id,
                      # created_at, started_at, exit_code, result, finished_at
        log.txt       # streamed stdout, newline-delimited

S3 is the source of truth, so the old stuck-``running`` problem is gone: if the
CLI is killed mid-run, ``run show`` reads whatever is on S3. While the run is
in flight only ``params.json`` exists (status=running); when it finishes the
CLI writes the final record to ``params.json`` (same key, updated) so a single
GET returns the authoritative state.

A lightweight ``runs/index.json`` manifest (a list of run summaries) is
maintained so ``run list`` is one GET instead of an S3 LIST + N GETs. The
manifest is a cache; S3 object existence is authoritative and the manifest is
repaired lazily (a missing manifest falls back to LIST + GET).

Logs buffer locally and flush to S3 every ``_FLUSH_LINES`` lines or
``_FLUSH_SECONDS`` seconds, and always on run finalization, so ``run logs``
(on this or another process) sees near-live output without per-line PUTs.
"""
from __future__ import annotations

import json
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Iterable

import boto3

from . import config

_FLUSH_LINES = 50
_FLUSH_SECONDS = 1.0
_LOG_DIR = Path.home() / ".cache" / "odoo-synth" / "run-logs"

# (run_id -> {seq, lines, last_flush, lock})
_buffers: dict[str, dict[str, Any]] = {}
_buf_lock = threading.Lock()
_write_lock = threading.Lock()  # serializes manifest + record writes


def _region() -> str:
    return config.require("AWS_REGION")


def _s3() -> boto3.client:
    return boto3.client("s3", region_name=_region())


def _bucket() -> str:
    b = config.dump_s3_bucket()
    if not b:
        raise RuntimeError("no S3 bucket configured for runs (set mask.dumps_bucket)")
    return b


def _prefix() -> str:
    return (config.dump_s3_prefix() or "masked-dumps").rstrip("/") + "/runs"


def _key(run_id: str, name: str) -> str:
    return f"{_prefix()}/{run_id}/{name}"


def _uri(run_id: str, name: str) -> str:
    return f"s3://{_bucket()}/{_key(run_id, name)}"


def _put(name: str, body: bytes, ctype: str = "application/json") -> None:
    _s3().put_object(Bucket=_bucket(), Key=name, Body=body, ContentType=ctype)


def _get(name: str) -> bytes | None:
    try:
        return _s3().get_object(Bucket=_bucket(), Key=name)["Body"].read()
    except _s3().exceptions.NoSuchKey:
        return None
    except Exception:  # noqa: BLE001
        return None


# ---------------------------------------------------------------------------
# manifest (runs/index.json) — a cached list for `run list`
# ---------------------------------------------------------------------------

def _manifest_key() -> str:
    return f"{_prefix()}/index.json"


def _read_manifest() -> list[dict[str, Any]]:
    raw = _get(_manifest_key())
    if not raw:
        return []
    try:
        data = json.loads(raw)
        return data if isinstance(data, list) else []
    except (ValueError, TypeError):
        return []


def _write_manifest(entries: list[dict[str, Any]]) -> None:
    _put(_manifest_key(), json.dumps(entries).encode())


def _manifest_upsert(entry: dict[str, Any]) -> None:
    with _write_lock:
        entries = _read_manifest()
        for i, e in enumerate(entries):
            if e.get("id") == entry.get("id"):
                entries[i] = {**e, **entry}
                break
        else:
            entries.insert(0, entry)
        # keep newest first, cap at 200
        entries = entries[:200]
        _write_manifest(entries)


# ---------------------------------------------------------------------------
# runs
# ---------------------------------------------------------------------------

def create_run(run_id: str, operation: str, params: dict[str, Any],
                profile_id: str | None = None) -> None:
    now = time.time()
    record = {
        "id": run_id,
        "operation": operation,
        "status": "running",
        "params": params,
        "profile_id": profile_id,
        "created_at": now,
        "started_at": now,
        "task_arn": None,
        "exit_code": None,
        "result": None,
        "finished_at": None,
    }
    _put(_key(run_id, "params.json"), json.dumps(record).encode())
    _manifest_upsert({
        "id": run_id, "operation": operation, "status": "running",
        "profile_id": profile_id, "created_at": now,
    })


def update_run(run_id: str, **fields: Any) -> None:
    if not fields:
        return
    with _write_lock:
        raw = _get(_key(run_id, "params.json"))
        record = json.loads(raw) if raw else {
            "id": run_id, "operation": None, "status": "running",
            "params": {}, "created_at": time.time()}
        record.update(fields)
        _put(_key(run_id, "params.json"), json.dumps(record).encode())
    # mirror status/exit_code into the manifest so `run list` is fresh
    if "status" in fields or "exit_code" in fields or "result" in fields:
        _manifest_upsert({
            "id": run_id, "status": fields.get("status", record.get("status")),
            "exit_code": fields.get("exit_code"),
            "operation": record.get("operation"),
            "profile_id": record.get("profile_id"),
            "created_at": record.get("created_at"),
            "finished_at": fields.get("finished_at"),
        })




def _runner_result(run_id: str) -> dict[str, Any] | None:
    """The runner's own result.json (written by the runner workspace into the
    run prefix). Present only after the runner finishes. Used to reconcile runs
    whose CLI finalizer died -- S3 is authoritative."""
    raw = _get(_key(run_id, "runner-result.json"))
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        return None


def _reconcile(run_id: str, record: dict[str, Any]) -> dict[str, Any]:
    """If the record is still 'running' but the runner wrote its result, finalize
    the record from the runner result. Returns the (possibly updated) record and
    persists the update + manifest if we finalized."""
    if record.get("status") != "running":
        return record
    rr = _runner_result(run_id)
    if not rr:
        return record
    exit_code = int(rr.get("exit_code", 1))
    status = "succeeded" if exit_code == 0 else "failed"
    record = dict(record)
    record["status"] = status
    record["exit_code"] = exit_code
    record["finished_at"] = record.get("finished_at") or time.time()
    if rr.get("error"):
        record.setdefault("result", {})
        record["result"] = {**(record.get("result") or {}), "error": rr["error"]}
    # task_arn/exit_code are already persisted as their own top-level columns
    # (the update_run() call right below) -- don't duplicate them into the
    # nested "result" blob too.
    record["result"] = {**(record.get("result") or {}), "runner_log_tail": rr.get("log_tail")}
    # persist the finalized record (without holding the caller's lock context)
    update_run(run_id, status=status, exit_code=exit_code,
               finished_at=record["finished_at"], result=record["result"],
               task_arn=rr.get("task_arn"))
    return record


def get_run(run_id: str) -> dict[str, Any] | None:
    raw = _get(_key(run_id, "params.json"))
    if not raw:
        return None
    d = json.loads(raw)
    d.setdefault("id", run_id)
    return _reconcile(run_id, d)


def list_runs(limit: int = 50) -> list[dict[str, Any]]:
    entries = _read_manifest()
    # cheap reconcile: any entry still 'running' whose runner wrote a result
    # gets finalized so the list reflects truth without a human re-checking.
    if not entries:
        # lazy repair: LIST the prefix + read each params.json
        try:
            resp = _s3().list_objects_v2(Bucket=_bucket(), Prefix=f"{_prefix()}/",
                                         Delimiter="/")
            prefixes = [p.get("Prefix") for p in resp.get("CommonPrefixes", [])]
            entries = []
            for p in prefixes:
                rid = (p or "").rstrip("/").rsplit("/", 1)[-1]
                rec = get_run(rid)
                if rec:
                    entries.append({
                        "id": rid, "operation": rec.get("operation"),
                        "status": rec.get("status"), "exit_code": rec.get("exit_code"),
                        "profile_id": rec.get("profile_id"),
                        "created_at": rec.get("created_at"),
                    })
            entries.sort(key=lambda e: e.get("created_at", 0), reverse=True)
        except Exception:  # noqa: BLE001
            pass
    # finalize any 'running' entries whose runner has since written a result
    for e in entries:
        if e.get("status") == "running":
            rr = _runner_result(e["id"])
            if rr:
                ec = int(rr.get("exit_code", 1))
                e["status"] = "succeeded" if ec == 0 else "failed"
                e["exit_code"] = ec
                e["finished_at"] = e.get("finished_at") or time.time()
                update_run(e["id"], status=e["status"], exit_code=ec,
                           finished_at=e["finished_at"], task_arn=rr.get("task_arn"))
    return entries[:limit]


# ---------------------------------------------------------------------------
# logs (buffered locally, flushed to S3)
# ---------------------------------------------------------------------------

def _buf(run_id: str) -> dict[str, Any]:
    with _buf_lock:
        b = _buffers.get(run_id)
        if b is None:
            _LOG_DIR.mkdir(parents=True, exist_ok=True)
            b = {"seq": 0, "lines": [], "last_flush": time.time(),
                 "lock": threading.Lock()}
            _buffers[run_id] = b
        return b


def _flush(run_id: str, b: dict[str, Any]) -> None:
    """Upload the accumulated log buffer to S3 as log.txt (full file, not just
    new lines — S3 has no append). Called under b['lock']."""
    if not b["lines"]:
        return
    # append to the existing log.txt if present, else start fresh
    existing = _get(_key(run_id, "log.txt")) or b""
    body = existing + "\n".join(b["lines"]).encode() + b"\n"
    _put(_key(run_id, "log.txt"), body, ctype="text/plain")
    b["lines"] = []
    b["last_flush"] = time.time()


def append_logs(run_id: str, lines: Iterable[str]) -> int:
    """Append lines; returns the last seq written."""
    b = _buf(run_id)
    with b["lock"]:
        for ln in lines:
            b["seq"] += 1
            b["lines"].append(ln)
        last = b["seq"]
        if (len(b["lines"]) >= _FLUSH_LINES or
                time.time() - b["last_flush"] >= _FLUSH_SECONDS):
            _flush(run_id, b)
        return last


def flush_logs(run_id: str) -> None:
    """Force-flush any buffered lines for a run (call on finalization)."""
    b = _buffers.get(run_id)
    if not b:
        return
    with b["lock"]:
        _flush(run_id, b)


def get_logs(run_id: str, after_seq: int = 0) -> list[dict[str, Any]]:
    raw = _get(_key(run_id, "log.txt"))
    if not raw:
        return []
    text = raw.decode(errors="replace")
    lines = text.splitlines()
    out = []
    seq = 0
    for ln in lines:
        seq += 1
        if seq > after_seq:
            out.append({"seq": seq, "ts": None, "line": ln})
    return out
