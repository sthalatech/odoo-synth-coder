# odoo-synth control panel

A thin web UI over the existing masking pipeline. It triggers the same AWS
Fargate tasks the shell scripts run (`deploy/07_mask.sh`,
`deploy/source/restore_dump.sh`), streams the container's CloudWatch logs live to
the browser, and shows the source/target Odoo URLs when a run finishes.

## What it does

- **restore** — recreate the `source` DB and stream a presigned-S3 `dump.sql`
  into it (masker image), then report `res_partner` / `res_users` counts.
- **mask** — run greenmask `source → masked`, neutralize, set the admin
  password, then surface the target ALB URL.

It reads all infra values from the repo's [config.env](../config.env) and
[deploy/state.env](../deploy/state.env), so there is **no config duplication** —
whatever the pipeline uses, the panel uses.

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
| POST | `/api/runs` | start a run `{operation, dump_url?, admin_password?}` |
| GET  | `/api/runs` | recent runs |
| GET  | `/api/runs/{id}` | run detail (status, result, urls) |
| GET  | `/api/runs/{id}/logs` | SSE log stream |
