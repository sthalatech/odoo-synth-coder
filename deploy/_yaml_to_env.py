#!/usr/bin/env python3
"""Load config.yaml and emit KEY=value lines for shell sourcing.

Resolves `ref:` secret forms:
  { ref: env:VAR }        -> value of env var VAR
  { ref: ssm:/path/name } -> value of SSM Parameter (decrypted)
Plain scalars are taken literally. Exports a flat KEY=VALUE set that matches
the same KEY=VALUE env-var names the pipeline scripts expect.

Usage:  python3 deploy/_yaml_to_env.py [path/to/config.yaml]
Default path: repo-root config.yaml (falls back to config.example.yaml).
"""
from __future__ import annotations
import os
import sys
from pathlib import Path

try:
    import yaml
except ImportError:
    sys.stderr.write("PyYAML is required: pip3 install --user pyyaml\n")
    sys.exit(2)

REPO = Path(__file__).resolve().parents[1]


def _ref(node) -> str:
    """Resolve a ref: node to a string, or return scalars as-is."""
    if isinstance(node, dict) and "ref" in node:
        spec = str(node["ref"])
        if spec.startswith("env:"):
            return os.environ.get(spec[4:], "")
        if spec.startswith("ssm:"):
            name = spec[4:]
            try:
                import boto3  # noqa: F401
                ssm = boto3.client("ssm", region_name=os.environ.get("AWS_REGION", "us-east-1"))
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


def _flatten(prefix: str, node, out: dict) -> None:
    # a ref-secret dict is a LEAF -- resolve it, don't recurse into its keys.
    if isinstance(node, dict) and "ref" in node:
        out[prefix.upper()] = _ref(node)
        return
    if isinstance(node, dict):
        for k, v in node.items():
            _flatten(f"{prefix}_{k}" if prefix else k, v, out)
    elif isinstance(node, list):
        return  # lists (mask profiles etc.) are not flattened to env vars
    else:
        out[prefix.upper()] = _ref(node)


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


def main() -> None:
    path = Path(sys.argv[1]) if len(sys.argv) > 1 else (REPO / "config.yaml")
    if not path.exists():
        alt = REPO / "config.example.yaml"
        if alt.exists():
            sys.stderr.write(f"WARN: {path} not found; using {alt}\n")
            path = alt
        else:
            sys.stderr.write("ERROR: no config.yaml and no config.example.yaml\n")
            sys.exit(2)
    doc = yaml.safe_load(path.read_text()) or {}
    flat: dict[str, str] = {}
    for top, node in doc.items():
        if top == "state":
            continue  # documentation-only block
        _flatten(top, node, flat)
    out: dict[str, str] = {}
    for k, v in flat.items():
        out[ALIASES.get(k, k)] = v
    for k, v in out.items():
        esc = v.replace("'", "'\"'\"'")
        print(f"{k}='{esc}'")


if __name__ == "__main__":
    main()
