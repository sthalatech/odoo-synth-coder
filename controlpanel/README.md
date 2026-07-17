# odoo-synth control panel → CLI (Phase C2)

The FastAPI web panel has been replaced by a single CLI: **`odoo-synth`**
(lives at [`../cli/odoo-synth`](../cli/odoo-synth)). The CLI calls the **same
backend library modules** in `backend/` directly over an in-process call
boundary — there is no HTTP server any more. Run history and logs are still
persisted to the same SQLite store (`backend/controlpanel.db`), so everything
the panel tracked survives across CLI invocations.

> **User management** is no longer exposed here — use the native Coder CLI:
>   `coder users create alice@example.com` / `coder users list`.
> **Workspace creation** is via the Coder dashboard (presets) or
>   `coder create -t odoo-synth-env ...`. The CLI still launches/tears down
>   developer environments via `odoo-synth env ...` (it calls `coder create`
>   under the hood).

## Install

```bash
cli/install.sh                       # symlinks /usr/local/bin/odoo-synth -> cli/odoo-synth
# or, manually:
sudo ln -sf /home/exedev/odoo-synth-coder/cli/odoo-synth /usr/local/bin/odoo-synth
```

Run from anywhere — the CLI resolves the repo root from its own location and
adds `controlpanel/` to `sys.path` so it can `from backend import …`.

## Commands

```
odoo-synth --help
odoo-synth profile create --label <l> --source-dsn postgresql://… [--ssh-* …] [--odoo-series 19.0 …]
odoo-synth profile list [--json]
odoo-synth profile show <id>
odoo-synth profile update <id> [--label …] [--source-dsn …]
odoo-synth profile delete <id>
odoo-synth profile discover <id>            # synchronous; streams logs to stdout
odoo-synth profile build <id>               # synchronous; streams logs to stdout
odoo-synth profile images <id> [--json]
odoo-synth profile images delete <id> --image <uri>
odoo-synth profile masking-rules <id> [--json]
odoo-synth profile masking-rules <id> --set <file|->   # update from YAML (file or stdin)
odoo-synth profile masking-rules <id> --reset          # reset to discovered plan
odoo-synth run mask --profile <id>                     # profile path (produces a dump)
odoo-synth run mask --source-dsn <dsn> [--mask-profile …] [--produce-dump] [--ssh-* …]  # legacy inline
odoo-synth run list [--json]
odoo-synth run show <id>
odoo-synth run logs <id> [--follow]                    # print stored logs; --follow polls
odoo-synth env list [--json]
odoo-synth env create --profile-id <id> | --source-run-id <id> | --dump-s3-uri s3://…
odoo-synth env show <id>
odoo-synth env password <id>
odoo-synth env delete <id>
odoo-synth env config
odoo-synth config                            # non-secret infra summary
```

`--verbose` shows full tracebacks on error; otherwise errors print one line to
stderr and exit non-zero.

## How long-running ops work

`profile discover`, `profile build`, and `run mask` run **synchronously in the
foreground**. They create a run row (`store.create_run`), mark it `running`,
call the backend op (`discovery.run_discovery` / `build.run_build` /
`pipeline.run_operation`) with an `emit` sink that **prints each log line to
stdout AND appends it to the run's log table**, then `store.update_run` with the
result. This mirrors the panel's `_worker` exactly, minus the background thread
and SSE plumbing — so `odoo-synth run logs <id>` replays the same persisted
logs afterwards.

## Architecture

```
cli/odoo-synth            argparse CLI (this is the whole UI now)
controlpanel/
  backend/                reusable library (unchanged)
    profiles.py           source-binding profiles + run_params
    discovery.py          discovery op + masking-rule validation
    build.py              provenance image build
    pipeline.py           mask op (ECS Fargate or Coder runner)
    store.py              SQLite persistence (runs + logs + envs + profiles)
    seed.py               best-effort starter profile
    config.py             reads ../config.env + ../deploy/state.env + config.yml
    environments.py       Coder workspace lifecycle (env create/teardown/list)
    controlpanel.db       the SQLite store (read by the CLI)
  config.yml              mask profiles + neutralize defaults + env settings
```

The panel server (`main.py`, `frontend/`, `Dockerfile`, `run_local.sh`) was
removed. Infra values still come from the repo's
[`../config.env`](../config.env) and [`../deploy/state.env`](../deploy/state.env)
via `backend/config.py`; mask profiles + neutralize toggle defaults still come
from [`config.yml`](config.yml) — no config duplication, nothing hardcoded.

## Masker knobs (env, honored by `masker/entrypoint.sh`)

The CLI passes these to the masker (same as the panel did); all overridable and
defaulting safely, so the masker also stays fully configurable when run outside
the CLI:

`SOURCE_DB_*` (the live source, parsed from the URL), `MASK_PROFILE`,
`GM_JOBS`, `NEUTRALIZE_MAIL`, `NEUTRALIZE_FETCHMAIL`, `NEUTRALIZE_PAYMENT`,
`NEUTRALIZE_SMTP_PARAM`, `RESET_ADMIN_LOGIN`, and `MASKED_DUMP_PUT_URL`
(presigned S3 PUT; when set, the masker `pg_dump`s the masked DB and uploads it
for download).

> Note: changes to `masker/entrypoint.sh` / `masker/profiles/` require
> rebuilding + pushing the masker image (`deploy/02_build_push.sh`) to take
> effect on real runs.
