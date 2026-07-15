"""Developer environment lifecycle via Coder (coder/coder).

An *environment* is a Coder workspace: an EC2 instance launched by the Coder
server from the `odoo-synth-env` Terraform template (existing thin golden AMI
+ existing env instance profile, no public IP, no per-env SG rules). The Coder
agent running inside the workspace dials out to the Coder server over the
public internet; the developer reaches the workspace (web terminal, VS Code
Web, port-forwarded Odoo) through Coder's Wireguard tunnel -- so the workspace
needs zero inbound ports and no Secrets Manager secret. This deletes ~5 AWS
artifacts per environment vs. the old hand-rolled EC2/Secrets-Manager/SG-ingress
design.

This module is a thin shim over the `coder` CLI (driven by CODER_URL +
CODER_SESSION_TOKEN). The panel stores the Coder workspace name and proxies
lifecycle to the CLI/API.
"""
from __future__ import annotations
import base64
import json
import os
import secrets
import string
import subprocess
import urllib.request
import uuid
from typing import Optional

from . import config, store

TEMPLATE_NAME = "odoo-synth-env"


def _region() -> str:
    return config.require("AWS_REGION")


def _coder_env() -> dict:
    """Env for the coder CLI: the server URL + a session token, plus AWS creds."""
    env = {
        "CODER_URL": config.get("CODER_URL", "") or "",
        "CODER_SESSION_TOKEN": config.get_fresh("CODER_SESSION_TOKEN", "") or "",
    }
    for k in ("AWS_REGION", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY",
              "AWS_SESSION_TOKEN", "AWS_DEFAULT_REGION"):
        v = config.get(k)
        if v:
            env[k] = v
    return env




def _gen_password(n: int = 24) -> str:
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(n))


def _subdomain_url(subdomain_name: str) -> str:
    """Build the browser-reachable URL for a subdomain-hosted coder_app.

    CODER_URL is the Coder server origin, e.g. http://203.0.113.10:8943, and
    CODER_WILDCARD_ACCESS_URL on the server is "*.<same host:port>". Coder
    exposes per-app `subdomain_name` = "<app>--<ws>--<owner>". The app origin is
    therefore "<subdomain_name>.<host>:<port>" with the same scheme:port as
    CODER_URL. (nip.io makes *.host resolve to host, so no real DNS needed.)
    """
    base = config.get("CODER_URL", "").rstrip("/")
    if not base or not subdomain_name:
        return ""
    from urllib.parse import urlsplit
    ps = urlsplit(base)
    host, port = ps.hostname, ps.port
    full_host = f"{subdomain_name}.{host}" + (f":{port}" if port else "")
    return f"{ps.scheme}://{full_host}"


def _api(path: str) -> dict:
    """Call the Coder HTTP API (CODER_URL/api/v2/<path>) and return JSON."""
    url = f"{config.get('CODER_URL','').rstrip('/')}/api/v2/{path.lstrip('/')}"
    tok = config.get_fresh("CODER_SESSION_TOKEN", "") or ""
    req = urllib.request.Request(url, headers={"Coder-Session-Token": tok})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read())
    except Exception:  # noqa: BLE001
        return {}

def _run(args: list, *, json_out: bool = True, timeout: int = 60):
    """Run a `coder` CLI command, returning parsed JSON (or stdout)."""
    cmd = ["coder"] + args + (["-o", "json"] if json_out else [])
    try:
        p = subprocess.run(cmd, env={**os.environ, **_coder_env()},
                           capture_output=True, text=True, timeout=timeout, check=False)
    except FileNotFoundError as exc:
        raise RuntimeError("coder CLI not installed on the panel host") from exc
    if p.returncode != 0:
        raise RuntimeError(f"coder {' '.join(args)} failed: {p.stderr.strip() or p.stdout.strip()}")
    if not json_out:
        return p.stdout
    try:
        return json.loads(p.stdout) if p.stdout.strip() else {}
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"coder {args} returned non-JSON: {p.stdout[:200]}") from exc


def _dump_uri_for_run(run_id: Optional[str], explicit: Optional[str]) -> Optional[str]:
    """Resolve the masked-dump S3 URI: explicit wins, else the run's result."""
    if explicit:
        return explicit
    if not run_id:
        return None
    run = store.get_run(run_id)
    if not run:
        return None
    res = run.get("result") or {}
    return res.get("masked_dump_s3_uri")


def _resolve_odoo_image(source_run_id: Optional[str], s: dict) -> Optional[str]:
    """Provenance-baked Odoo image: a run's own image wins, else configured."""
    if source_run_id:
        run = store.get_run(source_run_id)
        if run:
            img = (run.get("result") or {}).get("odoo_image")
            if img:
                return img
    return s.get("odoo_image")


def create(source_run_id: Optional[str], issue: Optional[str],
           dump_s3_uri: Optional[str], repo_url: Optional[str] = None,
           repo_branch: Optional[str] = None,
           profile_id: Optional[str] = None) -> str:
    """Create a Coder workspace for the env. Runs `coder create` with the
    template parameters; the Coder server provisions the EC2 instance and the
    agent's startup_script boots Odoo. The env id IS the workspace name, so the
    panel and Coder share one key."""
    if not config.environments_configured():
        raise RuntimeError(
            "developer environments are not configured (set CODER_URL and "
            "CODER_SESSION_TOKEN, and ensure the odoo-synth-env template is "
            "published to the Coder server)")
    s = config.environments_settings()
    profile = store.get_profile(profile_id) if profile_id else None
    dump = _dump_uri_for_run(source_run_id, dump_s3_uri)
    if profile:
        odoo_img = profile.get("image_uri") or _resolve_odoo_image(source_run_id, s)
        r_url = repo_url or profile.get("addons_git_url") or s.get("repo_url")
        r_branch = repo_branch or profile.get("addons_git_ref") or s.get("repo_branch")
        git_token_secret = profile.get("git_token_secret") or s.get("git_token_secret")
        conf_extra = profile.get("odoo_conf_extra") or ""
    else:
        odoo_img = _resolve_odoo_image(source_run_id, s)
        r_url = repo_url or s.get("repo_url")
        r_branch = repo_branch or s.get("repo_branch")
        git_token_secret = s.get("git_token_secret")
        conf_extra = ""
    conf_extra_b64 = base64.b64encode((conf_extra or "").encode()).decode()

    env_id = uuid.uuid4().hex[:10]
    store.create_environment(env_id, source_run_id, issue, dump,
                             repo_url=r_url, repo_branch=r_branch,
                             odoo_image=odoo_img, profile_id=profile_id)
    store.update_environment(env_id, status="provisioning",
                             odoo_image=odoo_img, repo_url=r_url, repo_branch=r_branch)

    admin_password = _gen_password()
    params = [
        ("ami_id", s["ami_id"]),
        ("instance_profile", s["instance_profile"]),
        ("subnet_id", s["subnet_id"]),
        ("security_group_id", s["security_group_id"]),
        ("region", _region()),
        ("instance_type", s["instance_type"]),
        ("odoo_image", odoo_img or ""),
        ("dump_s3_uri", dump or ""),
        ("repo_url", r_url or ""),
        ("repo_branch", r_branch or ""),
        ("git_token_secret", git_token_secret or ""),
        ("issue", issue or ""),
        ("db_name", s.get("db_name") or "odoo"),
        ("odoo_master_password",
         config.get("ODOO_MASTER_PASSWORD", "change_me_master") or "change_me_master"),
        ("odoo_conf_extra_b64", conf_extra_b64),
        ("admin_password", admin_password),
    ]
    args = ["create", "-t", TEMPLATE_NAME, "-y", "--no-wait", env_id]
    for k, v in params:
        args += ["--parameter", f"{k}={v}"]
    _run(args, json_out=False, timeout=120)
    store.update_environment(env_id, workspace_name=env_id, status="provisioning",
                            password=admin_password)
    return env_id


# Coder workspace build status -> our env status.
_STATUS_MAP = {
    "pending": "provisioning", "starting": "provisioning", "building": "provisioning",
    "running": "running", "stopped": "stopped", "stopping": "stopping",
    "deleting": "terminated", "deleted": "terminated", "failed": "failed",
    "canceling": "failed", "canceled": "failed",
}


def reconcile() -> None:
    """Refresh env statuses from the Coder API. Called when the UI lists envs
    so a panel restart recovers the true state -- the Coder server owns
    lifecycle now, so this is a single API call, not per-env EC2 polling."""
    try:
        ws = _run(["list", "-a"], json_out=True, timeout=30)
    except Exception:  # noqa: BLE001
        return
    by_name = {w.get("name"): w for w in (ws if isinstance(ws, list) else [])}
    for e in store.list_environments():
        name = e.get("workspace_name") or e.get("id")
        w = by_name.get(name)
        if not w:
            if e.get("status") not in ("terminated", "failed"):
                store.update_environment(e["id"], status="terminated",
                                        error="workspace not found in Coder")
            continue
        latest = (w.get("latest_build") or {})
        cs = _STATUS_MAP.get(latest.get("status", ""), e.get("status"))
        if cs != e.get("status"):
            store.update_environment(e["id"], status=cs)
        # app URLs: Coder serves each app on its OWN origin (subdomain app
        # hosting, CODER_WILDCARD_ACCESS_URL=*.host). This is REQUIRED for Odoo:
        # its login form / assets use absolute server-root paths
        # (/web/login, /web/session/authenticate, /web/static/...) that resolve
        # against the app's own origin. With the old path proxy
        # (@owner/ws/apps/slug) those hit the Coder dashboard origin and 404.
        # The API exposes subdomain_name = "<app>--<ws>--<owner>"; the full host
        # is "<subdomain_name>.<wildcard-base>" where wildcard-base is the
        # CODER_URL host (the server's wildcard is "*.<that host>", so we
        # prefix the subdomain name to the same host:port).
        vscode = odoo = None
        wuuid = w.get("id")
        if wuuid:
            d = _api(f"workspaces/{wuuid}?include_agents=true")
            for r in (d.get("latest_build") or {}).get("resources", []):
                for a in r.get("agents", []):
                    for app in a.get("apps", []) or []:
                        if not app.get("subdomain"):
                            continue  # only subdomain apps are reachable for Odoo
                        sd = app.get("subdomain_name")
                        if not sd:
                            continue
                        u = _subdomain_url(sd)
                        if app.get("slug") == "vscode": vscode = u
                        if app.get("slug") == "odoo": odoo = u
        store.update_environment(e["id"], vscode_url=vscode, odoo_url=odoo)


def get_password(env_id: str) -> Optional[str]:
    """The per-workspace password (Odoo admin + code-server), set by the panel
    at create time as the `admin_password` template parameter and stored on the
    env row. Returned directly (no Secrets Manager lookup needed)."""
    env = store.get_environment(env_id)
    return env.get("password") if env else None


def teardown(env_id: str) -> None:
    """Delete the Coder workspace (Coder terminates the EC2 instance + cleans
    up the Terraform state). No per-env SG rule or secret to revoke -- those no
    longer exist."""
    env = store.get_environment(env_id)
    if not env:
        raise ValueError("environment not found")
    name = env.get("workspace_name") or env_id
    try:
        _run(["delete", "-y", name], json_out=False, timeout=120)
    except Exception as exc:  # noqa: BLE001
        store.update_environment(env_id, status="failed", error=str(exc))
        return
    store.update_environment(env_id, status="terminated",
                             vscode_url=None, odoo_url=None)


# Back-compat: the panel used to call `environments.reconcile_booting`.
reconcile_booting = reconcile
