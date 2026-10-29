# openproject-agent-engine

Polls OpenProject work packages and dispatches CLI coding agents (pi,
OpenCode, or any configured command) to work on them. OpenProject is the
system of record: the engine claims work, prepares a per-agent clone, renders
a prompt, runs the agent, verifies the outcome against GitHub and
OpenProject, and performs **every** status transition itself. Agents never
change status.

## How a ticket flows

| Observed state | Engine action | Run | Verified outcome | Transition |
|---|---|---|---|---|
| `New`, eligible, `AGENT` set | claim, `In Progress` | implement | open PR / `BLOCKED` | `Ready for Review` / `On Hold` |
| `Ready for Review`, PR exists | claim, `In Testing` | review | PR merged / `REJECTED` | `Closed` / `Rejected` |
| `Ready for Review`, no PR | — | — | — | `On Hold` + comment |
| `Rejected`, eligible | claim, `In Progress` | fix | as implement | as implement |

`On Hold` is terminal: the engine ignores the ticket until a human moves it.
Branches are `op-<project>-<wp>` (one per ticket), one active run per agent,
and every engine comment starts with `ENGINE:`. Failed runs are retried up to
`MAX_CRASH_RETRIES`; the rejection cap is `MAX_REJECTIONS`.

## Requirements

- Python 3.12, `git`, `gh`, and the agent CLIs on the service user's `PATH`.
- OpenProject 17 (API v3) with a project, plus one-time administrator access.
- A GitHub repository the service user can clone, push to and merge into.

## Quick start

```bash
git clone <engine-repo> ~/op-agent-engine && cd ~/op-agent-engine
uv tool install .                        # venv fallback: docs/INSTALL.md
op-agent-engine init                     # scaffolds ~/.config/op-agent-engine/
# edit workflow.yaml (project, repo, statuses); in engine.env set OP_BASE_URL
# and OP_PROVISION_TOKEN (an admin token) — agent tokens come later
# one-time OpenProject setup: docs/OPENPROJECT_SETUP.md (statuses, AGENT
# field, roles, workflows)
set -a; . ~/.config/op-agent-engine/engine.env; set +a
op-agent-engine provision --apply        # creates the five users; lists the
                                         # token variables still to fill in
# mint one API token per user (as admin: Administration → Users → user →
# API tokens) and put the values in engine.env
op-agent-engine check-config
op-agent-engine install-service --apply  # installs + enables the systemd service (sudo)
op-agent-engine dry-run                  # logs the dispatch plan, writes nothing
```

The service starts immediately and autostarts on boot. Follow it with
`journalctl -u op-agent-engine -f`.

## Documentation

| Doc | Contents |
|---|---|
| [docs/INSTALL.md](docs/INSTALL.md) | install options, systemd service, updates |
| [docs/OPENPROJECT_SETUP.md](docs/OPENPROJECT_SETUP.md) | one-time instance setup: statuses, `AGENT` field, roles, workflows, tokens |
| [docs/CONFIG.md](docs/CONFIG.md) | `workflow.yaml`, environment variables, CLI reference |
| [docs/RUNBOOK.md](docs/RUNBOOK.md) | status, pause, run inspection, `On Hold` handling, recovery, security |
| [docs/ACCEPTANCE.md](docs/ACCEPTANCE.md) | fake-agent test and manual acceptance checklist |

Design rationale and the workflow state machine:
[docs/PLAN.md](docs/PLAN.md). Facts verified against real tooling:
[docs/VERIFICATION.md](docs/VERIFICATION.md).
