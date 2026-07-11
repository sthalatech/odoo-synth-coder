# Enterprise addons

Drop your Odoo Enterprise source here, one of two ways:

- **Zip:** place the bundle at `odoo/enterprise.zip`. `deploy/02_build_push.sh`
  auto-unzips it into this folder at build time (flattening a single wrapper
  dir like `enterprise-19.0/`).
- **Unzipped:** copy the enterprise module folders directly into this directory
  (so `odoo/enterprise/<module_name>/__manifest__.py` exists).

The contents are baked into the shared odoo image at `/opt/enterprise` and used
by BOTH the source and target Odoo services.

This folder's contents are gitignored (enterprise is proprietary); only this
README and `.gitkeep` are tracked.
