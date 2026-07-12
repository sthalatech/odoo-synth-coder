# odoo-synth control panel

A thin web UI over the existing masking pipeline. It triggers the same AWS
Fargate tasks the shell scripts run (`deploy/07_mask.sh`,
`deploy/source/restore_dump.sh`), streams the container's CloudWatch logs live to
the browser, and shows the source/target Odoo URLs when a run finishes.

## What it does

- **restore** — load a dump into a target database. The dump source is
  **selectable**:
  - `dump.sql` via URL
  - Odoo backup `.zip` via URL (dump.sql extracted from it)
  - upload a `dump.sql` (staged to S3, then streamed in)
  - upload an Odoo backup `.zip` (staged to S3, extracted, streamed in)
  - a **live database DSN** (`postgresql://…`) — `pg_dump`'d straight into the target
- **mask** — run greenmask `source → target` using a selectable **masking
  profile**, then neutralize (per-step **toggles**) and set the admin password.

All inputs are driven by [config.yml](config.yml): named **connection profiles**
(secrets resolved server-side from env vars — never sent to the browser),
**mask profiles** (map to `masker/profiles/<id>.yml`), restore **source types**,
and neutralize **toggle defaults**. Infra values come from the repo's
[config.env](../config.env) and [deploy/state.env](../deploy/state.env), so there
is **no config duplication and nothing hardcoded**.

## Architecture

- **Backend**: FastAPI + boto3. One worker thread per run does
  `ecs:RunTask` and tails CloudWatch (`logs:GetLogEvents`), persisting log lines
  to SQLite so history survives restarts.
- **Streaming**: Server-Sent Events (`/api/runs/{id}/logs`).
- **Frontend**: single static page (no build step) — form, live log pane,
  result URLs, recent-runs table.

```
controlpanel/
  backend/
    main.py       FastAPI app + SSE + worker thread
    pipeline.py   boto3 ECS orchestration + CloudWatch tail
    store.py      SQLite persistence (runs + logs)
    config.py     reads ../config.env + ../deploy/state.env
  frontend/       index.html + style.css + app.js
  Dockerfile
  run_local.sh
```

## Run locally

Needs AWS credentials in the environment (the same IAM identity the deploy
scripts use — it must allow `ecs:RunTask`, `ecs:RegisterTaskDefinition`,
`ecs:DescribeTasks`, `ec2:Describe*`, `logs:GetLogEvents`, `sts:GetCallerIdentity`).

```bash
cd controlpanel
./run_local.sh          # http://localhost:8000
```

## Run in Docker

```bash
cd controlpanel
cp ../config.env .              # build context needs these two files
cp ../deploy/state.env .
docker build -t odoo-synth-panel .
docker run --rm -p 8000:8000 \
  -e AWS_ACCESS_KEY_ID -e AWS_SECRET_ACCESS_KEY -e AWS_REGION \
  odoo-synth-panel
```

(For a persistent internal deployment, run this container on the existing ECS
cluster behind the ALB with the `odoo-synth-exec`/task role granting the ECS +
logs permissions above, instead of static keys.)

## API

| Method | Path | Purpose |
| --- | --- | --- |
| GET  | `/api/config` | non-secret infra summary |
| GET  | `/api/profiles` | connection profiles, mask profiles, restore source types, toggle defaults |
| POST | `/api/upload` | stage an uploaded `.sql`/`.zip` to S3 → returns a presigned URL |
| POST | `/api/runs` | start a run (restore/mask) with the selected inputs |
| GET  | `/api/runs` | recent runs |
| GET  | `/api/runs/{id}` | run detail (status, result, urls) |
| GET  | `/api/runs/{id}/logs` | SSE log stream |

## Masker knobs (env, honored by `masker/entrypoint.sh`)

The panel passes these to the masker task; they're all overridable and default
safely, so the masker also stays fully configurable when run outside the panel:

`MASK_PROFILE` (which `profiles/<id>.yml`), `GM_JOBS`, `NEUTRALIZE_MAIL`,
`NEUTRALIZE_FETCHMAIL`, `NEUTRALIZE_PAYMENT`, `NEUTRALIZE_SMTP_PARAM`,
`RESET_ADMIN_LOGIN`.

> Note: changes to `masker/entrypoint.sh` / `masker/profiles/` require
> rebuilding + pushing the masker image (`deploy/02_build_push.sh`) to take
> effect on real runs.
