# odoo-synth control panel

A thin web UI over the existing masking pipeline. It triggers the same AWS
Fargate tasks the shell scripts run (`deploy/07_mask.sh`,
`deploy/source/restore_dump.sh`), streams the container's CloudWatch logs live to
the browser, and shows the source/target Odoo URLs when a run finishes.

## What it does

One operation: **mask**.

- **Source** = a live Postgres database you point at with a connection URL
  (`postgresql://user:pass@host:5432/dbname`), entered per run. greenmask dumps
  and masks it directly.
- **Destination** = always created by us on the configured RDS (resolved
  server-side from env) and dropped + recreated on each run. You are **not**
  asked where it goes.
- **Output** = the masked DB is served by the managed Odoo (target URL). You can
  optionally tick "produce a downloadable pg_dump of the masked DB" to get a
  presigned download link in the result.

All non-infra options are driven by [config.yml](config.yml): the managed
**destination** (secrets resolved server-side from env vars — never sent to the
browser), selectable **mask profiles** (map to `masker/profiles/<id>.yml`), and
neutralize **toggle defaults**. Infra values come from the repo's
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
| GET  | `/api/profiles` | mask profiles + toggle defaults + destination info (drives the form) |
| POST | `/api/runs` | start a mask run with the selected inputs |
| GET  | `/api/runs` | recent runs |
| GET  | `/api/runs/{id}` | run detail (status, result, urls, masked-dump link) |
| GET  | `/api/runs/{id}/logs` | SSE log stream |

## Masker knobs (env, honored by `masker/entrypoint.sh`)

The panel passes these to the masker task; they're all overridable and default
safely, so the masker also stays fully configurable when run outside the panel:

`SOURCE_DB_*` (the live source, parsed from the URL), `MASK_PROFILE`, `GM_JOBS`,
`NEUTRALIZE_MAIL`, `NEUTRALIZE_FETCHMAIL`, `NEUTRALIZE_PAYMENT`,
`NEUTRALIZE_SMTP_PARAM`, `RESET_ADMIN_LOGIN`, and `MASKED_DUMP_PUT_URL`
(presigned S3 PUT; when set, the masker `pg_dump`s the masked DB and uploads it
for download).

> Note: changes to `masker/entrypoint.sh` / `masker/profiles/` require
> rebuilding + pushing the masker image (`deploy/02_build_push.sh`) to take
> effect on real runs.
