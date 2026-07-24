#!/usr/bin/env python3
"""Generate a per-source **greenmask** masking profile during discovery.

The masker runs greenmask (see masker/entrypoint.sh), so the editable
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
    base tables. ``unique`` marks a column covered by ANY UNIQUE index or
    constraint -- single- OR multi-column, full OR partial (WHERE ...). A
    shape-collapsing / non-injective transformer (Masking, RandomPerson, ...) on
    such a column can emit duplicate values; for a multi-column key whose other
    column has few distinct values (e.g. account_move(name, journal_id) WHERE
    state='posted'), two masked rows can collide on the full key and break a
    later CREATE UNIQUE INDEX (Odoo's _auto_init) or the table's COPY on restore.
    So any column in any unique key is left UNMASKED (kept) -- these are
    identifiers/sequences, not free PII. ``not_null`` = NOT NULL column."""
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
            "unique": False, "not_null": (nullable == "NO"),
            "is_selection": False}
    # ANY column of ANY unique index (covers single + multi-column, partial +
    # full, and constraint-backed indexes -- pg_constraint unique constraints
    # are realized as unique indexes, so pg_index alone sees them all).
    uniq = _psql_rows(
        "SELECT t.relname, a.attname "
        "FROM pg_index i "
        "JOIN pg_class t ON t.oid=i.indrelid "
        "JOIN pg_namespace n ON n.oid=t.relnamespace "
        "JOIN pg_attribute a ON a.attrelid=t.oid AND a.attnum=ANY(i.indkey) "
        "WHERE n.nspname='public' AND i.indisunique")
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

    # Odoo SELECTION fields are stored as varchar but hold a fixed vocabulary
    # (out_invoice/in_invoice, posted/draft/cancel, ...). They are NOT PII and
    # must NEVER be masked -- Odoo code does dict lookups keyed by the stored
    # value in computes/onchanges/views (e.g. account.move._get_move_display_name
    # does {'out_invoice': ...}[move.move_type]; masking move_type to '*****'
    # -> KeyError on EVERY record of that model). The name-based _SKIP_COLUMNS /
    # _SKIP_TABLES heuristics can't enumerate the ~590 distinct selection names,
    # so detect them authoritatively from ir_model_fields (ttype='selection').
    # Map Odoo model -> table by replacing '.' with '_' (the Odoo convention;
    # _auto=False/abstract models don't have tables, so their (model,col) won't
    # be in `tables` and are harmlessly skipped).
    sels = _psql_rows(
        "SELECT f.model, f.name "
        "FROM ir_model_fields f "
        "WHERE f.ttype='selection'")
    for row in sels:
        if len(row) < 2:
            continue
        model, field = row[0], row[1]
        tbl = model.replace(".", "_")
        if tbl in tables and field in tables[tbl]:
            tables[tbl][field]["is_selection"] = True
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
    # Odoo view/action metadata enums -- values are small fixed vocabularies
    # (tree,form / kanban / form / graph,...). Masking collapses them to a few
    # shapes -> duplicates -> collides on code-defined unique index
    # act_window_view_unique_mode_per_action (ir_actions._auto_init), crashing
    # `odoo -u`. Not PII.
    "view_mode", "view_type", "binding_type", "usage",
    # structural materialized path on any _parent_store model (e.g. "1/5/"):
    # Odoo splits it and casts each segment with int() -> hashing it 500s.
    "parent_path",
    # account_reports filter_* are selection/boolean-shaped CONFIG values, not
    # free text. Masking them to '********' breaks the stored-field recompute
    # in -u all (ValueError casting '********' to boolean). Not PII.
    "filter_account_type", "filter_hide_0_lines", "filter_hierarchy",
    "filter_multi_company", "filter_domain", "filter_pre_domain", "filter_by",
}

# Tables where a bare ``name`` column holds a PERSON/contact name (genuine PII)
# and SHOULD be masked. On every other table a bare ``name`` is an Odoo
# document SEQUENCE / reference (account.move.name = "INV/2024/0001",
# sale.order.name = "SO/2024/0001", stock.picking.name = "WH/IN/0001", ...) --
# an operational identifier, not PII. Masking such a name (RandomPerson)
# both destroys a useful debugging reference AND risks collisions on the
# code-defined unique indexes Odoo creates at runtime via _auto_init (e.g.
# account_move_unique_name ON (name, journal_id) WHERE state='posted') -- those
# indexes are NOT in the dumped source schema, so the unique-column guard above
# can't see them; the allowlist is the backstop. Keep this list narrow: only
# tables whose ``name`` is truly a human/company display name.
_NAME_IS_PERSON_TABLES = {
    "res_partner", "res_partner_address",       # contact / address book
    "hr_employee", "hr_applicant", "hr_department",
    "res_users",                                # user display name (login masked separately)
    "calendar_event",                           # meeting title often names people
    "mail_alias",                               # alias display name
    "discuss_channel",                          # channel display name (may name people)
}

# Tables where a bare ``name`` column holds a descriptive BUSINESS/ENTITY name
# (a company, warehouse, location, journal, account, product, ...) -- genuine
# identifying data that must be masked, NOT an Odoo document sequence. The
# person-allowlist above does not cover these because their ``name`` is an org
# / object label, not a person. Without this set these names leak unmasked
# (e.g. res_company.name = "Acme Corp Inc", stock_warehouse.name =
# "ACME WAREHOUSE", product_template.name = {"en_US": "Hand Bag"}).
#
# These are the *only* non-person tables whose bare ``name`` we mask; every
# other bare ``name`` is still treated as a document sequence and kept (see the
# note in the name branch below for why -- unique-index collision risk).
_NAME_IS_BUSINESS_TABLES = {
    "res_company",          # legal entity name
    "res_bank",             # bank name
    "stock_warehouse",      # warehouse name
    "stock_location",       # stock location name (UI shows complete_name, but
                             # the short name still leaks in lists/reports)
    "stock_picking_type",   # operation type name ("Receipts", "Delivery Orders")
    "account_journal",      # journal name (translatable -> jsonb)
    "account_account",      # chart-of-accounts name (translatable -> jsonb)
    "account_account_tag",  # account tag label
    "product_template",     # product name (translatable -> jsonb)
    "product_category",     # product category name
    "product_tag",          # product tag name
    "uom_uom",              # unit-of-measure name ("Units", "Dozen")
    "uom_category",         # UoM category name
}
# Bare ``name`` on these tables is structural reference data we must NOT touch
# (masking breaks Odoo's locale/country/currency integrity). Not in the business
# set above; documented here so nobody adds them by mistake.
_DO_NOT_MASK_NAME = {"res_country", "res_country_state", "res_lang", "res_currency"}

# Odoo translatable (jsonb) ``name`` columns: ``{"en_US": "Hand Bag", ...}``.
# greenmask text transformers (Masking/Hash) reject jsonb at validation, and
# RandomPerson/RandomCompany on jsonb emit a bare string ("Joana Sawayn") that
# is INVALID JSON -> COPY fails on restore -> the table comes back EMPTY. The
# robust, self-contained fix is ``Replace`` with a valid JSON document constant
# (keep_null preserves real NULLs). This loses per-row variety but is safe and
# restores cleanly; the operator can swap in a ``Cmd`` transformer later for
# varied realistic names. Map table -> the JSON replacement value.
_BUSINESS_NAME_JSON_REPLACE = {
    "product_template": '{"en_US": "Masked Product"}',
    "account_journal":  '{"en_US": "Masked Journal"}',
    "account_account":  '{"en_US": "Masked Account"}',
    # default for any other jsonb business-name table not listed above
    "_default":         '{"en_US": "Masked"}',
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
    "ir_actions", "ir_act_window", "ir_act_window_view", "ir_act_server",
    "ir_act_report", "ir_act_url", "ir_act_client", "ir_cron", "ir_rule",
    "ir_config_parameter", "ir_model_access", "res_groups",
    "ir_filters", "ir_logging",
    # ir_property: generic property store. value_reference holds "<model>,<id>"
    # references (e.g. "product.pricelist,5") and value_text holds property
    # values. Masking value_reference to '********' breaks the model,id format
    # Odoo parses with int() (pricelist compute -> invalid input syntax for
    # type integer). Not PII -- it's structural config; the referenced ids stay
    # valid against the masked tables. Never auto-mask.
    "ir_property",
    # accounting report CONFIGURATION (Odoo core account_reports). These are
    # structural report-template rows (filter_* are selection/boolean-shaped
    # config, expressions are code-like strings), not customer PII. Masking
    # filter_hide_0_lines etc. to '********' makes Odoo's stored-field recompute
    # during -u all raise ValueError (can't cast '********' to boolean) and abort
    # the schema-reconcile. Same class as the ir_* metadata tables above.
    "account_report", "account_report_column", "account_report_expression",
    "account_report_external_value", "account_report_file_download_error_wizard",
    "account_report_footnote", "account_report_horizontal_group_rule",
    "account_report_line", "account_reports_export_wizard",
    "account_reports_export_wizard_format",
    # credentials -- neutralized by the masker
    "ir_mail_server", "fetchmail_server", "payment_provider",
    # core reference / locale / config data -- seeded from Odoo XML, parsed by
    # code (e.g. res_lang.week_start is cast with int(); hashing it 500s every
    # web page). Zero customer PII, so never auto-mask.
    "res_lang", "res_country", "res_country_state", "res_country_group",
    "res_currency", "res_currency_rate", "res_bank",
    "decimal_precision", "uom_uom", "uom_category",
}


# High-volume, low-value tables whose ROW DATA we drop from the dump (schema is
# kept, so Odoo still boots -- these are logs, chatter/mail history, attachments,
# transient/queue data). This trims the bulk of an Odoo DB (attachments + mail
# are usually the largest tables) without touching business records like
# partners / orders / invoices. Emitted as pg_dump ``--exclude-table-data`` so
# the table exists but comes back empty.
#
# FK SAFETY: emptying a table breaks restore if a *retained* table has a foreign
# key pointing into it (the post-data FK constraint fails). So this is only a
# CANDIDATE set -- the actual exclusions are narrowed at generation time to the
# largest subset that is closed under "is referenced by" (see _safe_exclude_data),
# using the live FK graph. That keeps it correct across Odoo versions/modules
# without hardcoding a specific schema. Override per-source with the env var
# GM_EXCLUDE_TABLE_DATA (comma-separated table names, or "none" to disable).
_DEFAULT_EXCLUDE_TABLE_DATA = {
    # attachments / binaries -- typically the single largest table
    "ir_attachment", "message_attachment_rel",
    # mail / chatter history -- high volume, no business value in a dev replica
    "mail_message", "mail_message_res_partner_rel",
    "mail_message_res_partner_needaction_rel",
    "mail_message_res_partner_starred_rel",
    "mail_notification", "mail_tracking_value", "mail_followers",
    "mail_message_reaction", "mail_message_schedule",
    "mail_mail", "mail_mail_res_partner_rel", "mail_activity",
    # technical logs / transient / bus
    "ir_logging", "bus_bus", "bus_presence", "ir_cron_trigger",
    "base_import_import", "base_import_mapping", "web_editor_converter_test",
    "auditlog_log", "queue_job",  # common OCA high-volume tables (if present)
}


def _safe_exclude_data(schema: dict[str, dict[str, dict]],
                       candidates: set[str]) -> tuple[list[str], list[str]]:
    """Narrow ``candidates`` to the largest subset safe to empty: for every
    excluded table, EVERY table that has a foreign key into it must also be
    excluded (else the retained child's FK constraint fails on restore). We take
    the maximal subset closed under the "referenced-by" relation by iteratively
    dropping any candidate that has a referencer outside the set.

    Returns (safe_to_exclude_sorted, dropped_for_fk_safety_sorted). Only tables
    present in the live schema are considered."""
    present = {t for t in candidates if t in schema}
    # reverse FK graph: target_table -> {tables that reference it}
    referencers: dict[str, set[str]] = {}
    for tbl, cols in schema.items():
        for meta in cols.values():
            tgt = meta.get("fk_target")
            if tgt:
                referencers.setdefault(tgt, set()).add(tbl)

    safe = set(present)
    dropped: set[str] = set()
    changed = True
    while changed:
        changed = False
        for tbl in sorted(safe):
            # tables referencing tbl that are NOT themselves excluded => unsafe
            outside = {r for r in referencers.get(tbl, set())
                       if r != tbl and r not in safe}
            if outside:
                safe.discard(tbl)
                dropped.add(tbl)
                changed = True
    return sorted(safe), sorted(dropped)


def _exclude_candidates() -> set[str] | None:
    """Resolve the exclude-table-data candidate set. Env GM_EXCLUDE_TABLE_DATA
    overrides the default: a comma-separated table list, or "none"/"off" to
    disable data exclusion entirely (returns None)."""
    raw = os.environ.get("GM_EXCLUDE_TABLE_DATA")
    if raw is None:
        return set(_DEFAULT_EXCLUDE_TABLE_DATA)
    val = raw.strip()
    if val.lower() in ("none", "off", "false", ""):
        return None
    return {t.strip() for t in val.split(",") if t.strip()}


# Row-level date subsetting (keep only the last N days of high-volume
# TRANSACTIONAL tables) is handled by the MASKER after restore, NOT here:
# greenmask's dump-time subset engine panics on Odoo's schema ("more than one
# cycle group found in SCC") because the core tables form one strongly-connected
# component with multiple FK cycles. The masker instead runs a generic,
# FK-cascading DELETE keyed off GM_SUBSET_DAYS (see masker/entrypoint.sh), which
# Postgres can always execute regardless of schema cyclicity.


def transformer_for(column: str, dtype: str, fk_target: str | None,
                    unique: bool = False, table: str = "",
                    is_selection: bool = False) -> dict | None:
    """Return a greenmask transformer dict for a column, or None to leave it.

    ``unique`` = the column is part of ANY unique index/constraint (single- or
    multi-column, full or partial). Masking such a column risks collisions on
    the unique key (the account_move(name, journal_id) partial unique index is
    the canonical Odoo case: RandomPerson names collide across posted moves and
    break _auto_init's CREATE UNIQUE INDEX). These columns are identifiers /
    sequences, not free PII, so they are LEFT UNMASKED (return None -> keep).
    """
    if fk_target:
        return None  # FK (incl. partner ref): structural; target row is masked itself
    if is_selection:
        # Odoo selection field (detected from ir_model_fields.ttype='selection'):
        # a fixed-vocabulary enum (out_invoice/draft/posted/...), NOT PII. Odoo
        # code keys dicts off these values in computes/onchanges/views, so
        # masking them ('*****') raises KeyError on every record of the model.
        return None
    base = (dtype or "").lower().split("(")[0].strip()
    low = column.lower()
    if base == "bytea":
        return None  # attachment/image content: greenmask bytea handling varies; skip
    is_json = base in ("json", "jsonb")
    # Bare ``name`` on a business/entity table (company, warehouse, journal,
    # account, product, ...) is a descriptive label, not a document sequence --
    # mask it. This must run BEFORE the _TEXT_TYPES guard below so jsonb
    # translatable names (product_template.name = {"en_US": "Hand Bag"}) are
    # handled: text transformers reject jsonb, and RandomPerson/RandomCompany on
    # jsonb emit invalid JSON that breaks restore (see _BUSINESS_NAME_JSON_REPLACE).
    if (low == "name" and table in _NAME_IS_BUSINESS_TABLES
            and table not in _DO_NOT_MASK_NAME):
        if is_json:
            # jsonb translatable name. RandomCompany/RandomPerson on jsonb emit
            # a bare string -> invalid JSON -> COPY fails -> empty table, so use
            # Replace with a valid JSON constant. If the column is UNIQUE a
            # constant would collide on the unique index -- but no Odoo jsonb
            # ``name`` is unique in practice (verified: account_journal,
            # account_account, product_template names are all non-unique). Guard
            # anyway: leave a unique jsonb name unmasked rather than break
            # restore with a guaranteed collision.
            if unique:
                return None
            val = _BUSINESS_NAME_JSON_REPLACE.get(
                table, _BUSINESS_NAME_JSON_REPLACE["_default"])
            return {"name": "Replace", "column": column,
                    "value": val, "keep_null": True}
        # text name on a business table: a realistic company / entity name.
        # RandomCompany ("Epic Valley Inc.") fits org/warehouse/bank/journal/
        # account labels better than a person name. For a UNIQUE name column
        # (res_company.name, stock_warehouse.name both have single-column
        # UNIQUE indexes in real Odoo DBs) use engine=hash: it is deterministic
        # per input -> injective -> never collides on distinct names, so the
        # unique index survives. For non-unique names use engine=random for
        # maximum variety.
        engine = "hash" if unique else "random"
        return {"name": "RandomCompany", "column": column,
                "template": "{{ .CompanyName }} {{ .CompanySuffix }}",
                "engine": engine}
    if not is_json and base not in _TEXT_TYPES:
        return None
    if is_json:
        # Only the bare-name business case above handles jsonb; all other jsonb
        # columns pass through (greenmask text transformers don't apply, and we
        # don't generically rewrite arbitrary jsonb here).
        return None
    if low in _SKIP_COLUMNS or low.endswith("_id") or low.endswith("_state"):
        return None
    # Part of any unique key: keep the real value. Masking (even Hash on a
    # multi-column key) can collide and break a unique index/constraint on
    # restore or a later Odoo _auto_init. Unique columns are identifiers, not PII.
    if unique:
        return None
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
        # Person-ish name column. BUT: a BARE ``name`` on a non-person table is
        # an Odoo document sequence/reference (account.move / sale.order /
        # stock.picking / mrp.production / pos.order / ... = "INV/2024/0001"),
        # not PII. Masking it destroys a useful reference and risks collisions
        # on code-defined unique indexes (account_move_unique_name) that aren't
        # in the dumped schema. So only mask a bare ``name`` on tables in the
        # person allowlist; otherwise keep it. Compound name columns
        # (partner_name, display_name, commercial_company_name, first_name, ...)
        # are still PII caches and get masked regardless of table.
        bare_name = (low == "name")
        if bare_name and table not in _NAME_IS_PERSON_TABLES:
            return None  # document sequence / operational reference -- keep
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
                                 info.get("unique", False), table,
                                 info.get("is_selection", False))
            if t:
                tlist.append(t)
                n_cols += 1
        if tlist:
            table_transformers[table] = tlist
    # high-volume tables to dump schema-only (rows dropped), narrowed to the
    # FK-safe subset so the restore never fails on an orphaned foreign key.
    candidates = _exclude_candidates()
    exclude_data: list[str] = []
    dropped_unsafe: list[str] = []
    if candidates:
        exclude_data, dropped_unsafe = _safe_exclude_data(schema, candidates)
    # a table dumped schema-only (exclude-table-data) has no rows, so a row
    # filter on it is pointless -- drop any overlap.
    excluded_set = set(exclude_data)
    # NOTE: row-level date subsetting is intentionally NOT emitted as greenmask
    # `subset_conds` here. greenmask's subset engine panics on Odoo's schema
    # ("more than one cycle group found in SCC") because the core tables form a
    # single strongly-connected component with multiple FK cycles. Instead the
    # masker prunes old rows AFTER restore (GM_SUBSET_DAYS) with a generic,
    # FK-cascading DELETE that Postgres can always execute. See masker/entrypoint.sh.
    yaml_text = _render_greenmask(table_transformers, exclude_data, dropped_unsafe)
    return yaml_text, {"tables": len(table_transformers), "columns": n_cols,
                       "exclude_table_data": len(exclude_data)}


def _q(v: str) -> str:
    return '"' + str(v).replace('"', '\\"') + '"'


def _render_greenmask(table_transformers: dict[str, list[dict]],
                      exclude_table_data: list[str] | None = None,
                      dropped_unsafe: list[str] | None = None) -> str:
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
    ]
    if exclude_table_data:
        lines.append(
            "    # Row data dropped for these high-volume tables (schema kept so")
        lines.append(
            "    # Odoo still boots). Narrowed to the FK-safe subset. Edit freely.")
        lines.append("    exclude-table-data:")
        for t in exclude_table_data:
            lines.append(f"      - public.{t}")
    if dropped_unsafe:
        lines.append(
            "    # NOT excluded (a retained table has a FK into them; emptying")
        lines.append(
            f"    #   would break restore): {', '.join(dropped_unsafe)}")
    lines.append("  transformation:")
    # emit an entry for every table that needs a transformer.
    for table in sorted(table_transformers):
        lines.append("    - schema: public")
        lines.append(f"      name: {table}")
        if table == "res_partner":
            lines.append("      apply_for_inherited: true")
        tlist = table_transformers.get(table)
        if not tlist:
            continue
        lines.append("      transformers:")
        for t in tlist:
            # RandomPerson / RandomCompany have a nested `columns` param (each
            # with its own go-template), so they can't use the single-line
            # params form.
            if t["name"] in ("RandomPerson", "RandomCompany"):
                lines.append(f"        - name: {t['name']}")
                lines.append("          params:")
                # RandomCompany supports engine=random|hash; hash is injective
                # (deterministic per input) so it is safe on UNIQUE name
                # columns (res_company.name, stock_warehouse.name). RandomPerson
                # is only ever emitted on non-unique person names, so it has no
                # engine param.
                if t.get("engine"):
                    lines.append(f"            engine: {t['engine']}")
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
