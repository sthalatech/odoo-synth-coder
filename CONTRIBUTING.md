# Contributing

## Setup

Follow the "Quick start" section in the [README](README.md) to get the CLI
running against an existing deployment, or "Deploy the full pipeline" to stand
up your own AWS stack for testing.

## Before you commit

This project's whole job is handling production DB dumps, masking rules, and
cloud credentials — it is unusually easy to accidentally commit a real secret
or a real customer/company name while iterating locally. Before your first
commit in a fresh clone:

- Never edit `.gitignore`-covered files (`config.yaml`, `config.env`,
  `deploy/state.env`, `profiles/*.yaml`) and then `git add -f` them.
- Run a secret scanner (e.g. `gitleaks detect` or `git secrets --scan`) over
  your diff before pushing, especially if you copy examples from a real
  deployment into a doc or test fixture.
- Masking rules under `masker/rules/` and profile YAMLs are the most likely
  place to accidentally embed a real org/company name in a comment — keep
  examples generic (`Acme Corp`, `example.com`).

## Code conventions

- Shell scripts (`deploy/*.sh`): `set -euo pipefail`, match the existing
  `lib.sh` logging helpers rather than introducing new ones.
- Python (`controlpanel/backend/`, `cli/odoo-synth`): no new third-party deps
  without a reason; keep the CLI calling the backend library in-process (no
  HTTP layer — see `controlpanel/README.md`).
- Terraform (`coder/templates/*`): both Coder templates
  (`odoo-synth-env`, `odoo-synth-builder`) must be re-published together when
  shared conventions change — see `deploy/12_publish_template.sh`.

## Testing your change

There's no CI yet, so validate locally before opening a PR:

```bash
bash deploy/00_validate_config.sh    # config + AWS auth + CLI tooling sanity
odoo-synth profile list              # smoke test against a real deployment
```

For Coder template changes, republish to a test Coder server and create a
workspace from it before submitting.

## Pull requests

Keep PRs scoped to one change. Describe what you tested (which command, against
what kind of deployment) in the PR description — there's no CI to fall back on
yet, so this is the only signal reviewers have.
