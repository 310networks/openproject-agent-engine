# openproject-agent-engine

This engine monitors OpenProject project work packages and runs appropriate AI Agents to complete the ticket.

OpenProject is the system of record: the engine polls it, dispatches CLI coding
agents (pi, OpenCode, or any configured CLI) into per-agent clones, verifies the
outcome against GitHub and OpenProject, and performs every status transition
itself.

The implementation plan, workflow state machine and acceptance criteria live in
[`docs/PLAN.md`](docs/PLAN.md).

## Status

Milestones M0 (skeleton), M1 (OpenProject client and startup resolution),
M2 (polling and eligibility), M3 (store and claim protocol),
M4 (runner and implementer flow), M5 (reviewer flow), M6 (rejection loop and
comments), M7 (recovery, shutdown and pause) and M8 (agent provisioning and
project scoping) are implemented: package
layout, validated configuration models,
JSON logging with secret redaction, CLI entry points, example deployment
files, an async OpenProject API client with retries and pagination, startup
resolution of statuses, users, projects, types and the `AGENT` custom field,
candidate polling with all eligibility rules and dispatch ordering, a durable
SQLite run store with the optimistic-locking claim protocol, and the execution
layer: a generic runner with process groups, timeouts and result parsing,
per-agent workspace preparation (clone/fetch/checkout), Jinja2 prompt
rendering, implementer dispatch and outcome verification (`PR_OPENED`,
`BLOCKED`, crash retry with escalation), reviewer dispatch (`In Testing`
claim, PR lookup before the run, merged-PR approval to `Closed`, verified
rejection to `Rejected`, and `On Hold` for a missing PR or a `BLOCKED`
result), and the rejection loop: fix-pass dispatch with the latest reviewer
feedback and later human comments, rejection counting from the work package
history, and escalation to `On Hold` (with an engine comment) when a
rejection would exceed `MAX_REJECTIONS`. M7 adds startup reconciliation of
`In Progress` and `In Testing` work packages (resume and re-review runs),
PID-reuse-safe cleanup of leftover agent process groups, graceful shutdown
with `STOP_GRACE_SECONDS`, pause controls, the heartbeat file and the
`status` command. M8 adds OpenProject user and scoped-membership
reconciliation (`op-agent-engine provision`), per-agent `projects` scope in
eligibility, and verification that each configured agent token belongs to the
agent's login.

## Provisioning and project scope

The engine owns status transitions, so the OpenProject permission model is
deliberately narrow:

- Each agent user is a project member with the `Agent Worker` role: view work
  packages and add comments (`add_work_package_comments`), **no workflow
  transitions**. Agent users must be assignable.
- The `agent-engine` user has the `Agent Engine` role in each monitored
  project: view/edit work packages, add comments, and the transitions listed
  in PLAN section 7 (`New` -> `In Progress`, `In Progress` -> `Ready for
  Review`, `Ready for Review` -> `In Testing`, `In Testing` -> `Closed` /
  `Rejected`, `Rejected` -> `In Progress`, and any handled status -> `On
  Hold`). Configure these per role and type in OpenProject.
- The `AGENT` custom field, statuses, types and workflow transitions remain
  manual administrator setup.

`workflow.yaml` declares the role names under
`openproject.provisioning` and each agent's project scope under
`agents.<KEY>.projects` (`["*"]` means every enabled project; an explicit
list must name declared projects). `provision` is a dry-run unless `--apply`
is passed:

```bash
set -a; . ~/.config/op-agent-engine/engine.env; set +a
op-agent-engine provision --config ~/.config/op-agent-engine/workflow.yaml          # plan
op-agent-engine provision --apply --config ~/.config/op-agent-engine/workflow.yaml  # write
```

It creates missing users (never changes a login, never deletes or deactivates),
updates a differing display name/email, and creates or corrects one membership
per agent per scoped project plus the engine membership. A second run performs
no writes. `provision` and `check-config` use `OP_PROVISION_TOKEN` (falling
back to `OP_ENGINE_TOKEN` only when that token is an administrator); runtime
dispatch always uses the least-privilege `OP_ENGINE_TOKEN`.

**OpenProject API limit:** roles cannot be created and their permissions
cannot be read through the API (verified; see `docs/VERIFICATION.md` item 13).
Create the two roles manually first using
[`docs/OPENPROJECT_ADMIN_SETUP.md`](docs/OPENPROJECT_ADMIN_SETUP.md), which
lists the exact role permissions and workflow transitions. `provision` fails
with a specific message if a role is missing and prints a manual-verification
note for permissions. API tokens are also minted only in the UI; `provision`
reports missing `OP_TOKEN_*` variables and `check-config` fails if a configured
token belongs to a different login.

## Operations

The engine writes `DATA_DIR/heartbeat` after every poll. Inspect it with:

```bash
op-agent-engine status          # heartbeat age, active runs, last 10 runs
op-agent-engine status --json
```

Pause new dispatches without stopping running agents by setting
`ENGINE_PAUSED=true` in `engine.env` (restart required) or by creating an
empty `DATA_DIR/PAUSE` file (picked up on the next poll; delete it to resume).
On `SIGTERM` the engine stops polling and dispatching, waits up to
`STOP_GRACE_SECONDS` for running agents, then terminates the remaining
process groups and marks their runs `interrupted`. The next start resumes
those work packages from `In Progress` / `In Testing`.

The unit can optionally use the systemd watchdog: uncomment `Type=notify` and
`WatchdogSec=` in `deploy/op-agent-engine.service`. The engine sends
`READY=1` after startup resolution and `WATCHDOG=1` after every poll.

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
