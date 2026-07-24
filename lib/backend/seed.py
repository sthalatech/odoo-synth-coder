"""Idempotent seed: create a starter profile from the repo's existing config so
the panel is usable out of the box. Provenance (Odoo core ref + addons repo/ref
+ series) is read from config.yaml/state.env; NO secrets are embedded — the source
database URL and any tokens are added later from the UI.

Run with:  python -m backend.seed        (from the lib/ dir)
It is a no-op if any profile already exists.
"""
from __future__ import annotations

from . import config, store


def _v(*keys: str) -> str | None:
    for k in keys:
        val = config.get(k)
        if val:
            return val
    return None


def seed_starter_profile() -> str | None:
    """Create one starter profile if the profile table is empty. Returns the id
    of the created profile, or None if seeding was skipped."""
    if store.list_profiles():
        return None

    addons_url = _v("CUSTOM_ADDONS_GIT_URL")
    label = _v("PROFILE_LABEL", "PROJECT") or "starter"

    pid = "prof_starter"
    store.create_profile(
        pid,
        label,
        description="Seeded from config.yaml — add the source database URL from the UI.",
        odoo_series=_v("ODOO_SERIES"),
        odoo_git_url=_v("ODOO_GIT_URL") or "https://github.com/odoo/odoo",
        odoo_git_ref=_v("ODOO_GIT_REF"),
        addons_git_url=addons_url,
        addons_git_ref=_v("CUSTOM_ADDONS_GIT_REF"),
        needs_enterprise=1 if (_v("NEEDS_ENTERPRISE") or "").lower() in ("1", "true", "yes") else 0,
        enterprise_source=_v("ENTERPRISE_GIT_URL"),
        mask_inputs={"mask_profile": "odoo-core-pii"},
        image_status="draft",
    )
    return pid


if __name__ == "__main__":
    created = seed_starter_profile()
    if created:
        print(f"seeded starter profile: {created}")
    else:
        print("profiles already exist; nothing seeded")
