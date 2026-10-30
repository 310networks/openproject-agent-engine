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

- Python 3.12, `git`, `gh`, `uv`, and the agent CLIs on the service user's `PATH`.
- OpenProject 17 (API v3) with a project, plus one-time administrator access.
- A GitHub repository the service user can clone, push to and merge into.

## Quick start

1. Clone the repo, copy the config files, and update OP_BASE_URL and OP_PROVISION_TOKEN
```bash
git clone <engine-repo> ~/op-agent-engine && cd ~/op-agent-engine
cp workflow.example.yaml ./workflow.yaml
cp engine.env.example ./engine.env
chmod 600 ./engine.env
```
2. Setup the required statuses, AGENT custom field, and Agent Worker role: [docs/OPENPROJECT_SETUP.md](docs/OPENPROJECT_SETUP.md)

3. Run interactive install.sh script
./install.sh
```bash
./install.sh
```
The installer will ask the user to create API tokens for each of the agent users it created.
It will provide the login so the the user can login as the agent and create the API token.

At the `agent-engine` token prompt it reminds you to make that user an administrator,
which is required for the engine to list users and edit work packages.

The read-only dashboard is served at `http://127.0.0.1:8765` (see [docs/DASHBOARD.md](docs/DASHBOARD.md)).

## Uninstall

```bash
./uninstall.sh            # asks for confirmation
./uninstall.sh --dry-run  # print the plan only
```

## Documentation

| Doc | Contents |
|---|---|
| [docs/INSTALL.md](docs/INSTALL.md) | install options, systemd service, updates |
| [docs/OPENPROJECT_SETUP.md](docs/OPENPROJECT_SETUP.md) | one-time instance setup: statuses, `AGENT` field, Agent Worker role, admin engine user, tokens |
| [docs/CONFIG.md](docs/CONFIG.md) | `workflow.yaml`, environment variables, CLI reference |
| [docs/DASHBOARD.md](docs/DASHBOARD.md) | read-only web UI: engine status, agents, sessions, totals |
| [docs/RUNBOOK.md](docs/RUNBOOK.md) | status, pause, run inspection, `On Hold` handling, recovery, security |
| [docs/ACCEPTANCE.md](docs/ACCEPTANCE.md) | fake-agent test and manual acceptance checklist |

Design rationale and the workflow state machine:
[docs/PLAN.md](docs/PLAN.md). Facts verified against real tooling:
[docs/VERIFICATION.md](docs/VERIFICATION.md).
