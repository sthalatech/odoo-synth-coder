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
| GET  | `/api/environments/config` | whether developer environments are configured |
| GET  | `/api/environments` | list developer environments |
| POST | `/api/environments` | launch an environment (seed from a run's masked dump) |
| GET  | `/api/environments/{id}` | environment detail |
| DELETE | `/api/environments/{id}` | tear down (terminate instance + delete secret) |

## Developer environments (VS Code, per issue)

Ephemeral, isolated **code-server** boxes seeded from a masked `pg_dump`
artifact — one EC2 instance per environment, from a **thin** golden AMI
(Ubuntu + docker + code-server; **Odoo is not baked in**). At boot the instance:

1. starts a **local postgres** and restores the masked dump into it;
2. pulls the **provenance-baked odoo image** from ECR (core pinned to the run's
   git ref + the discovered `external_dependencies` — the same image the masked
   instance runs, so dependency discovery never repeats here) and runs it against
   the local DB;
3. clones the developer's **addons repo** into the workspace and **bind-mounts it
   live into Odoo** (`EXTRA_ADDONS_PATH`, highest addons_path priority), so edits
   reload without a rebuild;
4. starts code-server behind a per-environment password (Secrets Manager).

The env inherits the exact odoo image from the run it seeds from (captured on the
run as `odoo_image`), so the code matches the masked data. Developers get a
running instance fast and never touch infra/provenance setup.

Setup (one-time):

1. Launch a fresh Ubuntu instance, run
   [environments/provision.sh](environments/provision.sh), then bake an AMI
   from it (`aws ec2 create-image`). Thin AMI — no Odoo baked in.
2. Security group: inbound `code_port` (8443) and `odoo_port` (8069) from your
   CIDR; egress to S3 + ECR. Instance profile: read the masked-dump S3 prefix,
   `secretsmanager:GetSecretValue` on `odoo-synth/env/*` (+ the git-token secret),
   and **ECR pull** (`ecr:GetAuthorizationToken`, `ecr:BatchGetImage`,
   `ecr:GetDownloadUrlForLayer`).
3. Set in `config.env` / `deploy/state.env`: `ENV_AMI_ID`, `ENV_SG_ID`,
   `ENV_SUBNET_ID`, `ENV_INSTANCE_PROFILE`, `ENV_KEY_NAME` (optional),
   `ENV_REPO_URL` / `ENV_REPO_BRANCH` (default to `CUSTOM_ADDONS_GIT_URL/REF`),
   and `ENV_GIT_TOKEN_SECRET` (optional, for a private addons repo).

From the **Environments** page (or a run's *create env* action): pick a mask run
that produced a dump, optionally override the addons repo/branch → an instance
boots, restores the masked DB, runs Odoo, and starts code-server. The `vscode`
column links to the editor, `odoo` to the masked app. Tear down when the issue
closes.

> Per-env auth is a random Secrets-Manager password today; put an
> org-restricted `oauth2-proxy` in front for production.

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
