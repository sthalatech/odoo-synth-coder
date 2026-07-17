# Custom addons

Put each of your custom module folders here, e.g.:

```
odoo/custom-addons/
  my_module/__manifest__.py
  another_module/__manifest__.py
```

They are baked into the shared odoo image at `/opt/custom` (used by BOTH the
source and target services) and take priority over enterprise/community/core.

Alternatives:
- Git: set `custom_addons_git_url` in `config.yaml` (cloned to
  `/mnt/extra-addons-custom`).
- Live edit-reload (no rebuild): mount an EFS volume into the task and set the
  container env `EXTRA_ADDONS_PATH` to the mount path (highest priority).

Rebuild + redeploy after changes: `bash deploy/02_build_push.sh` then
`aws ecs update-service ... --force-new-deployment`.
