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

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import config, pipeline, store, environments, profiles, discovery, build
from .seed import seed_starter_profile

FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"

app = FastAPI(title="odoo-synth control panel")


@app.on_event("startup")
def _startup() -> None:
    store.init()
    try:
        seed_starter_profile()
    except Exception:  # noqa: BLE001 — seeding is best-effort
        pass


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
        if operation == "discover":
            dres = discovery.run_discovery(params["profile_id"], emit)
            result = {"exit_code": dres.get("exit_code", 1), **dres}
        elif operation == "build":
            bres = build.run_build(params["profile_id"], emit)
            result = {"exit_code": bres.get("exit_code", 1), **bres}
        else:
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
    # PROFILE path: run from a saved profile (source + mask inputs come from it)
    profile_id: Optional[str] = None
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


class EnvironmentRequest(BaseModel):
    profile_id: Optional[str] = None       # profile whose image + addons drive the env
    source_run_id: Optional[str] = None    # mask run whose dump seeds the env
    issue: Optional[str] = None            # github issue ref (optional)
    dump_s3_uri: Optional[str] = None      # explicit s3:// masked dump (optional)
    repo_url: Optional[str] = None         # addons repo to clone + live-mount (optional)
    repo_branch: Optional[str] = None      # branch/ref of the addons repo (optional)
    # access controls the user supplies from the UI:
    #  * allow_ip: an IP or CIDR to authorize on the env security group (web +
    #    SSH ports). "auto" resolves to the caller's own public IP.
    #  * ssh_public_key: an OpenSSH public key injected into the workspace
    #    user's authorized_keys so desktop VS Code (Remote-SSH) can connect.
    allow_ip: Optional[str] = None
    ssh_public_key: Optional[str] = None


class ProfileRequest(BaseModel):
    """A saved binding of a source system to its provenance code + image.

    All fields optional so the same model can be used for create and PATCH.
    """
    label: Optional[str] = None
    description: Optional[str] = None
    # SOURCE connection (password is split out into Secrets Manager server-side)
    source_dsn: Optional[str] = None       # postgresql://user:pass@host:port/db
    # optional SSH tunnel to reach the source through a bastion
    ssh_enabled: Optional[bool] = None
    ssh_bastion: Optional[str] = None      # user@host[:port]
    ssh_key: Optional[str] = None          # private key material (PEM) -> secret
    git_token: Optional[str] = None        # PAT for private addons repo -> secret
    # provenance (decision 2a: odoo_git_ref is manual)
    odoo_series: Optional[str] = None      # e.g. "19.0"
    odoo_git_url: Optional[str] = None
    odoo_git_ref: Optional[str] = None
    addons_git_url: Optional[str] = None
    addons_git_ref: Optional[str] = None
    # enterprise (decision 3: per-profile with explicit indicator)
    needs_enterprise: Optional[bool] = None
    enterprise_source: Optional[str] = None
    # mask inputs saved with the profile
    mask_profile: Optional[str] = None
    admin_password: Optional[str] = None
    gm_jobs: Optional[int] = None
    neutralize_mail: Optional[bool] = None
    neutralize_fetchmail: Optional[bool] = None
    neutralize_payment: Optional[bool] = None
    neutralize_smtp_param: Optional[bool] = None
    reset_admin_login: Optional[bool] = None
    produce_dump: Optional[bool] = None


class ImageDeleteRequest(BaseModel):
    image_uri: str


class MaskingRulesRequest(BaseModel):
    masking_rules: Optional[str] = None


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

    }


@app.get("/api/mask-config")
def api_mask_config() -> dict:
    """Non-secret metadata that drives the mask form (mask rulesets, defaults)."""
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


@app.get("/api/profiles")
def api_list_profiles() -> dict:
    return {"profiles": [profiles.public_view(p) for p in store.list_profiles()]}


@app.post("/api/profiles")
def api_create_profile(req: ProfileRequest) -> dict:
    payload = {k: v for k, v in req.model_dump().items() if v is not None}
    if not payload.get("label"):
        raise HTTPException(400, "a profile label is required")
    pid = profiles.create(payload)
    return {"profile_id": pid}


@app.get("/api/profiles/{profile_id}")
def api_get_profile(profile_id: str) -> dict:
    p = store.get_profile(profile_id)
    if not p:
        raise HTTPException(404, "profile not found")
    return profiles.public_view(p)


@app.patch("/api/profiles/{profile_id}")
def api_update_profile(profile_id: str, req: ProfileRequest) -> dict:
    payload = {k: v for k, v in req.model_dump().items() if v is not None}
    try:
        profiles.update(profile_id, payload)
    except KeyError:
        raise HTTPException(404, "profile not found")
    return {"status": "updated"}


@app.delete("/api/profiles/{profile_id}")
def api_delete_profile(profile_id: str) -> dict:
    profiles.delete(profile_id)
    return {"status": "deleted"}


@app.post("/api/profiles/{profile_id}/discover")
def api_discover_profile(profile_id: str) -> dict:
    prof = store.get_profile(profile_id)
    if not prof:
        raise HTTPException(404, "profile not found")
    conn = prof.get("source_conn") or {}
    if not conn.get("host"):
        raise HTTPException(400, "profile has no source database URL; add one first")
    run_id = uuid.uuid4().hex[:12]
    store.create_run(run_id, "discover",
                     {"operation": "discover", "profile_id": profile_id},
                     profile_id=profile_id)
    threading.Thread(
        target=_worker, args=(run_id, "discover", {"profile_id": profile_id}),
        daemon=True,
    ).start()
    return {"run_id": run_id}


@app.get("/api/profiles/{profile_id}/masking-rules")
def api_get_masking_rules(profile_id: str) -> dict:
    p = store.get_profile(profile_id)
    if not p:
        raise HTTPException(404, "profile not found")
    return {"masking_rules": p.get("masking_rules") or "",
            "discovery_yaml_uri": p.get("discovery_yaml_uri")}


@app.put("/api/profiles/{profile_id}/masking-rules")
def api_update_masking_rules(profile_id: str, req: MaskingRulesRequest) -> dict:
    p = store.get_profile(profile_id)
    if not p:
        raise HTTPException(404, "profile not found")
    ok, err = discovery.validate_masking_rules(req.masking_rules or "")
    if not ok:
        raise HTTPException(400, f"invalid masking rules: {err}")
    store.update_profile(profile_id, masking_rules=req.masking_rules or "")
    return {"status": "updated"}


@app.post("/api/profiles/{profile_id}/masking-rules/reset")
def api_reset_masking_rules(profile_id: str) -> dict:
    """Re-seed the editable plan from the last discovery.json in S3."""
    p = store.get_profile(profile_id)
    if not p:
        raise HTTPException(404, "profile not found")
    plan = discovery.discovered_masking_plan(p)
    if not plan:
        raise HTTPException(400, "no discovered masking plan available; run discovery first")
    store.update_profile(profile_id, masking_rules=plan)
    return {"status": "reset", "masking_rules": plan}


@app.post("/api/profiles/{profile_id}/build")
def api_build_profile(profile_id: str) -> dict:
    prof = store.get_profile(profile_id)
    if not prof:
        raise HTTPException(404, "profile not found")
    if not prof.get("discovery_hash"):
        raise HTTPException(400, "run discovery first (no discovery result yet)")
    run_id = uuid.uuid4().hex[:12]
    store.create_run(run_id, "build",
                     {"operation": "build", "profile_id": profile_id},
                     profile_id=profile_id)
    threading.Thread(
        target=_worker, args=(run_id, "build", {"profile_id": profile_id}),
        daemon=True,
    ).start()
    return {"run_id": run_id}


@app.get("/api/profiles/{profile_id}/images")
def api_list_profile_images(profile_id: str) -> dict:
    try:
        return build.list_images(profile_id)
    except KeyError:
        raise HTTPException(404, "profile not found")


@app.post("/api/profiles/{profile_id}/images/delete")
def api_delete_profile_image(profile_id: str, req: ImageDeleteRequest) -> dict:
    try:
        return build.delete_image(profile_id, req.image_uri)
    except KeyError:
        raise HTTPException(404, "profile not found")
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@app.post("/api/runs")
def api_start_run(req: RunRequest) -> dict:
    if req.operation != "mask":
        raise HTTPException(400, "operation must be 'mask'")

    profile_id = req.profile_id
    if profile_id:
        # PROFILE path: source + mask inputs come from the saved profile.
        prof = store.get_profile(profile_id)
        if not prof:
            raise HTTPException(404, "profile not found")
        try:
            # Profile-driven runs always produce the masked S3 dump: it is the
            # seed artifact a developer environment launched from this profile
            # consumes. (The Runs UI checkbox only applies to the legacy path.)
            params = profiles.run_params(
                profile_id, overrides={"produce_dump": True})
        except (KeyError, ValueError) as exc:
            raise HTTPException(400, f"cannot run profile: {exc}")
        odoo_image = prof.get("image_uri")
        stored_params = {
            "operation": "mask",
            "profile_id": profile_id,
            "mask_profile": params.get("mask_profile"),
            "source_present": True,
            "ssh_enabled": bool(params.get("ssh_enabled")),
            "produce_dump": bool(params.get("produce_dump")),
            "odoo_image": odoo_image,
        }
    else:
        # LEGACY inline path: caller supplies the source DSN + mask inputs.
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

        params = req.model_dump()
        odoo_image = None
        stored_params = {
            "operation": req.operation,
            "mask_profile": req.mask_profile,
            "source_present": bool(req.source_dsn),
            "ssh_enabled": bool(req.ssh_enabled),
            "produce_dump": bool(req.produce_dump),
            "admin_password_set": bool(req.admin_password),
        }

    run_id = uuid.uuid4().hex[:12]
    store.create_run(run_id, "mask", stored_params, profile_id=profile_id)

    threading.Thread(
        target=_worker, args=(run_id, "mask", params), daemon=True
    ).start()
    return {"run_id": run_id}


@app.get("/api/runs")
def api_list_runs() -> dict:
    runs = store.list_runs()
    # attach the developer-environment vscode url (if any) for each run
    by_run = store.environments_by_run()
    for r in runs:
        env = by_run.get(r["id"])
        if env and env.get("vscode_url"):
            res = r.get("result") or {}
            res["vscode_url"] = env["vscode_url"]
            r["result"] = res
        if env:
            r["environment"] = {"id": env["id"], "status": env["status"]}
    return {"runs": runs}


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
# developer environments
# ---------------------------------------------------------------------------

@app.get("/api/environments/config")
def api_env_config() -> dict:
    s = config.environments_settings()
    return {
        "configured": config.environments_configured(),
        "enabled": s["enabled"],
        "instance_type": s["instance_type"],
        "code_port": s["code_port"],
        "odoo_port": s["odoo_port"],
        "repo_url": s["repo_url"],
        "repo_branch": s["repo_branch"],
        "odoo_image": s["odoo_image"],
    }


@app.get("/api/environments")
def api_list_environments() -> dict:
    # recover any envs left 'booting' by a panel restart (their in-process poll
    # thread died, but the instance still tagged itself ready).
    environments.reconcile_booting()
    return {"environments": store.list_environments()}


def _caller_ip(request: Request) -> Optional[str]:
    """Best-effort public IP of the API caller (honours the ALB/proxy
    X-Forwarded-For header, else the socket peer)."""
    xff = request.headers.get("x-forwarded-for")
    if xff:
        return xff.split(",")[0].strip()
    return request.client.host if request.client else None


@app.get("/api/whoami")
def api_whoami(request: Request) -> dict:
    """Return the caller's public IP so the UI can pre-fill the allow-IP field."""
    return {"ip": _caller_ip(request)}


@app.post("/api/environments")
def api_create_environment(req: EnvironmentRequest, request: Request) -> dict:
    if not config.environments_configured():
        raise HTTPException(
            400,
            "developer environments are not configured (set ENV_AMI_ID, ENV_SG_ID "
            "and the other environments.* values in config.env / state.env)",
        )
    prof = None
    if req.profile_id:
        prof = store.get_profile(req.profile_id)
        if not prof:
            raise HTTPException(404, "profile not found")
        if prof.get("image_status") != "ready" or not prof.get("image_uri"):
            raise HTTPException(
                400,
                "that profile has no built image yet; run discover then build "
                "on the profile first",
            )
    if req.source_run_id:
        run = store.get_run(req.source_run_id)
        if not run:
            raise HTTPException(404, "source run not found")
        res = run.get("result") or {}
        if not req.dump_s3_uri and not res.get("masked_dump_s3_uri"):
            raise HTTPException(
                400,
                "that run has no masked dump to seed from; re-run the mask with "
                "'produce a downloadable pg_dump' enabled, or pass an explicit dump_s3_uri",
            )
    elif not req.dump_s3_uri:
        # no source run and no explicit dump -> the env would boot with an empty
        # database and Odoo would 500. Require one of the two.
        raise HTTPException(
            400,
            "a masked dump is required: pick a source run that produced a "
            "downloadable pg_dump, or pass an explicit dump_s3_uri "
            "(s3://bucket/key)",
        )
    allow_ip = req.allow_ip
    if allow_ip and allow_ip.strip().lower() == "auto":
        allow_ip = _caller_ip(request)
    env_id = environments.create(req.source_run_id, req.issue, req.dump_s3_uri,
                                 repo_url=req.repo_url, repo_branch=req.repo_branch,
                                 profile_id=req.profile_id,
                                 allow_ip=allow_ip,
                                 ssh_public_key=req.ssh_public_key)
    return {"environment_id": env_id}


@app.get("/api/environments/{env_id}")
def api_get_environment(env_id: str) -> dict:
    env = store.get_environment(env_id)
    if not env:
        raise HTTPException(404, "environment not found")
    return env


@app.get("/api/environments/{env_id}/password")
def api_get_environment_password(env_id: str) -> dict:
    """Reveal the code-server login password (read from Secrets Manager on
    demand; never persisted in the panel DB)."""
    if not store.get_environment(env_id):
        raise HTTPException(404, "environment not found")
    pw = environments.get_password(env_id)
    if pw is None:
        raise HTTPException(404, "no password available for this environment")
    return {"password": pw}


@app.delete("/api/environments/{env_id}")
def api_teardown_environment(env_id: str) -> dict:
    if not store.get_environment(env_id):
        raise HTTPException(404, "environment not found")
    environments.teardown(env_id)
    return {"status": "terminated"}


# ---------------------------------------------------------------------------
# static frontend (mounted last so /api takes precedence)
# ---------------------------------------------------------------------------

@app.get("/")
def index() -> FileResponse:
    return FileResponse(FRONTEND_DIR / "index.html")


app.mount("/static", StaticFiles(directory=FRONTEND_DIR), name="static")
