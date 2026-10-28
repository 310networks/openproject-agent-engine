# openproject-agent-engine

This engine monitors OpenProject project work packages and runs appropriate AI Agents to complete the ticket.

OpenProject is the system of record: the engine polls it, dispatches CLI coding
agents (pi, OpenCode, or any configured CLI) into per-agent clones, verifies the
outcome against GitHub and OpenProject, and performs every status transition
itself.

The implementation plan, workflow state machine and acceptance criteria live in
[`docs/PLAN.md`](docs/PLAN.md).

## Status

Milestones M0 (skeleton) and M1 (OpenProject client and startup resolution) are
implemented: package layout, validated configuration models, JSON logging with
secret redaction, CLI entry points, example deployment files, an async
OpenProject API client with retries and pagination, and startup resolution of
statuses, users, projects, types and the `AGENT` custom field. Polling and
dispatch are not implemented yet.

## Development setup

Requires Python 3.12.

```bash
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
.venv/bin/pytest
.venv/bin/ruff check .
.venv/bin/mypy
```

## Configuration check

`workflow.example.yaml` contains `${VAR}` references that must be supplied by
the environment (the real values live in
`~/.config/op-agent-engine/engine.env`, mode `0600`):

```bash
cp config/workflow.example.yaml ~/.config/op-agent-engine/workflow.yaml
cp deploy/engine.env.example ~/.config/op-agent-engine/engine.env
chmod 600 ~/.config/op-agent-engine/engine.env
# edit both files, then:
set -a; . ~/.config/op-agent-engine/engine.env; set +a
op-agent-engine check-config --config ~/.config/op-agent-engine/workflow.yaml
```

`check-config` validates the whole file (including `${VAR}` resolution, runner
references, placeholder names, unique workspaces and exactly one reviewer),
then contacts OpenProject and resolves every configured status, user, project,
project type and the `AGENT` custom field option. It prints each resolved ID,
exits 1 with a specific message on the first invalid file or unresolved item,
and supports `--json` for machine-readable output. Use `--offline` to validate
the file without contacting OpenProject.

Status names must match the OpenProject instance exactly (case-sensitive). The
`agent-engine` API token needs the global `manage_user` permission (or
administrator) because startup resolves agent logins through `GET /api/v3/users`.
See `docs/VERIFICATION.md` for the live-instance findings.

## Deployment

`deploy/op-agent-engine.service` is the systemd unit template, and
`deploy/engine.env.example` is the environment file template. Replace
`<host-user>` and the token placeholders before installing. The unit's `PATH`
must contain every agent CLI; see the comments in the unit file.

## Verified external behavior

Facts confirmed against the real CLIs and host during M0 (pi/OpenCode flags,
OpenProject CLI environment variables, PATH and authentication) are recorded in
[`docs/VERIFICATION.md`](docs/VERIFICATION.md), together with what remains to be
verified in later milestones.
