# openproject-agent-engine

This engine monitors OpenProject project work packages and runs appropriate AI Agents to complete the ticket.

OpenProject is the system of record: the engine polls it, dispatches CLI coding
agents (pi, OpenCode, or any configured CLI) into per-agent clones, verifies the
outcome against GitHub and OpenProject, and performs every status transition
itself.

The implementation plan, workflow state machine and acceptance criteria live in
[`docs/PLAN.md`](docs/PLAN.md).

## Status

Milestone M0 (skeleton) is implemented: package layout, validated configuration
models, JSON logging with secret redaction, CLI entry points, example workflow
configuration, and example systemd deployment files. Polling and dispatch are
not implemented yet.

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
references, placeholder names, unique workspaces and exactly one reviewer) and
prints a summary. It exits 1 with a specific message on the first invalid file.
Use `--json` for machine-readable output.

The engine's own OpenProject IDs (statuses, users, projects, custom field) are
resolved against the API at startup (M1); `check-config` does not contact
OpenProject.

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
