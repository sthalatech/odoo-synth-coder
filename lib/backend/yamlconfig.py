"""Shared config.yaml -> flat env-var loader.

Single source of truth for turning the repo's ``config.yaml`` (the structured
single-source-of-truth config) into the flat ``KEY=value`` env-var set the
shell pipeline scripts expect. Used by two callers:

  * ``lib/backend/config.py``       - imports :func:`load_env_dict` in-process
    (no subprocess) so the Python backend reads the exact same values the
    shell pipeline does.
  * ``deploy/_yaml_to_env.py``      - a thin CLI wrapper that shells out to
    :func:`emit_shell_env` so ``deploy/lib.sh`` can ``eval "$(python3
    deploy/_yaml_to_env.py)"`` and export the same vars.

Secrets are resolved via the ``ref:`` form:
  ``{ ref: env:VAR }``        -> value of env var ``VAR``
  ``{ ref: ssm:/path/name }`` -> decrypted value of the SSM Parameter
Plain scalars are taken literally.

This module must stay import-safe with only the stdlib + PyYAML. ``boto3`` is
imported lazily (only when an ``ssm:`` ref is actually encountered).
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

import yaml

# Explicit mapping from auto-flattened YAML keys -> the env-var names the
# pipeline scripts expect (only where they differ from the UPPER_SNAKE form).
ALIASES = {
    "AWS_REGION": "AWS_REGION",
    "AWS_PROJECT": "PROJECT",
    "ODOO_SERIES": "ODOO_SERIES",
    "ODOO_IMAGE": "ODOO_IMAGE",
    "ODOO_GIT_URL": "ODOO_GIT_URL",
    "ODOO_GIT_REF": "ODOO_GIT_REF",
    "ODOO_ADMIN_PASSWORD": "ODOO_ADMIN_PASSWORD",
    "ODOO_MASTER_PASSWORD": "ODOO_MASTER_PASSWORD",
    "ADDONS_CUSTOM_GIT_URL": "CUSTOM_ADDONS_GIT_URL",
    "ADDONS_CUSTOM_GIT_REF": "CUSTOM_ADDONS_GIT_REF",
    "ADDONS_ENTERPRISE_ZIP": "ENTERPRISE_ZIP",
    "DATABASE_PG_MAJOR": "PG_MAJOR",
    "DATABASE_SOURCE_NAME": "SOURCE_DB_NAME",
    "DATABASE_MASKED_NAME": "TARGET_DB_NAME",
    "DATABASE_TARGET_USER": "TARGET_DB_USER",
    "DATABASE_TARGET_PASSWORD": "TARGET_DB_PASSWORD",
    "DATABASE_SOURCE_MASTER_USER": "SOURCE_DB_MASTER_USER",
    "DATABASE_SOURCE_MASTER_PASSWORD": "SOURCE_DB_MASTER_PASSWORD",
    "BUILD_ECR_REPO": "ECR_REPO",
    "BUILD_BUILDER_INSTANCE_TYPE": "BUILDER_INSTANCE_TYPE",
    "MASK_GREENMASK_VERSION": "GREENMASK_VERSION",
    "MASK_DUMPS_BUCKET": "DUMP_S3_BUCKET",
    "MASK_DUMPS_PREFIX": "DUMP_S3_PREFIX",
    "CODER_INSTANCE_TYPE": "CODER_INSTANCE_TYPE",
    "CODER_VOLUME_GB": "CODER_VOLUME_GB",
    "CODER_PORT": "CODER_PORT",
    "CODER_SESSION_TOKEN": "CODER_SESSION_TOKEN",
    "CODER_ANTHROPIC_SECRET_NAME": "CODER_ANTHROPIC_SECRET_NAME",
}


def ref_value(node: Any) -> str:
    """Resolve a ``ref:`` node to a string, or return scalars as-is.

    A ``ref`` dict is a leaf: ``{ ref: env:VAR }`` / ``{ ref: ssm:/name }``.
    Scalars are stringified; bools become ``true``/``false``; ``None`` -> ``""``.
    SSM lookups are best-effort: on failure a warning is written to stderr and
    an empty string is returned (matching the historical behaviour).
    """
    if isinstance(node, dict) and "ref" in node:
        spec = str(node["ref"])
        if spec.startswith("env:"):
            return os.environ.get(spec[4:], "")
        if spec.startswith("ssm:"):
            name = spec[4:]
            try:
                import boto3  # noqa: F401  (lazy: only needed for ssm refs)
                ssm = boto3.client(
                    "ssm", region_name=os.environ.get("AWS_REGION", "us-east-1"))
                r = ssm.get_parameter(Name=name, WithDecryption=True)
                return r["Parameter"]["Value"]
            except Exception as e:  # noqa: BLE001
                sys.stderr.write(f"WARN: could not read SSM {name}: {e}\n")
                return ""
        return spec
    if isinstance(node, bool):
        return "true" if node else "false"
    if node is None:
        return ""
    return str(node)


def _flatten(prefix: str, node: Any, out: dict[str, str]) -> None:
    # a ref-secret dict is a LEAF -- resolve it, don't recurse into its keys.
    if isinstance(node, dict) and "ref" in node:
        out[prefix.upper()] = ref_value(node)
        return
    if isinstance(node, dict):
        for k, v in node.items():
            _flatten(f"{prefix}_{k}" if prefix else k, v, out)
    elif isinstance(node, list):
        return  # lists (mask profiles etc.) are not flattened to env vars
    else:
        out[prefix.upper()] = ref_value(node)


def load_env_dict(path: Path) -> dict[str, str]:
    """Load a config.yaml file and return its flat ``{ENV_VAR: value}`` dict.

    The ``state`` top-level block (documentation-only) is skipped. Raises
    ``FileNotFoundError`` if *path* does not exist -- callers decide whether
    that's fatal (the backend treats a missing config.yaml as a hard error)
    or recoverable (the shell wrapper falls back to config.example.yaml).
    """
    doc = yaml.safe_load(path.read_text()) or {}
    flat: dict[str, str] = {}
    for top, node in doc.items():
        if top == "state":
            continue  # documentation-only block
        _flatten(top, node, flat)
    out: dict[str, str] = {}
    for k, v in flat.items():
        out[ALIASES.get(k, k)] = v
    return out


def emit_shell_env(path: Path) -> str:
    """Return ``KEY='value'`` lines (shell-sourceable) for *path*.

    Values are single-quoted with embedded single-quotes escaped via the
    standard ``'"'"'`` idiom so ``eval`` reproduces them verbatim.
    """
    out: dict[str, str] = load_env_dict(path)
    lines: list[str] = []
    for k, v in out.items():
        esc = v.replace("'", "'\"'\"'")
        lines.append(f"{k}='{esc}'")
    return "\n".join(lines)
