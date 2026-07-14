#!/usr/bin/env python3
"""Provenance discovery worker.

Runs inside the discovery container (source DB reachable directly or through the
SSH tunnel the entrypoint opened). It inspects a *live* Odoo source database and
its addons repo to produce a ``discovery.yaml`` describing exactly what an image
must contain to run this dataset:

  * odoo_series          - from ir_module_module(base).latest_version
  * installed_modules    - state='installed' in ir_module_module
  * custom_modules       - installed modules that live in the addons repo
  * python_deps          - union of:
        - manifest external_dependencies.python for installed custom modules
        - packages named in any requirements.txt in the repo
        - a heuristic AST import scan that catches *undeclared* third-party
          imports (e.g. googletrans) that metadata alone would miss
  * apt_deps             - manifest external_dependencies.bin (mapped to apt)

The result is uploaded to a presigned S3 PUT URL. Everything is best-effort:
discovery never hard-fails on a single unreadable module.
"""
from __future__ import annotations

import ast
import hashlib
import json
import os
import subprocess
import sys
import urllib.request
from pathlib import Path


def log(msg: str) -> None:
    print(f"[discover] {msg}", flush=True)


# ---------------------------------------------------------------------------
# source DB inspection (via psql; the image ships the postgres client)
# ---------------------------------------------------------------------------

def _psql(query: str) -> list[str]:
    env = dict(os.environ)
    env["PGPASSWORD"] = os.environ.get("SOURCE_DB_PASSWORD", "")
    env["PGCONNECT_TIMEOUT"] = "15"
    cmd = [
        "psql", "-tA", "-F", "\t", "-v", "ON_ERROR_STOP=1",
        "-h", os.environ["SOURCE_DB_HOST"],
        "-p", os.environ.get("SOURCE_DB_PORT", "5432"),
        "-U", os.environ["SOURCE_DB_USER"],
        "-d", os.environ["SOURCE_DB_NAME"],
        "-c", query,
    ]
    out = subprocess.check_output(cmd, env=env, text=True)
    return [ln for ln in out.splitlines() if ln.strip()]


def read_source() -> tuple[str, list[str]]:
    base_ver = _psql("SELECT latest_version FROM ir_module_module WHERE name='base'")
    series = ""
    if base_ver:
        parts = base_ver[0].split(".")
        if len(parts) >= 2:
            series = f"{parts[0]}.{parts[1]}"
    installed = _psql(
        "SELECT name FROM ir_module_module WHERE state='installed' ORDER BY name")
    log(f"source series={series or '?'} installed_modules={len(installed)}")
    return series, installed


# ---------------------------------------------------------------------------
# addons repo clone + scan
# ---------------------------------------------------------------------------

def clone_addons(dest: Path) -> Path | None:
    url = os.environ.get("ADDONS_GIT_URL")
    if not url:
        log("no addons repo configured; skipping addons scan")
        return None
    ref = os.environ.get("ADDONS_GIT_REF") or ""
    token = os.environ.get("GIT_TOKEN") or ""
    clone_url = url
    if token and url.startswith("https://"):
        clone_url = url.replace("https://", f"https://x-access-token:{token}@", 1)
    log(f"cloning addons {url} @ {ref or 'default'}")
    subprocess.check_call(
        ["git", "clone", "--filter=blob:none", "--quiet", clone_url, str(dest)])
    if ref:
        # works for branch, tag, or full commit sha
        subprocess.check_call(["git", "-C", str(dest), "checkout", "--quiet", ref])
    return dest


def find_manifests(root: Path) -> dict[str, Path]:
    """Map module technical name -> its directory, for every addon in the repo."""
    mods: dict[str, Path] = {}
    for man in root.rglob("__manifest__.py"):
        mods[man.parent.name] = man.parent
    for man in root.rglob("__openerp__.py"):
        mods.setdefault(man.parent.name, man.parent)
    return mods


def parse_manifest(path: Path) -> dict:
    try:
        return ast.literal_eval(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        log(f"could not parse manifest {path}: {exc}")
        return {}


# ---------------------------------------------------------------------------
# heuristic import scan (catches undeclared third-party deps)
# ---------------------------------------------------------------------------

# import-name -> pip package name, where they differ
_IMPORT_TO_PIP = {
    "googletrans": "googletrans",
    "dateutil": "python-dateutil",
    "yaml": "PyYAML",
    "PIL": "Pillow",
    "cv2": "opencv-python-headless",
    "bs4": "beautifulsoup4",
    "Crypto": "pycryptodome",
    "jwt": "PyJWT",
    "sklearn": "scikit-learn",
    "serial": "pyserial",
    "OpenSSL": "pyOpenSSL",
    "usb": "pyusb",
    "ldap": "python-ldap",
    "magic": "python-magic",
    "fitz": "PyMuPDF",
}

# things that ship with Odoo or are provided by the base image / core reqs, so we
# never want to add them as extra deps.
_ODOO_PROVIDED = {
    "odoo", "openerp", "werkzeug", "lxml", "psycopg2", "passlib", "requests",
    "PIL", "reportlab", "babel", "decorator", "docutils", "jinja2", "markupsafe",
    "psutil", "pydot", "pyparsing", "pypdf2", "pyserial", "python-dateutil",
    "pytz", "pyusb", "qrcode", "vobject", "werkzeug", "xlrd", "xlsxwriter",
    "xlwt", "zeep", "num2words", "ofxparse", "freezegun", "gevent", "greenlet",
    "idna", "polib", "cryptography", "libsass", "chardet", "urllib3",
}


def _stdlib_names() -> set[str]:
    names = set(getattr(sys, "stdlib_module_names", set()))
    names |= {"__future__", "typing_extensions"}
    return names


# odoo.tools.config keys an addon reads from odoo.conf. If a source deployment
# defines custom keys there (e.g. an SSO addon doing config['sso_api_secret'])
# and the key is absent, Odoo raises KeyError and the request 500s. We surface
# these so the profile can seed them (empty) into the dev env's odoo.conf.
# Anything Odoo itself defines is filtered out — those always exist.
_CORE_CONFIG_KEYS = {
    "addons_path", "admin_passwd", "data_dir", "db_host", "db_port", "db_user",
    "db_password", "db_name", "db_maxconn", "db_template", "dbfilter",
    "http_port", "http_interface", "http_enable", "list_db", "proxy_mode",
    "workers", "max_cron_threads", "limit_time_cpu", "limit_time_real",
    "limit_memory_hard", "limit_memory_soft", "limit_request", "log_level",
    "logfile", "log_handler", "server_wide_modules", "smtp_server", "smtp_port",
    "smtp_user", "smtp_password", "smtp_ssl", "email_from", "unaccent",
    "without_demo", "test_enable", "test_file", "test_tags", "gevent_port",
    "screencasts", "screenshots", "geoip_database", "geoip_city_db",
    "geoip_country_db", "pidfile", "syslog", "csv_internal_sep",
    "publisher_warranty_url", "reportgz", "root_path", "upgrade_path",
    "pre_upgrade_scripts", "load_language", "language", "transient_age_limit",
    "osv_memory_age_limit", "websocket_keep_alive_timeout",
    "websocket_rate_limit_burst", "websocket_rate_limit_delay",
}

# attribute chains whose subscript/`.get()` reads an odoo.conf key:
#   config['x'] / config.get('x') / tools.config['x'] / conf.config['x']
_CONFIG_OBJ_NAMES = {"config"}


def _config_key_from_node(node: ast.AST):
    """Return the string literal key if `node` is `<cfg>['key']` or
    `<cfg>.get('key'...)` where <cfg> is a config object, else None."""
    if isinstance(node, ast.Subscript):
        base = node.value
        name = (base.attr if isinstance(base, ast.Attribute)
                else base.id if isinstance(base, ast.Name) else None)
        if name in _CONFIG_OBJ_NAMES:
            sl = node.slice
            if isinstance(sl, ast.Constant) and isinstance(sl.value, str):
                return sl.value
    if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
            and node.func.attr == "get"):
        base = node.func.value
        name = (base.attr if isinstance(base, ast.Attribute)
                else base.id if isinstance(base, ast.Name) else None)
        if name in _CONFIG_OBJ_NAMES and node.args:
            a0 = node.args[0]
            if isinstance(a0, ast.Constant) and isinstance(a0.value, str):
                return a0.value
    return None


def config_key_scan(root: Path) -> set[str]:
    """Find odoo.tools.config keys the addons read from odoo.conf, minus the
    keys Odoo always defines. These are the source-specific config options a
    masked dev replica must have present (even if empty) to avoid KeyError."""
    keys: set[str] = set()
    for py in root.rglob("*.py"):
        try:
            tree = ast.parse(py.read_text(encoding="utf-8"), filename=str(py))
        except Exception:  # noqa: BLE001 — skip unparseable files
            continue
        for node in ast.walk(tree):
            k = _config_key_from_node(node)
            if k:
                keys.add(k)
    return {k for k in keys if k not in _CORE_CONFIG_KEYS}


def import_scan(root: Path, local_modules: set[str]) -> set[str]:
    stdlib = _stdlib_names()
    found: set[str] = set()
    for py in root.rglob("*.py"):
        try:
            tree = ast.parse(py.read_text(encoding="utf-8"), filename=str(py))
        except Exception:  # noqa: BLE001 — skip unparseable files
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for n in node.names:
                    found.add(n.name.split(".")[0])
            elif isinstance(node, ast.ImportFrom):
                if node.level:  # relative import within the addon
                    continue
                if node.module:
                    found.add(node.module.split(".")[0])
    third_party = set()
    for name in found:
        if name in stdlib or name in local_modules:
            continue
        if name in _ODOO_PROVIDED or name.lower() in _ODOO_PROVIDED:
            continue
        third_party.add(name)
    return third_party


# PyPI names that must never be emitted as deps: junk/namespace/meta packages.
# "google" is a search-scraper that hijacks the google.* namespace and breaks
# real distributions (google-genai -> google.genai); the real package is
# discovered separately from requirements.txt / manifests.
_JUNK_PIP = {"google", "pip", "setuptools", "wheel"}


def to_pip_names(imports: set[str]) -> set[str]:
    return {_IMPORT_TO_PIP.get(i, i) for i in imports
            if _IMPORT_TO_PIP.get(i, i).lower() not in _JUNK_PIP}


# ---------------------------------------------------------------------------
# requirements.txt scan
# ---------------------------------------------------------------------------

def scan_requirements(root: Path) -> tuple[set[str], list[str]]:
    pkgs: set[str] = set()
    files: list[str] = []
    for req in root.rglob("requirements.txt"):
        files.append(str(req.relative_to(root)))
        for line in req.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = line.split("#", 1)[0].strip()
            if line and not line.startswith("-"):
                pkgs.add(line)
    return pkgs, files


# ---------------------------------------------------------------------------

def upload(payload: dict) -> None:
    put_url = os.environ.get("DISCOVERY_PUT_URL")
    body = json.dumps(payload, indent=2, sort_keys=True).encode()
    if not put_url:
        log("no DISCOVERY_PUT_URL; printing discovery result instead")
        print(body.decode())
        return
    req = urllib.request.Request(put_url, data=body, method="PUT",
                                 headers={"Content-Type": "application/json"})
    urllib.request.urlopen(req, timeout=60)  # noqa: S310 — presigned S3 URL
    log("uploaded discovery.json to S3")


def main() -> int:
    series, installed = read_source()
    installed_set = set(installed)

    python_declared: set[str] = set()
    apt_deps: set[str] = set()
    custom_modules: list[str] = []
    req_pkgs: set[str] = set()
    req_files: list[str] = []
    heuristic: set[str] = set()
    config_keys: set[str] = set()

    workdir = Path("/tmp/addons")
    repo = clone_addons(workdir)
    if repo:
        mods = find_manifests(repo)
        local_modules = set(mods.keys())
        for name, mod_dir in mods.items():
            if name not in installed_set:
                continue
            custom_modules.append(name)
            man = parse_manifest(mod_dir / "__manifest__.py")
            ext = (man.get("external_dependencies") or {})
            for p in ext.get("python", []) or []:
                python_declared.add(p)
            for b in ext.get("bin", []) or []:
                apt_deps.add(b)
        req_pkgs, req_files = scan_requirements(repo)
        heuristic = to_pip_names(import_scan(repo, local_modules))
        config_keys = config_key_scan(repo)
        log(f"custom_modules={len(custom_modules)} declared_py={len(python_declared)} "
            f"req_pkgs={len(req_pkgs)} heuristic_py={len(heuristic)} "
            f"config_keys={len(config_keys)}")

    python_deps = sorted(python_declared | req_pkgs | heuristic)
    # deps that came ONLY from the heuristic scan (i.e. undeclared) — surfaced so
    # a human can eyeball them before they get baked into an image.
    undeclared = sorted(heuristic - python_declared - req_pkgs)

    payload = {
        "profile_id": os.environ.get("PROFILE_ID", ""),
        "odoo_series": series,
        "odoo_git_ref": os.environ.get("ODOO_GIT_REF", ""),
        "addons_git_url": os.environ.get("ADDONS_GIT_URL", ""),
        "addons_git_ref": os.environ.get("ADDONS_GIT_REF", ""),
        "installed_modules": installed,
        "custom_modules": sorted(custom_modules),
        "python_deps": python_deps,
        "python_deps_undeclared": undeclared,
        "apt_deps": sorted(apt_deps),
        "requirements_files": req_files,
        "required_config_keys": sorted(config_keys),
    }
    payload["discovery_hash"] = hashlib.sha256(
        json.dumps({k: payload[k] for k in (
            "odoo_series", "odoo_git_ref", "addons_git_ref",
            "custom_modules", "python_deps", "apt_deps",
            "required_config_keys")},
            sort_keys=True).encode()).hexdigest()[:12]

    upload(payload)
    log(f"discovery complete: hash={payload['discovery_hash']} "
        f"python_deps={len(python_deps)} undeclared={len(undeclared)}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except subprocess.CalledProcessError as exc:
        log(f"ERROR: command failed: {exc}")
        sys.exit(2)
    except Exception as exc:  # noqa: BLE001
        log(f"ERROR: {exc}")
        sys.exit(1)
