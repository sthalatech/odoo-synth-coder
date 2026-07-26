#!/usr/bin/env python3
"""odoo-synth — CLI for the odoo-synth masking pipeline (Phase C2).

Replaces the FastAPI web panel. Calls the same backend library modules
(lib/backend/*.py) directly over an in-process call boundary — no HTTP.
Long-running ops (discover / build / run mask) run SYNCHRONOUSLY in the
foreground and stream their log lines to stdout, while persisting them to the
the same backend stores the panel used (so run history + logs survive across calls).

User management is no longer exposed here: use the native `coder users` CLI.
Workspace lifecycle is owned by Coder natively (`coder create` / dashboard
presets).

Run from anywhere — the repo root is resolved from this file's location.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import traceback
import uuid
from pathlib import Path
from typing import Any, Optional

# --- repo-root resolution + backend import ----------------------------------
REPO_ROOT = Path(__file__).resolve().parents[1]
LIB = REPO_ROOT / "lib"
sys.path.insert(0, str(LIB))

from backend import (  # noqa: E402  (import after sys.path tweak)
    build,
    config,
    discovery,
    environments,
    pipeline,
    profiles,
    seed,
    store,
)


# --- helpers ----------------------------------------------------------------

def _err(msg: str) -> None:
    print(msg, file=sys.stderr)


def _emit_sink(run_id: str):
    """Return an `emit(line)` callable that prints to stdout AND persists the
    line to the run's log table (mirrors the panel's _worker emit-sink, but
    synchronous and immediate — no flush thread needed).

    Never raises: a dead stdout (e.g. the CLI's pipe closed because the
    foreground process was killed/timeboxed) must NOT abort the run -- the
    pipeline keeps running on the Coder runner and the result is still
    written to the store. BrokenPipeError is swallowed and stdout is rewired
    to /dev/null so further prints are no-ops instead of raising."""
    stdout_dead = False

    def emit(line: str) -> None:
        nonlocal stdout_dead
        if not stdout_dead:
            try:
                print(line, flush=True)
            except BrokenPipeError:
                stdout_dead = True
                try:
                    sys.stdout = open(os.devnull, "w")
                except Exception:  # noqa: BLE001
                    pass
            except Exception:  # noqa: BLE001 — never let printing kill the run
                pass
        try:
            store.append_logs(run_id, [line])
        except Exception:  # noqa: BLE001
            pass
    return emit


def _trim_result(result: dict) -> dict:
    """task_arn/exit_code are already persisted as their own top-level columns
    on the run record (see the update_run() calls below) -- drop them from
    the nested "result" blob so `run show` doesn't print each value twice."""
    return {k: v for k, v in result.items() if k not in ("task_arn", "exit_code")}


def _run_sync(operation: str, params: dict, profile_id: Optional[str],
              stored_params: dict) -> int:
    """Mirror backend/main.py `_worker` but synchronous + foreground.

    Creates the run row, marks it running, calls the op, then updates the row
    with the result (success or failure). Returns the run's exit_code (0 on
    success, non-zero on failure). Prints logs to stdout as they are emitted.
    """
    run_id = uuid.uuid4().hex[:12]
    store.create_run(run_id, operation, stored_params, profile_id=profile_id)
    store.update_run(run_id, status="running", started_at=time.time())
    emit = _emit_sink(run_id)
    exit_code = 1
    result: dict = {}
    try:
        if operation == "discover":
            dres = discovery.run_discovery(params["profile_id"], emit, run_id=run_id)
            result = {"exit_code": dres.get("exit_code", 1), **dres}
        elif operation == "build":
            bres = build.run_build(params["profile_id"], emit, run_id=run_id)
            result = {"exit_code": bres.get("exit_code", 1), **bres}
        else:
            result = pipeline.run_operation(operation, params, emit, run_id=run_id)
        _ec = result.get("exit_code", 1)
        exit_code = int(_ec) if _ec is not None else 1
    except BrokenPipeError:
        # The CLI's stdout pipe was closed (e.g. the foreground process was
        # killed/timeboxed). The pipeline keeps running on the runner and
        # the op may have completed -- record whatever result we have. If we
        # never got a result, leave the run "running" so a later `run show`
        # reflects the true state once the backend finishes.
        if result:
            _ec = result.get("exit_code", 1)
            exit_code = int(_ec) if _ec is not None else 1
            status = "succeeded" if exit_code == 0 else "failed"
            store.flush_logs(run_id)
            store.update_run(run_id, status=status, task_arn=result.get("task_arn"),
                             exit_code=exit_code, result=_trim_result(result),
                             finished_at=time.time())
        return exit_code
    except Exception as exc:  # noqa: BLE001
        emit(f"[odoo-synth] ERROR: {exc}")
        store.flush_logs(run_id)
        store.update_run(
            run_id,
            status="failed",
            result={"error": str(exc)},
            finished_at=time.time(),
        )
        _err(f"\n[odoo-synth] run {run_id} -> failed: {exc}")
        return 1
    status = "succeeded" if exit_code == 0 else "failed"
    store.flush_logs(run_id)
    store.update_run(
        run_id,
        status=status,
        task_arn=result.get("task_arn"),
        exit_code=exit_code,
        result=_trim_result(result),
        finished_at=time.time(),
    )
    try:
        _err(f"\n[odoo-synth] run {run_id} -> {status} (exit {exit_code})")
    except BrokenPipeError:
        pass
    return exit_code


def _print_json(obj: Any) -> None:
    print(json.dumps(obj, indent=2, default=str))


def _fmt_ts(ts: Any) -> str:
    if not ts:
        return "-"
    try:
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(float(ts)))
    except (TypeError, ValueError):
        return str(ts)


def _table(rows: list[dict], cols: list[tuple[str, str]]) -> None:
    """Pretty-print a list of dicts as a simple aligned table.
    `cols` is a list of (header, key) tuples."""
    headers = [h for h, _ in cols]
    widths = [len(h) for h in headers]
    rendered = []
    _ts_cols = {"created_at", "started_at", "finished_at", "pushed_at", "ts"}
    for r in rows:
        cells = []
        for _, k in cols:
            v = r.get(k)
            if v is None:
                cells.append("")
            elif k in _ts_cols:
                cells.append(_fmt_ts(v))
            else:
                cells.append(str(v))
        rendered.append(cells)
        for i, c in enumerate(cells):
            widths[i] = max(widths[i], len(c))
    fmt = "  ".join(f"{{:<{w}}}" for w in widths)
    print(fmt.format(*headers))
    print("  ".join("-" * w for w in widths))
    for cells in rendered:
        print(fmt.format(*cells))


def _require_profile(profile_id: str) -> dict:
    p = store.get_profile(profile_id)
    if not p:
        _err(f"profile not found: {profile_id}")
        sys.exit(2)
    return p


# --- arg helpers ------------------------------------------------------------

def _add_ssh_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--ssh-enabled", action="store_true", default=None,
                   help="enable SSH tunnel to the source")
    p.add_argument("--no-ssh", dest="ssh_enabled", action="store_false", default=None,
                   help="disable SSH tunnel (override a profile's stored tunnel for this run)")
    p.add_argument("--ssh-bastion", metavar="USER@HOST[:PORT]", default=None,
                   help="bastion host for the SSH tunnel")
    p.add_argument("--ssh-key", metavar="PATH", default=None,
                   help="path to a PEM private key for the SSH tunnel (read into a secret)")


def _add_mask_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--mask-profile", default=None,
                   help="mask ruleset id (e.g. odoo-core-pii)")
    p.add_argument("--admin-password", default=None,
                   help="Odoo admin password to set on the masked DB")
    p.add_argument("--gm-jobs", type=int, default=None, help="greenmask parallel jobs")
    p.add_argument("--neutralize-mail", dest="neutralize_mail", action="store_true",
                   default=None)
    p.add_argument("--no-neutralize-mail", dest="neutralize_mail", action="store_false")
    p.add_argument("--neutralize-fetchmail", dest="neutralize_fetchmail",
                   action="store_true", default=None)
    p.add_argument("--no-neutralize-fetchmail", dest="neutralize_fetchmail",
                   action="store_false")
    p.add_argument("--neutralize-payment", dest="neutralize_payment",
                   action="store_true", default=None)
    p.add_argument("--no-neutralize-payment", dest="neutralize_payment",
                   action="store_false")
    p.add_argument("--neutralize-smtp-param", dest="neutralize_smtp_param",
                   action="store_true", default=None)
    p.add_argument("--no-neutralize-smtp-param", dest="neutralize_smtp_param",
                   action="store_false")
    p.add_argument("--reset-admin-login", dest="reset_admin_login",
                   action="store_true", default=None)
    p.add_argument("--no-reset-admin-login", dest="reset_admin_login",
                   action="store_false")
    p.add_argument("--produce-dump", action="store_true", default=None,
                   help="also produce a downloadable pg_dump of the masked DB")
    p.add_argument("--subset-days", type=int, default=None,
                   help="keep only the last N days of transactional tables")
    p.add_argument("--exclude-table-data", default=None,
                   help="comma-sep tables to empty (or 'none')")


def _read_ssh_key(path: Optional[str]) -> Optional[str]:
    if not path:
        return None
    return Path(path).read_text()


def _read_file_text(path: str) -> str:
    """Read a file's text (for @file arguments). Errors if it can't be read."""
    return Path(path).read_text()


# --- profile commands -------------------------------------------------------

def cmd_profile_create(args) -> int:
    payload = {
        "label": args.label,
        "description": args.description,
        "source_dsn": args.source_dsn,
        "ssh_enabled": args.ssh_enabled,
        "ssh_bastion": args.ssh_bastion,
        "ssh_key": _read_ssh_key(args.ssh_key),
        "git_token": args.git_token,
        "odoo_series": args.odoo_series,
        "odoo_git_url": args.odoo_git_url,
        "odoo_git_ref": args.odoo_git_ref,
        "addons_git_url": args.addons_git_url,
        "addons_git_ref": args.addons_git_ref,
        "needs_enterprise": args.needs_enterprise,
        "enterprise_source": args.enterprise_source,
        "agent_name": args.agent_name,
        "agent_system_prompt": (
            _read_file_text(args.agent_system_prompt[1:])
            if args.agent_system_prompt and args.agent_system_prompt.startswith("@")
            else args.agent_system_prompt),
        "pr_base": args.pr_base,
    }
    payload = {k: v for k, v in payload.items() if v is not None}
    if not payload.get("label"):
        _err("a profile label is required (--label)")
        return 2
    pid = profiles.create(payload)
    if args.json:
        _print_json({"profile_id": pid})
    else:
        print(f"created profile {pid}")
    return 0


def cmd_profile_list(args) -> int:
    rows = [profiles.public_view(p) for p in store.list_profiles()]
    if args.json:
        _print_json({"profiles": rows})
        return 0
    if not rows:
        print("(no profiles)")
        return 0
    _table(rows, [("ID", "id"), ("LABEL", "label"), ("SERIES", "odoo_series"),
                  ("IMAGE", "image_status"), ("DISCOVERY", "discovery_hash")])
    return 0


def cmd_profile_show(args) -> int:
    p = _require_profile(args.profile_id)
    view = profiles.public_view(p)
    _print_json(view)
    return 0


def cmd_profile_update(args) -> int:
    payload: dict[str, Any] = {}
    for k in ("label", "description", "odoo_series", "odoo_git_url",
              "odoo_git_ref", "addons_git_url", "addons_git_ref",
              "enterprise_source"):
        v = getattr(args, k.replace("-", "_"), None)
        if v is not None:
            payload[k] = v
    if args.needs_enterprise is not None:
        payload["needs_enterprise"] = args.needs_enterprise
    if args.source_dsn is not None:
        payload["source_dsn"] = args.source_dsn
    if args.ssh_enabled is not None:
        payload["ssh_enabled"] = args.ssh_enabled
    if args.ssh_bastion is not None:
        payload["ssh_bastion"] = args.ssh_bastion
    if args.ssh_key is not None:
        payload["ssh_key"] = _read_ssh_key(args.ssh_key)
    if args.git_token is not None:
        payload["git_token"] = args.git_token
    if args.agent_name is not None:
        payload["agent_name"] = args.agent_name
    if args.agent_system_prompt is not None:
        sp = args.agent_system_prompt
        if sp.startswith("@"):
            sp = _read_file_text(sp[1:])
        payload["agent_system_prompt"] = sp
    if args.pr_base is not None:
        payload["pr_base"] = args.pr_base
    if not payload:
        _err("no update fields supplied")
        return 2
    try:
        profiles.update(args.profile_id, payload)
    except KeyError:
        _err(f"profile not found: {args.profile_id}")
        return 2
    print(f"updated profile {args.profile_id}")
    return 0


def cmd_profile_delete(args) -> int:
    profiles.delete(args.profile_id)
    print(f"deleted profile {args.profile_id}")
    return 0


def cmd_profile_discover(args) -> int:
    p = _require_profile(args.profile_id)
    conn = p.get("source_conn") or {}
    if not conn.get("host"):
        _err("profile has no source database URL; add one first")
        return 2
    params = {"profile_id": args.profile_id}
    # operation/profile_id are already top-level fields on the run record
    # (store.create_run receives them explicitly) -- no need to repeat them
    # inside "params" too.
    return _run_sync("discover", params, args.profile_id, {})


def cmd_profile_build(args) -> int:
    p = _require_profile(args.profile_id)
    if not p.get("discovery_hash"):
        _err("run discovery first (no discovery result yet)")
        return 2
    params = {"profile_id": args.profile_id}
    return _run_sync("build", params, args.profile_id, {})


def cmd_profile_images(args) -> int:
    try:
        res = build.list_images(args.profile_id)
    except KeyError:
        _err(f"profile not found: {args.profile_id}")
        return 2
    if args.json:
        _print_json(res)
        return 0
    print(f"current: {res.get('current') or '(none)'}")
    imgs = res.get("images") or []
    if not imgs:
        print("(no images)")
        return 0
    _table(imgs, [("URI", "uri"), ("CURRENT", "current"),
                  ("EXISTS", "exists"), ("SIZE_MB", "size_mb"),
                  ("PUSHED", "pushed_at")])
    return 0


def cmd_profile_images_delete(args) -> int:
    try:
        res = build.delete_image(args.profile_id, args.image)
    except KeyError:
        _err(f"profile not found: {args.profile_id}")
        return 2
    except ValueError as exc:
        _err(str(exc))
        return 2
    _print_json(res)
    return 0


def cmd_profile_masking_rules(args) -> int:
    p = _require_profile(args.profile_id)
    if args.reset:
        plan = discovery.discovered_masking_plan(p)
        if not plan:
            _err("no discovered masking plan available; run discovery first")
            return 2
        store.update_profile(args.profile_id, masking_rules=plan)
        print(plan)
        return 0
    if args.set is not None:
        # read YAML from file or stdin
        if args.set == "-":
            text = sys.stdin.read()
        else:
            text = Path(args.set).read_text()
        ok, err = discovery.validate_masking_rules(text)
        if not ok:
            _err(f"invalid masking rules: {err}")
            return 2
        store.update_profile(args.profile_id, masking_rules=text)
        print(f"updated masking rules for {args.profile_id}")
        return 0
    # show
    rules = p.get("masking_rules") or ""
    out = {"masking_rules": rules,
           "discovery_yaml_uri": p.get("discovery_yaml_uri")}
    if args.json:
        _print_json(out)
    else:
        print(f"discovery_yaml_uri: {out['discovery_yaml_uri'] or '(none)'}")
        print("---- masking_rules ----")
        print(rules or "(none)")
    return 0


# --- run commands -----------------------------------------------------------

def _mask_params_from_legacy(args) -> dict:
    """Build pipeline params from inline CLI args (legacy path, mirrors
    api_start_run's non-profile branch)."""
    if not args.source_dsn:
        _err("source database URL (postgresql://…) is required "
             "for the inline path (use --source-dsn or --profile)")
        sys.exit(2)
    try:
        pipeline.parse_dsn(args.source_dsn)
    except Exception as exc:  # noqa: BLE001
        _err(f"invalid source URL: {exc}")
        sys.exit(2)
    if args.ssh_enabled:
        if not args.ssh_bastion:
            _err("SSH tunnel enabled but --ssh-bastion is missing")
            sys.exit(2)
        if not args.ssh_key:
            _err("SSH tunnel enabled but --ssh-key is missing")
            sys.exit(2)
        try:
            pipeline.parse_bastion(args.ssh_bastion)
        except Exception as exc:  # noqa: BLE001
            _err(f"invalid bastion: {exc}")
            sys.exit(2)
    params = {
        "operation": "mask",
        "source_dsn": args.source_dsn,
        "ssh_enabled": bool(args.ssh_enabled),
        "ssh_bastion": args.ssh_bastion,
        "ssh_key": _read_ssh_key(args.ssh_key),
        "mask_profile": args.mask_profile,
        "admin_password": args.admin_password,
        "gm_jobs": args.gm_jobs,
        "neutralize_mail": args.neutralize_mail,
        "neutralize_fetchmail": args.neutralize_fetchmail,
        "neutralize_payment": args.neutralize_payment,
        "neutralize_smtp_param": args.neutralize_smtp_param,
        "reset_admin_login": args.reset_admin_login,
        "produce_dump": bool(args.produce_dump),
        "subset_days": args.subset_days,
        "exclude_table_data": args.exclude_table_data,
    }
    params = {k: v for k, v in params.items() if v is not None}
    return params


def _refresh_env_preset(profile_id: str) -> None:
    """Best-effort: regenerate coder/templates/odoo-synth-workspacer/presets.tf
    from the profile+run stores and push just that template, so a successful mask
    immediately shows up as a one-click preset in the Coder dashboard (named
    "<label> (masked <timestamp>)" -- one preset per profile, refreshed each
    time, not one per run: `coder templates push` already creates a new
    template version per call, and only the active version's presets are
    offered for new workspaces, so historical presets would just be clutter).

    Never fails the caller -- the mask run already succeeded; a push failure
    here is reported but swallowed, same as deploy/12_publish_template.sh's
    own WARN-and-continue behavior."""
    try:
        subprocess.run([sys.executable, str(REPO_ROOT / "deploy" / "_gen_presets.py")],
                       check=True, cwd=REPO_ROOT, capture_output=True, text=True, timeout=60)
    except subprocess.CalledProcessError as exc:
        _err(f"[odoo-synth] WARN: preset generation failed (mask succeeded regardless): "
             f"{(exc.stderr or '').strip()}")
        return
    tpl_dir = REPO_ROOT / "coder" / "templates" / "odoo-synth-workspacer"
    try:
        subprocess.run(["coder", "templates", "push", "-y", "--directory", str(tpl_dir), "odoo-synth-workspacer"],
                       env={**os.environ, **pipeline._coder_env()}, cwd=tpl_dir,
                       check=True, capture_output=True, text=True, timeout=180)
        print(f"[odoo-synth] preset refreshed for profile {profile_id} -> Coder dashboard")
    except subprocess.CalledProcessError as exc:
        _err(f"[odoo-synth] WARN: template push failed (mask succeeded regardless): "
             f"{(exc.stderr or '').strip()}")
    except FileNotFoundError:
        _err("[odoo-synth] WARN: `coder` CLI not found; skipped preset refresh")


def _mask_profile(profile_id: str, args) -> int:
    """Run mask for a saved profile. Shared by the standardized `profile mask
    <id>` command and the legacy `run mask --profile <id>` form. On success,
    refreshes that profile's Coder dashboard preset (see _refresh_env_preset)."""
    prof = store.get_profile(profile_id)
    if not prof:
        _err(f"profile not found: {profile_id}")
        return 2
    # Bastion overrides: --ssh-enabled/--ssh-bastion/--ssh-key on the CLI
    # override the profile's stored SSH tunnel settings. A profile may be
    # created without a bastion and later run through one (or vice versa).
    overrides: dict = {"produce_dump": True}
    # Mask knobs: the profile path rebuilds params from run_params() (which
    # starts from the profile's saved mask_inputs), so per-run CLI flags
    # must be forwarded as overrides or they are silently dropped. Only
    # non-None values override -- argparse leaves unset flags at None, and
    # run_params() filters None overrides, so this is a pure override.
    for _k in ("mask_profile", "admin_password", "gm_jobs", "subset_days",
               "exclude_table_data", "neutralize_mail", "neutralize_fetchmail",
               "neutralize_payment", "neutralize_smtp_param", "reset_admin_login"):
        _v = getattr(args, _k, None)
        if _v is not None:
            overrides[_k] = _v
    if args.ssh_enabled is not None:
        overrides["ssh_enabled"] = bool(args.ssh_enabled)
    if args.ssh_bastion is not None:
        if args.ssh_enabled is None and not (prof.get("source_conn") or {}).get("ssh_enabled"):
            # --ssh-bastion implies --ssh-enabled if neither was set
            overrides["ssh_enabled"] = True
        try:
            pipeline.parse_bastion(args.ssh_bastion)
        except Exception as exc:  # noqa: BLE001
            _err(f"invalid bastion: {exc}")
            return 2
        overrides["ssh_bastion"] = args.ssh_bastion
    if args.ssh_key is not None:
        overrides["ssh_key"] = _read_ssh_key(args.ssh_key)
    # consistency check: if SSH ends up enabled, bastion + key must be present
    try:
        params = profiles.run_params(profile_id, overrides=overrides)
    except (KeyError, ValueError) as exc:
        _err(f"cannot run profile: {exc}")
        return 2
    if params.get("ssh_enabled"):
        if not params.get("ssh_bastion"):
            _err("SSH tunnel enabled but no bastion set (use --ssh-bastion)")
            return 2
        if not params.get("ssh_key"):
            _err("SSH tunnel enabled but no key set (use --ssh-key)")
            return 2
    # operation/profile_id are already top-level fields on the run record;
    # odoo_image is already recorded as provenance in the result once the
    # mask succeeds (pipeline.run_operation echoes params["odoo_image"] there),
    # and source_present is always true on this path (run_params() already
    # raised above if the profile had no source connection) -- so none of
    # those need to be repeated here.
    stored = {
        "mask_profile": params.get("mask_profile"),
        "ssh_enabled": bool(params.get("ssh_enabled")),
        "ssh_bastion": params.get("ssh_bastion"),
        "produce_dump": bool(params.get("produce_dump")),
    }
    exit_code = _run_sync("mask", params, profile_id, stored)
    if exit_code == 0:
        _refresh_env_preset(profile_id)
    return exit_code


def cmd_profile_mask(args) -> int:
    return _mask_profile(args.profile_id, args)


def cmd_run_mask(args) -> int:
    if args.profile:
        return _mask_profile(args.profile, args)
    params = _mask_params_from_legacy(args)
    stored = {
        "mask_profile": params.get("mask_profile"),
        "source_present": bool(params.get("source_dsn")),
        "ssh_enabled": bool(params.get("ssh_enabled")),
        "ssh_bastion": params.get("ssh_bastion"),
        "produce_dump": bool(params.get("produce_dump")),
        "admin_password_set": bool(params.get("admin_password")),
    }
    return _run_sync("mask", params, None, stored)


def cmd_run_list(args) -> int:
    runs = store.list_runs()
    by_run = store.environments_by_run()
    for r in runs:
        env = by_run.get(r["id"])
        if env:
            r["environment"] = {"id": env["id"], "status": env["status"]}
    if args.json:
        _print_json({"runs": runs})
        return 0
    if not runs:
        print("(no runs)")
        return 0
    _table(runs, [("ID", "id"), ("OP", "operation"), ("STATUS", "status"),
                  ("EXIT", "exit_code"), ("PROFILE", "profile_id"),
                  ("CREATED", "created_at")])
    return 0


def cmd_run_show(args) -> int:
    run = store.get_run(args.run_id)
    if not run:
        _err(f"run not found: {args.run_id}")
        return 2
    _print_json(run)
    return 0


def cmd_run_logs(args) -> int:
    run = store.get_run(args.run_id)
    if not run:
        _err(f"run not found: {args.run_id}")
        return 2
    cursor = 0
    while True:
        rows = store.get_logs(args.run_id, after_seq=cursor)
        for r in rows:
            cursor = r["seq"]
            print(r["line"], flush=True)
        if not args.follow:
            return 0
        if run["status"] in ("succeeded", "failed"):
            # final drain
            rows = store.get_logs(args.run_id, after_seq=cursor)
            for r in rows:
                cursor = r["seq"]
                print(r["line"], flush=True)
            return 0
        time.sleep(0.6)
        run = store.get_run(args.run_id) or run


# --- env commands -----------------------------------------------------------

def cmd_env_list(args) -> int:
    environments.reconcile_booting()
    envs = store.list_environments()
    if args.json:
        _print_json({"environments": envs})
        return 0
    if not envs:
        print("(no environments)")
        return 0
    _table(envs, [("ID", "id"), ("STATUS", "status"), ("PROFILE", "profile_id"),
                  ("RUN", "source_run_id"), ("ODOO", "odoo_url")])
    return 0


def cmd_env_create(args) -> int:
    if not config.environments_configured():
        _err("developer environments are not configured "
             "(set CODER_URL + CODER_SESSION_TOKEN, ensure the "
             "odoo-synth-workspacer template is published)")
        return 2
    if args.profile_id:
        prof = store.get_profile(args.profile_id)
        if not prof:
            _err(f"profile not found: {args.profile_id}")
            return 2
        if prof.get("image_status") != "ready" or not prof.get("image_uri"):
            _err("that profile has no built image yet; run discover then build "
                 "on the profile first")
            return 2
    if args.source_run_id:
        run = store.get_run(args.source_run_id)
        if not run:
            _err(f"source run not found: {args.source_run_id}")
            return 2
        res = run.get("result") or {}
        if not args.dump_s3_uri and not res.get("masked_dump_s3_uri"):
            _err("that run has no masked dump to seed from; re-run the mask "
                 "with --produce-dump, or pass --dump-s3-uri")
            return 2
    elif not args.dump_s3_uri:
        _err("a masked dump is required: pass --source-run-id <run that produced "
             "a dump> or --dump-s3-uri s3://bucket/key")
        return 2
    env_id = environments.create(
        args.source_run_id, args.issue, args.dump_s3_uri,
        repo_url=args.repo_url, repo_branch=args.repo_branch,
        profile_id=args.profile_id,
        name=args.name)
    print(f"created environment {env_id}")
    return 0


def cmd_env_wait(args) -> int:
    ok = environments.wait_for_env(args.env_id, timeout=args.timeout, poll=args.poll)
    if ok:
        print(f"environment {args.env_id} is running")
        return 0
    _err(f"environment {args.env_id} did not reach running within {args.timeout}s "
         "(or the build failed)")
    return 1


def cmd_env_agent(args) -> int:
    env = store.get_environment(args.env_id)
    if not env:
        _err(f"environment not found: {args.env_id}")
        return 2
    # system prompt: explicit file wins, else the built-in project prompt shipped
    # with the repo (provisioned in the first phase; contents filled in later).
    system_prompt = ""
    sp = args.system_prompt or str(REPO_ROOT / environments.AGENT_SYSTEM_PROMPT_PATH)
    try:
        if sp and Path(sp).exists():
            system_prompt = Path(sp).read_text()
    except Exception:  # noqa: BLE001
        pass
    issue = args.issue or env.get("issue") or ""
    res = environments.run_agent(
        args.env_id, args.task, agent=args.agent,
        max_iterations=args.max_iterations, issue=issue,
        system_prompt=system_prompt, timeout=args.timeout)
    print(f"[agent] workspace={res['workspace']} agent={res['agent']} "
          f"exit={res['exit_code']}")
    if getattr(args, "json", False):
        _print_json(res)
    if res["exit_code"] != 0:
        _err("agent did not exit cleanly; see output above")
    return 0 if res["exit_code"] == 0 else 1


def cmd_env_show(args) -> int:
    env = store.get_environment(args.env_id)
    if not env:
        _err(f"environment not found: {args.env_id}")
        return 2
    _print_json(env)
    return 0


def cmd_env_password(args) -> int:
    env = store.get_environment(args.env_id)
    if not env:
        _err(f"environment not found: {args.env_id}")
        return 2
    pw = environments.get_password(args.env_id)
    if pw is None:
        _err("no password available for this environment")
        return 2
    print(pw)
    return 0


def cmd_env_delete(args) -> int:
    env = store.get_environment(args.env_id)
    if not env:
        _err(f"environment not found: {args.env_id}")
        return 2
    environments.teardown(args.env_id)
    print(f"terminated environment {args.env_id}")
    return 0


def cmd_env_config(args) -> int:
    s = config.environments_settings()
    out = {
        "configured": config.environments_configured(),
        "enabled": s["enabled"],
        "coder_url": s["coder_url"],
        "instance_type": s["instance_type"],
        "repo_url": s["repo_url"],
        "repo_branch": s["repo_branch"],
        "odoo_image": s["odoo_image"],
    }
    _print_json(out)
    return 0


# --- config command ---------------------------------------------------------

def cmd_config(args) -> int:
    dest = config.destination()
    env = config.environments_settings()
    # coder_connected reflects a USABLE token (configured-and-valid OR the
    # on-disk keyring session from an interactive `coder login`), not just a
    # configured (possibly stale) value.
    coder_ok = bool(env.get("coder_url")) and bool(config.coder_token())
    out = {
        "project": config.get("PROJECT"),
        "region": config.get("AWS_REGION"),
        "destination_db": dest.get("dbname"),
        "destination_host": dest.get("host"),
        "coder_url": env.get("coder_url") or None,
        "coder_connected": coder_ok,
        "compute": "coder" if coder_ok else "unconfigured",
    }
    _print_json(out)
    return 0


# --- argparse wiring --------------------------------------------------------

def _build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="odoo-synth",
        description=("odoo-synth masking pipeline CLI. Calls the backend "
                     "library directly (no HTTP). Long-running ops run in the "
                     "foreground and stream logs to stdout.\n\n"
                     "User management is now native: `coder users create/list`. "
                     "Workspace creation is via the Coder dashboard (presets) "
                     "or `coder create`."))
    ap.add_argument("--verbose", action="store_true",
                    help="show full tracebacks on error")
    sub = ap.add_subparsers(dest="cmd", required=True)

    # profile
    pprof = sub.add_parser("profile", help="source-binding profiles")
    psub = pprof.add_subparsers(dest="profile_cmd", required=True)

    pc = psub.add_parser("create", help="create a profile")
    pc.add_argument("--label", required=True)
    pc.add_argument("--description", default=None)
    pc.add_argument("--source-dsn", default=None,
                    help="postgresql://user:pass@host:port/db")
    pc.add_argument("--git-token", default=None, help="PAT for the private addons repo (stored as a Coder user secret, injected into workspaces as GH_PAT_<profile_id>)")
    pc.add_argument("--odoo-series", default=None, help='e.g. "19.0"')
    pc.add_argument("--odoo-git-url", default=None)
    pc.add_argument("--odoo-git-ref", default=None, help="manual git ref")
    pc.add_argument("--addons-git-url", default=None)
    pc.add_argument("--addons-git-ref", default=None)
    pc.add_argument("--needs-enterprise", action="store_true", default=None)
    pc.add_argument("--enterprise-source", default=None)
    pc.add_argument("--agent-name", default=None,
                   help="agent the launcher drives for this repo's issues "
                        "(opencode|claude-code); default opencode")
    pc.add_argument("--agent-system-prompt", default=None,
                   help="project system prompt for the agent (literal text or "
                        "@file). Empty = use the built-in agent-system-prompt.md")
    pc.add_argument("--pr-base", default=None,
                   help="target branch for `gh pr create --base <branch>` when "
                        "the agent finishes (default uat). The issue launcher "
                        "uses this so the agent opens its PR against the right "
                        "integration branch.")
    _add_ssh_args(pc)
    pc.add_argument("--json", action="store_true")
    pc.set_defaults(func=cmd_profile_create)

    pl = psub.add_parser("list", help="list profiles")
    pl.add_argument("--json", action="store_true")
    pl.set_defaults(func=cmd_profile_list)

    ps = psub.add_parser("show", help="show a profile")
    ps.add_argument("profile_id")
    ps.set_defaults(func=cmd_profile_show)

    pu = psub.add_parser("update", help="update a profile")
    pu.add_argument("profile_id")
    pu.add_argument("--label", default=None)
    pu.add_argument("--description", default=None)
    pu.add_argument("--source-dsn", default=None)
    pu.add_argument("--git-token", default=None)
    pu.add_argument("--odoo-series", default=None)
    pu.add_argument("--odoo-git-url", default=None)
    pu.add_argument("--odoo-git-ref", default=None)
    pu.add_argument("--addons-git-url", default=None)
    pu.add_argument("--addons-git-ref", default=None)
    pu.add_argument("--needs-enterprise", action="store_true", default=None)
    pu.add_argument("--no-needs-enterprise", dest="needs_enterprise",
                    action="store_false")
    pu.add_argument("--enterprise-source", default=None)
    pu.add_argument("--agent-name", default=None,
                   help="agent the launcher drives for this repo's issues "
                        "(opencode|claude-code); default opencode")
    pu.add_argument("--agent-system-prompt", default=None,
                   help="project system prompt for the agent (literal text or "
                        "@file to read from a file). Empty = use the built-in "
                        "agent-system-prompt.md")
    pu.add_argument("--pr-base", default=None,
                   help="target branch for `gh pr create --base <branch>` when "
                        "the agent finishes (default uat). Set per-profile so the "
                        "issue launcher tells the agent the right integration branch.")
    _add_ssh_args(pu)
    pu.set_defaults(func=cmd_profile_update)

    pd = psub.add_parser("delete", help="delete a profile (and its secrets)")
    pd.add_argument("profile_id")
    pd.set_defaults(func=cmd_profile_delete)

    pdisc = psub.add_parser("discover", help="run discovery (synchronous, streams logs)")
    pdisc.add_argument("profile_id")
    pdisc.set_defaults(func=cmd_profile_discover)

    pbld = psub.add_parser("build", help="build the odoo image (synchronous, streams logs)")
    pbld.add_argument("profile_id")
    pbld.set_defaults(func=cmd_profile_build)

    pmask = psub.add_parser("mask", help="run a mask operation (synchronous, streams logs); "
                                          "refreshes the Coder dashboard preset on success")
    pmask.add_argument("profile_id")
    _add_ssh_args(pmask)
    _add_mask_args(pmask)
    pmask.set_defaults(func=cmd_profile_mask)

    pimg = psub.add_parser("images", help="profile image management")
    pimg.add_argument("--json", action="store_true")
    pimgsub = pimg.add_subparsers(dest="images_cmd")
    pimg.set_defaults(func=cmd_profile_images, profile_id=None)
    pil = pimgsub.add_parser("list", help="list images (default)")
    pil.add_argument("profile_id")
    pil.set_defaults(func=cmd_profile_images)
    pid = pimgsub.add_parser("delete", help="delete an ECR image tag")
    pid.add_argument("profile_id")
    pid.add_argument("--image", required=True, help="image URI to delete")
    pid.set_defaults(func=cmd_profile_images_delete)

    pmr = psub.add_parser("masking-rules", help="show / set / reset the editable masking plan")
    pmr.add_argument("profile_id")
    pmr.add_argument("--set", metavar="FILE|-", default=None,
                     help="set masking rules from a YAML file (or '-' for stdin)")
    pmr.add_argument("--reset", action="store_true",
                     help="reset to the discovered masking plan")
    pmr.add_argument("--json", action="store_true")
    pmr.set_defaults(func=cmd_profile_masking_rules)

    # run
    prun = sub.add_parser("run", help="mask runs")
    rsub = prun.add_subparsers(dest="run_cmd", required=True)

    rm = rsub.add_parser("mask", help="run a mask operation (synchronous, streams logs). "
                                       "Prefer `profile mask <id>` for saved profiles -- "
                                       "this form remains for the inline --source-dsn path.")
    rm.add_argument("--profile", default=None,
                    help="run from a saved profile id (equivalent to `profile mask <id>`)")
    rm.add_argument("--source-dsn", default=None,
                    help="inline source (legacy path, no profile): postgresql://…")
    _add_ssh_args(rm)
    _add_mask_args(rm)
    rm.set_defaults(func=cmd_run_mask)

    rl = rsub.add_parser("list", help="list recent runs")
    rl.add_argument("--json", action="store_true")
    rl.set_defaults(func=cmd_run_list)

    rs = rsub.add_parser("show", help="show a run")
    rs.add_argument("run_id")
    rs.set_defaults(func=cmd_run_show)

    rg = rsub.add_parser("logs", help="print a run's stored logs")
    rg.add_argument("run_id")
    rg.add_argument("--follow", action="store_true",
                    help="poll for new lines until the run finishes")
    rg.set_defaults(func=cmd_run_logs)

    # env
    penv = sub.add_parser("env", help="developer environments (Coder workspaces)")
    esub = penv.add_subparsers(dest="env_cmd", required=True)

    el = esub.add_parser("list", help="list environments")
    el.add_argument("--json", action="store_true")
    el.set_defaults(func=cmd_env_list)

    ec = esub.add_parser("create", help="launch an environment")
    ec.add_argument("--profile-id", default=None)
    ec.add_argument("--source-run-id", default=None)
    ec.add_argument("--issue", default=None, help="github issue ref (optional)")
    ec.add_argument("--dump-s3-uri", default=None,
                    help="explicit s3:// masked dump (optional override)")
    ec.add_argument("--repo-url", default=None)
    ec.add_argument("--repo-branch", default=None)
    ec.add_argument("--name", default=None,
                    help="human-friendly Coder workspace name (e.g. iss-42-fix-login). "
                         "Coerced to [a-z0-9-]; falls back to the random env id when empty.")
    ec.set_defaults(func=cmd_env_create)

    ew = esub.add_parser("wait", help="wait for an env's workspace to reach running")
    ew.add_argument("env_id")
    ew.add_argument("--timeout", type=int, default=1200,
                    help="max seconds to wait for the build (default 1200)")
    ew.add_argument("--poll", type=int, default=10)
    ew.set_defaults(func=cmd_env_wait)

    ea = esub.add_parser("agent", help="invoke the AI agent (opencode/claude-code) inside an env")
    ea.add_argument("env_id")
    ea.add_argument("task", help="task string for the agent (e.g. the issue summary)")
    ea.add_argument("--agent", default="opencode",
                    help="agent to invoke headlessly (opencode|claude-code); superpowers drives it")
    ea.add_argument("--max-iterations", type=int, default=15,
                    help="kept for compat; the agent self-drives via superpowers")
    ea.add_argument("--timeout", type=int, default=3600,
                    help="wall-clock cost guard in seconds (default 3600)")
    ea.add_argument("--issue", default=None, help="github issue ref for context")
    ea.add_argument("--system-prompt", default=None,
                    help="path to a project system prompt file (default: built-in)")
    ea.add_argument("--json", action="store_true", help="print the result as JSON")
    ea.set_defaults(func=cmd_env_agent)

    es = esub.add_parser("show", help="show an environment")
    es.add_argument("env_id")
    es.set_defaults(func=cmd_env_show)

    epw = esub.add_parser("password", help="reveal the env login password")
    epw.add_argument("env_id")
    epw.set_defaults(func=cmd_env_password)

    ed = esub.add_parser("delete", help="tear down an environment")
    ed.add_argument("env_id")
    ed.set_defaults(func=cmd_env_delete)

    ecfg = esub.add_parser("config", help="show environment infra settings")
    ecfg.set_defaults(func=cmd_env_config)

    # config
    pcfg = sub.add_parser("config", help="non-secret infra summary")
    pcfg.set_defaults(func=cmd_config)

    return ap


def main(argv: Optional[list[str]] = None) -> int:
    ap = _build_parser()
    args = ap.parse_args(argv)

    # startup: init the store + best-effort seed (mirrors the panel startup)
    store.init()
    try:
        seed.seed_starter_profile()
    except Exception:  # noqa: BLE001 — seeding is best-effort
        pass

    try:
        return int(args.func(args) or 0)
    except KeyboardInterrupt:
        _err("interrupted")
        return 130
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001
        if getattr(args, "verbose", False):
            traceback.print_exc()
        else:
            _err(f"error: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
