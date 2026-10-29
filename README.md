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
cp workflow.example.yaml ./workflow.yaml
cp engine.env.example ./engine.env
chmod 600 ./engine.env
# edit workflow.yaml (project, repo, statuses) and engine.env
# (OP_BASE_URL and OP_PROVISION_TOKEN — an admin token).
# One-time OpenProject setup first: docs/OPENPROJECT_SETUP.md
# (statuses, AGENT field, Agent Worker role).
./install.sh
```

`install.sh` is an interactive installer. It plays a short robot intro,
installs the package with `uv`, copies your two files into
`~/.config/op-agent-engine/`, provisions the OpenProject users, prints each
new user's generated password, then prompts for that user's API token one at
a time, verifies each token, installs the systemd service and runs a dry
poll. Passwords are shown once; tokens are written to both `engine.env` files
with mode `600` and are never printed. At the `agent-engine` token prompt it
reminds you to make that user an administrator, which is required for the
engine to list users and edit work packages.

The service starts immediately and autostarts on boot. Follow it with
`journalctl -u op-agent-engine -f`.

The manual equivalent is in [docs/INSTALL.md](docs/INSTALL.md); `uv` is the
only installer prerequisite.

## Uninstall

```bash
./uninstall.sh            # asks for confirmation
./uninstall.sh --dry-run  # print the plan only
```

The uninstaller stops and removes the systemd units, removes
`~/.config/op-agent-engine/` and `~/.local/share/op-agent-engine/`, deletes
the checkout's `workflow.yaml`/`engine.env` and their backups, removes or
locks the OpenProject users, then uninstalls the `uv` tool. The
administrator `agent-engine` account is never deleted by the API; the script
reminds you to remove it manually under **Administration → Users**. Use
`--keep-config`, `--keep-data`, `--keep-users` or `--keep-tool` to retain a
layer; `--purge-agent-repos` also deletes `WORKSPACE_ROOT/*/repositories`.

## Documentation

| Doc | Contents |
|---|---|
| [docs/INSTALL.md](docs/INSTALL.md) | install options, systemd service, updates |
| [docs/OPENPROJECT_SETUP.md](docs/OPENPROJECT_SETUP.md) | one-time instance setup: statuses, `AGENT` field, Agent Worker role, admin engine user, tokens |
| [docs/CONFIG.md](docs/CONFIG.md) | `workflow.yaml`, environment variables, CLI reference |
| [docs/RUNBOOK.md](docs/RUNBOOK.md) | status, pause, run inspection, `On Hold` handling, recovery, security |
| [docs/ACCEPTANCE.md](docs/ACCEPTANCE.md) | fake-agent test and manual acceptance checklist |

Design rationale and the workflow state machine:
[docs/PLAN.md](docs/PLAN.md). Facts verified against real tooling:
[docs/VERIFICATION.md](docs/VERIFICATION.md).
