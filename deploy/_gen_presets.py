#!/usr/bin/env python3
"""Generate Coder workspace preset blocks from the profile + run stores.

Emits Terraform `data "coder_workspace_preset"` blocks -- one per profile that
has both a built Odoo image AND a successful mask run (so it has a masked dump
to hydrate from). Written to coder/templates/odoo-synth-env/presets.tf, which
`coder templates push` picks up automatically.

A profile becomes a one-click "Create workspace" preset in the Coder dashboard,
carrying its image, dump, git token secret, repo, and discovery-derived
odoo.conf extras -- everything the panel used to pass. Adding a new repo+DB is
now: build + mask it (via the CLI), then re-run `deploy/12_publish_template.sh`.

Stores (no-SQL thin architecture, no SQLite):
  * profiles  -> lib/backend/profile_store.py (one YAML per profile)
  * runs/logs -> lib/backend/run_store.py   (S3: params.json + logs)

Usage: python3 deploy/_gen_presets.py
"""
from __future__ import annotations
import base64
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
# allow running from a checkout without the package installed
sys.path.insert(0, str(REPO / "lib"))
from backend import profile_store, run_store  # noqa: E402

OUT = REPO / "coder" / "templates" / "odoo-synth-env" / "presets.tf"
DEFAULT_INSTANCE = "t3.large"


def _hcl_string(s: str) -> str:
    """Escape a string for an HCL quoted literal."""
    return s.replace("\\", "\\\\").replace('"', '\\"')


def _strip_empty_values(text: str) -> str:
    """Drop ``key =`` lines whose value is empty (nothing after ``=``).

    Keeps comments, blank lines, and any key with a non-empty value (values
    may contain spaces or ``=``). See the call site for why this is needed.
    """
    import re
    out = []
    for ln in text.splitlines():
        # ``key =`` with only whitespace after the = -> skip
        if re.match(r"^\s*[A-Za-z0-9_.]+\s*=\s*$", ln):
            continue
        out.append(ln)
    # preserve a single trailing newline if the input had one
    res = "\n".join(out)
    if text.endswith("\n") and out:
        res += "\n"
    return res


def _latest_mask_dump(profile_id: str) -> str | None:
    """S3 URI of the masked dump from the most recent succeeded mask run for a
    profile, or None if there isn't one yet.

    Skips runs whose recorded dump URI is incomplete (some older runs stored
    only the bucket+prefix, e.g. ``s3://.../masked-dumps/`` with no
    ``<run>/masked.dump`` suffix) -- the env template needs the full object
    key. Returns the newest *valid* URI."""
    best: tuple[float, str] | None = None
    for r in run_store.list_runs(limit=500):
        if r.get("profile_id") != profile_id:
            continue
        if r.get("status") != "succeeded" or r.get("operation") != "mask":
            continue
        full = run_store.get_run(r["id"]) or {}
        uri = (full.get("result") or {}).get("masked_dump_s3_uri")
        if not uri or not uri.endswith("masked.dump"):
            continue
        ts = float(full.get("created_at") or 0)
        if best is None or ts > best[0]:
            best = (ts, uri)
    return best[1] if best else None


def main() -> int:
    blocks: list[str] = []
    seen = 0
    for p in profile_store.list_profiles(limit=500):
        # needs a built image
        if not p.get("image_uri"):
            continue
        if (p.get("image_status") or "") not in ("built", "ready", "discovered"):
            continue
        # needs a successful mask run with a dump
        dump_uri = _latest_mask_dump(p["id"])
        if not dump_uri:
            continue
        # Strip ``key =`` lines with empty values: Odoo's config parser
        # type-checks every option, and an empty string fails for int/bool
        # options (e.g. ``limit_time_real_cron =`` aborts startup with
        # ``invalid integer value: ''`` before any addon runs). Discovery seeds
        # these as placeholders for keys the source DB reads; an empty value
        # is no help to addons (they read config via dict lookup, which returns
        # None/default when the key is absent) and it crashes Odoo. Keep lines
        # with a real value, comments, and blanks.
        conf_extra = _strip_empty_values(p.get("odoo_conf_extra") or "")
        conf_b64 = base64.b64encode(conf_extra.encode()).decode()
        label = (p.get("label") or p["id"]).strip() or p["id"]
        desc = (p.get("description") or f"Profile {p['id']}").strip()
        repo_url = p.get("addons_git_url") or ""
        repo_branch = p.get("addons_git_ref") or ""
        # The git token is a Coder user secret injected as $GIT_TOKEN_<UPPER_ID>;
        # the preset carries the env-var NAME (not the value -- write-only in Coder).
        from backend import profiles as _profiles
        git_tok_env = (_profiles.git_token_env_name(p["id"])
                       if p.get("git_token_secret") else "")
        agent_name = (p.get("agent_name") or "opencode").strip() or "opencode"
        agent_prompt = (p.get("agent_system_prompt") or "").strip()
        # base64 the prompt so multi-line HCL strings stay safe; the template
        # startup script base64-decodes it before staging AGENT_CONTEXT.md.
        agent_prompt_b64 = base64.b64encode(agent_prompt.encode()).decode()
        seen += 1
        res_id = p["id"].replace("-", "_").replace(".", "_")
        blocks.append(f'''# Preset for profile {p["id"]} (auto-generated by deploy/_gen_presets.py)
data "coder_workspace_preset" "profile_{res_id}" {{
  default     = {"true" if seen == 1 else "false"}
  name        = "{_hcl_string(label)}"
  description = "{_hcl_string(desc)}"
  parameters = {{
    odoo_image          = "{_hcl_string(p["image_uri"])}"
    dump_s3_uri         = "{_hcl_string(dump_uri)}"
    git_token_env      = "{_hcl_string(git_tok_env)}"
    instance_type       = "{DEFAULT_INSTANCE}"
    odoo_conf_extra_b64 = "{conf_b64}"
    agent_name          = "{_hcl_string(agent_name)}"
    agent_system_prompt_b64 = "{agent_prompt_b64}"
{f'    repo_url            = "{_hcl_string(repo_url)}"\n' if repo_url else ""}{f'    repo_branch         = "{_hcl_string(repo_branch)}"\n' if repo_branch else ""}  }}
}}
''')

    header = ('# ----------------------------------------------------------------------\n'
              '# Auto-generated by deploy/_gen_presets.py from the profile + run stores.\n'
              '# Do not edit by hand -- re-run deploy/12_publish_template.sh to refresh.\n'
              '# One preset per profile that has a built image + a successful mask run.\n'
              '# ----------------------------------------------------------------------\n\n')
    OUT.write_text(header + "\n".join(blocks))
    sys.stderr.write(f"generated {seen} preset(s) -> {OUT.relative_to(REPO)}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())