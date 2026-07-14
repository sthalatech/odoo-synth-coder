#!/usr/bin/env python3
"""Generate a per-source **greenmask** masking profile during discovery.

The ECS masker runs greenmask (see masker/entrypoint.sh), so the editable
artifact we produce is a complete greenmask profile: the same common/log/
storage/dump boilerplate as the baked ``odoo-core-pii.yml`` (with the same
``${SOURCE_DB_*}`` / ``${GM_STORAGE}`` / ``${GM_JOBS}`` envsubst placeholders),
plus a ``dump.transformation`` list auto-derived from the *live* source schema:
every PII-shaped column on a public table is assigned a greenmask transformer
by column shape + name.

The operator reviews/edits this YAML before masking runs; the masker downloads
it and uses it verbatim (after envsubst) instead of the baked profile.

Best-effort: a column we cannot classify is left untouched (it passes through
greenmask unchanged, exactly as today).
"""
from __future__ import annotations

import os
import subprocess


# ---------------------------------------------------------------------------
# schema snapshot (columns + FK targets) via psql
# ---------------------------------------------------------------------------

def _psql_rows(query: str) -> list[list[str]]:
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
    return [ln.split("\t") for ln in out.splitlines() if ln.strip()]


def snapshot_schema() -> dict[str, dict[str, dict]]:
    """table -> {column -> {data_type, fk_target}} for public base tables."""
    tables: dict[str, dict[str, dict]] = {}
    cols = _psql_rows(
        "SELECT c.table_name, c.column_name, c.data_type "
        "FROM information_schema.columns c "
        "JOIN information_schema.tables t "
        "  ON t.table_schema=c.table_schema AND t.table_name=c.table_name "
        "WHERE c.table_schema='public' AND t.table_type='BASE TABLE' "
        "ORDER BY c.table_name, c.ordinal_position")
    for row in cols:
        if len(row) < 3:
            continue
        tbl, col, dtype = row[0], row[1], row[2]
        tables.setdefault(tbl, {})[col] = {"data_type": dtype, "fk_target": None}
    fks = _psql_rows(
        "SELECT tc.table_name, kcu.column_name, ccu.table_name "
        "FROM information_schema.table_constraints tc "
        "JOIN information_schema.key_column_usage kcu "
        "  ON tc.constraint_name=kcu.constraint_name "
        "  AND tc.table_schema=kcu.table_schema "
        "JOIN information_schema.constraint_column_usage ccu "
        "  ON ccu.constraint_name=tc.constraint_name "
        "  AND ccu.table_schema=tc.table_schema "
        "WHERE tc.constraint_type='FOREIGN KEY' AND tc.table_schema='public'")
    for row in fks:
        if len(row) < 3:
            continue
        tbl, col, ftbl = row[0], row[1], row[2]
        if tbl in tables and col in tables[tbl]:
            tables[tbl][col]["fk_target"] = ftbl
    return tables


# ---------------------------------------------------------------------------
# classification -> greenmask transformer
# ---------------------------------------------------------------------------

_TEXT_TYPES = {"text", "character varying", "varchar", "char", "character"}

# Technical/enum columns we never scramble even if free-text: doing so would
# break Odoo. The operator can still add them by hand.
_SKIP_COLUMNS = {
    "state", "lang", "tz", "active", "color", "type", "res_model", "model",
    "ref", "code", "currency", "website_url",
    # structural materialized path on any _parent_store model (e.g. "1/5/"):
    # Odoo splits it and casts each segment with int() -> hashing it 500s.
    "parent_path",
}

# Odoo core auth / model-metadata / technical tables the masker handles itself
# (admin-password reset + neutralize) or that carry structural XML-id / model
# data Odoo depends on. Auto-masking their columns is redundant at best and
# destructive at worst: e.g. greenmask hashing res_users.login (UNIQUE/NOT NULL)
# can make greenmask emit the table with ZERO rows, wiping base.public_user /
# user_admin and 500-ing every request. Never auto-transform these; the operator
# can still add rules by hand. Generic across sources -- table *names* only.
_SKIP_TABLES = {
    # authentication & users -- reset/neutralized by the masker post-restore
    "res_users", "res_users_log", "res_users_settings", "res_users_apikeys",
    "res_users_apikeys_description", "auth_totp_device",
    # XML-id / model metadata -- structural; masking breaks external IDs
    "ir_model_data", "ir_model", "ir_model_fields", "ir_model_fields_selection",
    "ir_model_relation", "ir_model_constraint", "ir_module_module",
    "ir_module_module_dependency", "ir_translation", "ir_ui_view", "ir_ui_menu",
    "ir_actions", "ir_act_window", "ir_act_server", "ir_cron", "ir_rule",
    "ir_config_parameter", "ir_model_access", "res_groups",
    # credentials -- neutralized by the masker
    "ir_mail_server", "fetchmail_server", "payment_provider",
    # core reference / locale / config data -- seeded from Odoo XML, parsed by
    # code (e.g. res_lang.week_start is cast with int(); hashing it 500s every
    # web page). Zero customer PII, so never auto-mask.
    "res_lang", "res_country", "res_country_state", "res_country_group",
    "res_currency", "res_currency_rate", "res_bank",
    "decimal_precision", "uom_uom", "uom_category",
}


def transformer_for(column: str, dtype: str, fk_target: str | None) -> dict | None:
    """Return a greenmask transformer dict for a column, or None to leave it."""
    if fk_target:
        return None  # FK (incl. partner ref): structural; target row is masked itself
    base = (dtype or "").lower().split("(")[0].strip()
    low = column.lower()
    if base == "bytea":
        return None  # attachment/image content: greenmask bytea handling varies; skip
    if base not in _TEXT_TYPES:
        return None
    if low in _SKIP_COLUMNS or low.endswith("_id") or low.endswith("_state"):
        return None
    if "email" in low:
        return {"name": "RandomEmail", "column": column, "keep_null": True}
    if any(k in low for k in ("phone", "mobile", "fax")):
        return {"name": "RandomE164PhoneNumber", "column": column, "keep_null": True}
    if any(k in low for k in ("name", "contact", "display_name", "commercial",
                              "first", "last")):
        return {"name": "Replace", "column": column, "value": "REDACTED", "keep_null": True}
    # generic free-text (street, city, vat, website, notes, comments, ...) -> Hash
    return {"name": "Hash", "column": column}


# ---------------------------------------------------------------------------
# plan generation + greenmask YAML render
# ---------------------------------------------------------------------------

def generate_plan(installed_modules: list[str], _baseline_dir=None) -> tuple[str, dict]:
    """Build the editable greenmask profile YAML + stats. Includes every public
    table that has at least one transformable PII-shaped column."""
    schema = snapshot_schema()
    table_transformers: dict[str, list[dict]] = {}
    n_cols = 0
    for table in sorted(schema):
        if table in _SKIP_TABLES:
            continue  # core auth/metadata/credential tables: masker handles them
        tlist: list[dict] = []
        for col in sorted(schema[table]):
            info = schema[table][col]
            t = transformer_for(col, info["data_type"], info["fk_target"])
            if t:
                tlist.append(t)
                n_cols += 1
        if tlist:
            table_transformers[table] = tlist
    yaml_text = _render_greenmask(table_transformers)
    return yaml_text, {"tables": len(table_transformers), "columns": n_cols}


def _q(v: str) -> str:
    return '"' + str(v).replace('"', '\\"') + '"'


def _render_greenmask(table_transformers: dict[str, list[dict]]) -> str:
    lines: list[str] = [
        "# greenmask masking profile (auto-generated during provenance discovery).",
        "# Transformers were derived from the LIVE source schema: PII-shaped text",
        "# columns on public tables are masked. Review/edit before masking runs.",
        "# Columns not listed pass through unchanged. FKs and bytea are skipped",
        "# (the referenced row is masked at its own table).",
        "#",
        "# Rendered by the masker via envsubst (${SOURCE_DB_*}/${GM_STORAGE}/${GM_JOBS}).",
        "common:",
        '  pg_bin_path: "/usr/lib/postgresql/16/bin"',
        "  tmp_dir: /tmp",
        "log:",
        "  level: info",
        "storage:",
        "  type: directory",
        "  directory:",
        "    path: ${GM_STORAGE}",
        "dump:",
        "  pg_dump_options:",
        '    dbname: "host=${SOURCE_DB_HOST} port=${SOURCE_DB_PORT} user=${SOURCE_DB_USER} password=${SOURCE_DB_PASSWORD} dbname=${SOURCE_DB_NAME}"',
        "    jobs: ${GM_JOBS}",
        "  transformation:",
    ]
    for table in sorted(table_transformers):
        lines.append("    - schema: public")
        lines.append(f"      name: {table}")
        if table == "res_partner":
            lines.append("      apply_for_inherited: true")
        lines.append("      transformers:")
        for t in table_transformers[table]:
            params = [f"column: {t['column']}"]
            if "value" in t:
                params.append(f"value: {_q(t['value'])}")
            if t.get("keep_null"):
                params.append("keep_null: true")
            lines.append(
                f"        - {{name: {t['name']}, params: {{{', '.join(params)}}}}}")
    lines.append("")
    return "\n".join(lines)
