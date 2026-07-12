"""FastAPI control panel for the odoo-synth masking pipeline.

Endpoints
  GET  /                     -> static UI
  GET  /api/config           -> non-secret infra summary for the UI
  POST /api/runs             -> start a run (restore | mask); returns run_id
  GET  /api/runs             -> recent runs
  GET  /api/runs/{id}        -> run detail (status, result, urls)
  GET  /api/runs/{id}/logs   -> Server-Sent Events stream of log lines
"""
from __future__ import annotations
import asyncio
import threading
import time
import uuid
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, UploadFile, File, Form
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import config, pipeline, store, uploads

FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"

app = FastAPI(title="odoo-synth control panel")


@app.on_event("startup")
def _startup() -> None:
    store.init()


# ---------------------------------------------------------------------------
# worker: run a pipeline operation in a background thread, persisting logs
# ---------------------------------------------------------------------------

def _worker(run_id: str, operation: str, params: dict) -> None:
    store.update_run(run_id, status="running", started_at=time.time())

    buf: list[str] = []
    lock = threading.Lock()

    def emit(line: str) -> None:
        with lock:
            buf.append(line)

    def flusher_done() -> None:
        with lock:
            pending = buf[:]
            buf.clear()
        if pending:
            store.append_logs(run_id, pending)

    # periodic flush thread so the SSE stream sees lines promptly
    stop_flush = threading.Event()

    def flush_loop() -> None:
        while not stop_flush.is_set():
            flusher_done()
            stop_flush.wait(0.5)

    ft = threading.Thread(target=flush_loop, daemon=True)
    ft.start()

    try:
        result = pipeline.run_operation(operation, params, emit)
        status = "succeeded" if result.get("exit_code") == 0 else "failed"
        store.update_run(
            run_id,
            status=status,
            task_arn=result.get("task_arn"),
            exit_code=result.get("exit_code"),
            result=result,
            finished_at=time.time(),
        )
    except Exception as exc:  # noqa: BLE001
        emit(f"[panel] ERROR: {exc}")
        store.update_run(
            run_id,
            status="failed",
            result={"error": str(exc)},
            finished_at=time.time(),
        )
    finally:
        stop_flush.set()
        ft.join(timeout=2)
        flusher_done()


# ---------------------------------------------------------------------------
# request models
# ---------------------------------------------------------------------------

class RunRequest(BaseModel):
    operation: str  # "restore" | "mask"
    # restore inputs
    source_type: Optional[str] = None      # sql_url|zip_url|sql_upload|zip_upload|db_dsn
    url: Optional[str] = None              # resolved URL (direct, or from an upload)
    dsn: Optional[str] = None              # for db_dsn
    target_conn: Optional[str] = None      # connection profile id
    source_conn: Optional[str] = None      # connection profile id (mask)
    source_db: Optional[str] = None        # override db name on the source conn
    target_db: Optional[str] = None        # override db name on the target conn
    # mask inputs
    mask_profile: Optional[str] = None
    admin_password: Optional[str] = None
    gm_jobs: Optional[int] = None
    neutralize_mail: Optional[bool] = None
    neutralize_fetchmail: Optional[bool] = None
    neutralize_payment: Optional[bool] = None
    neutralize_smtp_param: Optional[bool] = None
    reset_admin_login: Optional[bool] = None


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------

@app.get("/api/config")
def api_config() -> dict:
    return {
        "project": config.get("PROJECT"),
        "region": config.get("AWS_REGION"),
        "source_db": config.get("SOURCE_DB_NAME"),
        "target_db": config.get("TARGET_DB_NAME"),
        "source_cluster": config.get("SOURCE_ECS_CLUSTER"),
        "masked_cluster": config.get("ECS_CLUSTER"),
        "source_url": (
            f"http://{config.get('SRC_ALB_DNS')}/web/login"
            if config.get("SRC_ALB_DNS") else None
        ),
        "target_url": (
            f"http://{config.get('ALB_DNS')}/web/login"
            if config.get("ALB_DNS") else None
        ),
    }


@app.get("/api/profiles")
def api_profiles() -> dict:
    """Non-secret metadata that drives the form: connection profiles (id+label
    only), mask profiles, restore source types, and toggle defaults."""
    conns = [{"id": c["id"], "label": c.get("label", c["id"]),
              "dbname": config.get(c["dbname_env"]) if c.get("dbname_env") else c.get("dbname")}
             for c in config.connection_profiles()]
    return {
        "connections": conns,
        "mask_profiles": config.mask_profiles(),
        "restore_source_types": config.restore_source_types(),
        "neutralize_defaults": config.neutralize_defaults(),
        "reset_admin_login": config.panel().get("reset_admin_login", True),
        "gm_jobs": config.neutralize_defaults().get("gm_jobs", 4),
        "upload_enabled": bool(config.dump_s3_bucket()),
    }


@app.post("/api/upload")
async def api_upload(file: UploadFile = File(...)) -> dict:
    """Stage an uploaded .sql/.zip to S3 and return a presigned URL the caller
    passes back into POST /api/runs as `url`."""
    try:
        url = uploads.stage_upload(file.file, file.filename or "upload.bin")
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(400, f"upload failed: {exc}")
    return {"url": url, "filename": file.filename}


@app.post("/api/runs")
def api_start_run(req: RunRequest) -> dict:
    if req.operation not in ("restore", "mask"):
        raise HTTPException(400, "operation must be 'restore' or 'mask'")

    if req.operation == "restore":
        if not req.source_type:
            raise HTTPException(400, "restore requires source_type")
        needs_url = req.source_type in ("sql_url", "zip_url", "sql_upload", "zip_upload")
        if needs_url and not req.url:
            raise HTTPException(400, f"{req.source_type} requires a url")
        if req.source_type == "db_dsn" and not req.dsn:
            raise HTTPException(400, "db_dsn requires a dsn")

    run_id = uuid.uuid4().hex[:12]
    stored_params = {
        "operation": req.operation,
        "source_type": req.source_type,
        "source_conn": req.source_conn,
        "target_conn": req.target_conn,
        "target_db": req.target_db,
        "mask_profile": req.mask_profile,
        "url_present": bool(req.url),
        "dsn_present": bool(req.dsn),
        "admin_password_set": bool(req.admin_password),
    }
    store.create_run(run_id, req.operation, stored_params)

    params = req.model_dump()
    threading.Thread(
        target=_worker, args=(run_id, req.operation, params), daemon=True
    ).start()
    return {"run_id": run_id}


@app.get("/api/runs")
def api_list_runs() -> dict:
    return {"runs": store.list_runs()}


@app.get("/api/runs/{run_id}")
def api_get_run(run_id: str) -> dict:
    run = store.get_run(run_id)
    if not run:
        raise HTTPException(404, "run not found")
    return run


@app.get("/api/runs/{run_id}/logs")
async def api_stream_logs(run_id: str):
    if not store.get_run(run_id):
        raise HTTPException(404, "run not found")

    async def gen():
        cursor = 0
        while True:
            rows = store.get_logs(run_id, after_seq=cursor)
            for r in rows:
                cursor = r["seq"]
                # SSE frame; escape newlines defensively
                line = r["line"].replace("\r", "")
                yield f"data: {line}\n\n"
            run = store.get_run(run_id)
            if run and run["status"] in ("succeeded", "failed"):
                # one final drain then emit a terminal event
                rows = store.get_logs(run_id, after_seq=cursor)
                for r in rows:
                    cursor = r["seq"]
                    yield f"data: {r['line']}\n\n"
                yield f"event: end\ndata: {run['status']}\n\n"
                return
            await asyncio.sleep(0.6)

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ---------------------------------------------------------------------------
# static frontend (mounted last so /api takes precedence)
# ---------------------------------------------------------------------------

@app.get("/")
def index() -> FileResponse:
    return FileResponse(FRONTEND_DIR / "index.html")


app.mount("/static", StaticFiles(directory=FRONTEND_DIR), name="static")
