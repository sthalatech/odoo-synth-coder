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

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import config, pipeline, store

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
    dump_url: Optional[str] = None
    admin_password: Optional[str] = None


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


@app.post("/api/runs")
def api_start_run(req: RunRequest) -> dict:
    if req.operation not in ("restore", "mask"):
        raise HTTPException(400, "operation must be 'restore' or 'mask'")
    if req.operation == "restore" and not req.dump_url:
        raise HTTPException(400, "restore requires dump_url (presigned S3 URL)")

    run_id = uuid.uuid4().hex[:12]
    # redact nothing sensitive is stored beyond what's needed; dump_url is a
    # short-lived presigned URL, keep only a marker
    stored_params = {
        "operation": req.operation,
        "dump_url_present": bool(req.dump_url),
        "admin_password_set": bool(req.admin_password),
    }
    store.create_run(run_id, req.operation, stored_params)

    params = {"dump_url": req.dump_url, "admin_password": req.admin_password}
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
