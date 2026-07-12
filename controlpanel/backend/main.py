"""FastAPI control panel for the odoo-synth masking pipeline.

Endpoints
  GET  /                     -> static UI
  GET  /api/config           -> non-secret infra summary for the UI
  GET  /api/profiles         -> mask profiles + toggle defaults (drives the form)
  POST /api/runs             -> start a mask run; returns run_id
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
    operation: str = "mask"
    # SOURCE: a live Postgres DB the user points at
    source_dsn: Optional[str] = None       # postgresql://user:pass@host:port/db
    # optional SSH tunnel to reach the source through a bastion
    ssh_enabled: Optional[bool] = None
    ssh_bastion: Optional[str] = None      # user@host[:port]
    ssh_key: Optional[str] = None          # private key material (PEM)
    # masking
    mask_profile: Optional[str] = None
    admin_password: Optional[str] = None
    gm_jobs: Optional[int] = None
    # neutralize toggles
    neutralize_mail: Optional[bool] = None
    neutralize_fetchmail: Optional[bool] = None
    neutralize_payment: Optional[bool] = None
    neutralize_smtp_param: Optional[bool] = None
    reset_admin_login: Optional[bool] = None
    # output
    produce_dump: Optional[bool] = None    # also produce a downloadable pg_dump


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------

@app.get("/api/config")
def api_config() -> dict:
    dest = config.destination()
    return {
        "project": config.get("PROJECT"),
        "region": config.get("AWS_REGION"),
        "destination_db": dest.get("dbname"),
        "destination_host": dest.get("host"),
        "masked_cluster": config.get("ECS_CLUSTER"),
        "target_url": (
            f"http://{config.get('ALB_DNS')}/web/login"
            if config.get("ALB_DNS") else None
        ),
    }


@app.get("/api/profiles")
def api_profiles() -> dict:
    """Non-secret metadata that drives the form."""
    dest = config.destination()
    return {
        "mask_profiles": config.mask_profiles(),
        "neutralize_defaults": config.neutralize_defaults(),
        "reset_admin_login": config.panel().get("reset_admin_login", True),
        "gm_jobs": config.neutralize_defaults().get("gm_jobs", 4),
        "dump_download_enabled": bool(config.dump_s3_bucket()),
        "destination_label": config.panel().get("destination", {}).get("label", "managed"),
        "destination_db": dest.get("dbname"),
    }


@app.post("/api/runs")
def api_start_run(req: RunRequest) -> dict:
    if req.operation != "mask":
        raise HTTPException(400, "operation must be 'mask'")
    if not req.source_dsn:
        raise HTTPException(400, "source database URL (postgresql://…) is required")
    try:
        pipeline.parse_dsn(req.source_dsn)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(400, f"invalid source URL: {exc}")

    if req.ssh_enabled:
        if not req.ssh_bastion:
            raise HTTPException(400, "SSH tunnel enabled but bastion (user@host[:port]) is missing")
        if not req.ssh_key:
            raise HTTPException(400, "SSH tunnel enabled but private key is missing")
        try:
            pipeline.parse_bastion(req.ssh_bastion)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(400, f"invalid bastion: {exc}")

    run_id = uuid.uuid4().hex[:12]
    stored_params = {
        "operation": req.operation,
        "mask_profile": req.mask_profile,
        "source_present": bool(req.source_dsn),
        "ssh_enabled": bool(req.ssh_enabled),
        "produce_dump": bool(req.produce_dump),
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
