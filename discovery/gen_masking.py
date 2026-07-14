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
    """table -> {column -> {data_type, fk_target, unique, not_null}} for public
    base tables. ``unique`` marks a column covered by a SINGLE-column UNIQUE or
    PRIMARY KEY constraint (a shape-collapsing transformer on such a column emits
    duplicate values -> the whole table fails its COPY on restore and lands
    EMPTY). ``not_null`` marks a NOT NULL column (must never be nulled out)."""
    tables: dict[str, dict[str, dict]] = {}
    cols = _psql_rows(
        "SELECT c.table_name, c.column_name, c.data_type, c.is_nullable "
        "FROM information_schema.columns c "
        "JOIN information_schema.tables t "
        "  ON t.table_schema=c.table_schema AND t.table_name=c.table_name "
        "WHERE c.table_schema='public' AND t.table_type='BASE TABLE' "
        "ORDER BY c.table_name, c.ordinal_position")
    for row in cols:
        if len(row) < 4:
            continue
        tbl, col, dtype, nullable = row[0], row[1], row[2], row[3]
        tables.setdefault(tbl, {})[col] = {
            "data_type": dtype, "fk_target": None,
            "unique": False, "not_null": (nullable == "NO")}
    # single-column UNIQUE / PRIMARY KEY constraints: a masked value that
    # collides here fails the table's COPY on restore (table ends up empty).
    # Only single-column constraints matter -- a multi-column unique key can
    # still be satisfied even if one masked column repeats.
    uniq = _psql_rows(
        "SELECT t.relname, a.attname "
        "FROM pg_constraint con "
        "JOIN pg_class t ON t.oid=con.conrelid "
        "JOIN pg_namespace n ON n.oid=t.relnamespace "
        "JOIN pg_attribute a ON a.attrelid=t.oid AND a.attnum=ANY(con.conkey) "
        "WHERE n.nspname='public' AND con.contype IN ('u','p') "
        "  AND array_length(con.conkey,1)=1")
    for row in uniq:
        if len(row) < 2:
            continue
        tbl, col = row[0], row[1]
        if tbl in tables and col in tables[tbl]:
            tables[tbl][col]["unique"] = True
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


def transformer_for(column: str, dtype: str, fk_target: str | None,
                    unique: bool = False) -> dict | None:
    """Return a greenmask transformer dict for a column, or None to leave it.

    ``unique`` = the column is covered by a single-column UNIQUE/PK constraint.
    Shape-collapsing transformers (Masking/Replace) would emit duplicate values
    on such a column, which fails the table's COPY on restore and leaves the
    table EMPTY. So a unique text column is masked with ``Hash`` instead, which
    is deterministic and distinctness-preserving (each distinct input -> a
    distinct output), keeping the constraint satisfiable. Generic across sources.
    """
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
    # UNIQUE/PK text column: only a distinctness-preserving transformer is safe.
    if unique:
        return {"name": "Hash", "column": column}
    # The greenmask `Masking` transformer keeps the value *shape* while hiding
    # the content (e.g. "John Smith" -> "Jo** *****"), which is far more useful
    # in a dev replica than a constant "REDACTED". Its `type` param picks the
    # masking style; we classify by column name (generic across sources). NOTE:
    # Masking accepts ONLY `column` + `type` -- it rejects `keep_null` with a
    # fatal validation error, so we must not emit it here (it masks NULLs to a
    # masked-empty value, which is harmless for these free-text columns).
    def _mask(mtype: str) -> dict:
        return {"name": "Masking", "column": column, "type": mtype}
    if "email" in low:
        return _mask("email")
    if any(k in low for k in ("phone", "mobile", "fax", "tel")):
        return _mask("mobile")
    if any(k in low for k in ("credit_card", "card_number", "cc_number")):
        return _mask("credit_card")
    if any(k in low for k in ("vat", "ssn", "tin", "passport", "aadhaar",
                              "national_id", "tax_id")):
        return _mask("id")
    if any(k in low for k in ("zip", "postcode", "postal")):
        return _mask("postcode")
    if any(k in low for k in ("street", "addr", "city")):
        return _mask("addr")
    if any(k in low for k in ("url", "website", "web")):
        return _mask("url")
    if any(k in low for k in ("name", "contact", "display_name", "commercial",
                              "first", "last")):
        # Person-ish name column: replace with a fully random person name via
        # greenmask RandomPerson. Unlike Masking type=name (which keeps the
        # first+last char of every word AND the word count -> "I**a Y**a
        # C**ter" is trivially reconstructable), RandomPerson emits a brand-new
        # realistic name with ZERO derivability from the original. We pick a
        # template so first/last-name columns stay single-token and generic
        # name columns get a full name.
        if "first" in low:
            tmpl = "{{ .FirstName }}"
        elif "last" in low:
            tmpl = "{{ .LastName }}"
        else:
            tmpl = "{{ .FirstName }} {{ .LastName }}"
        return {"name": "RandomPerson", "column": column, "template": tmpl}
    # generic free-text (notes, comments, description, ...) -> default masking
    return _mask("default")


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
            t = transformer_for(col, info["data_type"], info["fk_target"],
                                 info.get("unique", False))
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
            # RandomPerson has a nested `columns` param (each with its own
            # go-template), so it can't use the single-line params form.
            if t["name"] == "RandomPerson":
                lines.append("        - name: RandomPerson")
                lines.append("          params:")
                lines.append("            columns:")
                lines.append(f"              - name: {t['column']}")
                lines.append(f"                template: {_q(t['template'])}")
                continue
            params = [f"column: {t['column']}"]
            if "type" in t:
                params.append(f"type: {t['type']}")
            if "value" in t:
                params.append(f"value: {_q(t['value'])}")
            if t.get("keep_null"):
                params.append("keep_null: true")
            lines.append(
                f"        - {{name: {t['name']}, params: {{{', '.join(params)}}}}}")
    lines.append("")
    return "\n".join(lines)
