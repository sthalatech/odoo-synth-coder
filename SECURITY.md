# Security Policy

## Reporting a vulnerability

Please **do not** open a public GitHub issue for security vulnerabilities.

Instead, email **security@<your-domain>** with:

- A description of the issue and its potential impact.
- Steps to reproduce (a minimal repro is ideal).
- The version/commit you tested against.

We'll acknowledge your report within a few business days and follow up with a
fix timeline once we've confirmed the issue.

## Scope

This project provisions AWS infrastructure, moves production database dumps
through a masking pipeline, and launches developer sandboxes with generated
credentials. In-scope concerns include (non-exhaustive):

- PII/secrets surviving the masking pipeline (`masker/rules/*.yml`,
  `masker/entrypoint.sh`) into a "masked" dump or dev environment.
- Privilege escalation or credential leakage between isolated Coder
  workspaces, or from a workspace back to the control-plane AWS account.
- Secrets (DB passwords, Coder session tokens, Secrets Manager ARNs) leaking
  into logs, generated Terraform state, or committed config.
- Injection issues in the CLI/backend (`cli/odoo-synth`,
  `controlpanel/backend/`) when handling profile input, SSH/bastion config, or
  shell-outs in `deploy/*.sh`.

## Out of scope

- Vulnerabilities requiring access to a deployer's own AWS credentials or
  `config.yaml` (protecting those is the deployer's responsibility).
- Issues in third-party components (Odoo, Greenmask, Coder) — report those
  upstream.

## Supported versions

This project does not yet maintain release branches; security fixes land on
`main`.
