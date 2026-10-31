# openproject-agent-engine

Polls OpenProject work packages and dispatches CLI coding agents (pi,
OpenCode, or any configured command) to work on them. OpenProject is the
system of record: the engine claims work, prepares a per-agent clone, renders
a prompt, runs the agent, verifies the outcome against GitHub and
OpenProject, and performs **every** status transition itself. Agents never
change status.

The engine runs as a container. The image carries Python, `git`, `gh`, the
OpenProject CLI and the agent CLIs; you supply configuration and credentials in
one directory.

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

- Docker Engine with Compose v2 (nothing else is installed on the host).
- OpenProject 17 (API v3) with a project, plus one-time administrator access.
- A GitHub repository the engine can clone, push to and merge into.

## Quick start

```bash
git clone <engine-repo> && cd openproject-agent-engine/docker
vim config/engine.env            # OP_BASE_URL + OP_PROVISION_TOKEN + AGENT (see below)
vim config/workflow.yaml         # projects, repositories, agents
vim config/pi/auth.json          # provider key: pi (or config/opencode/opencode.json for opencode)
./provision-users.sh             # create the accounts, mint one token each
docker compose up -d
```

Before the first run, complete the one-time instance setup — statuses, the
`AGENT` custom field and the Agent Worker role — in
[docs/OPENPROJECT_SETUP.md](docs/OPENPROJECT_SETUP.md).

`./provision-users.sh` creates the OpenProject accounts (the engine runtime user
plus one per agent) and then interviews you for one API token each: it shows
each generated password once at that account's token prompt, so you can sign in
as the agent, create the token under **My account → Access tokens**, and paste
it. At the `agent-engine` prompt it reminds you to make that user an
administrator first (**Administration → Users → agent-engine → Administrator**),
which is what lets the engine list users and edit work packages. Tokens are
written to `config/engine.env` (mode `0600`) and the script finishes with
`check-config`. Re-running is safe and is how you fix a token that belongs to
the wrong account; `--reprompt-tokens` asks for all of them again.

The dashboard is served at `http://127.0.0.1:8765`
(see [docs/DASHBOARD.md](docs/DASHBOARD.md)).

## The deployment directory

`docker/` is the whole deployment: copy it to `/opt/op-agent-engine` (or
anywhere else) and run `docker compose up -d` there. Everything is pulled from
Docker Hub — nothing is built on the host — and nothing outside `config/` is
mounted, so no UID/GID matching or home-directory layout is involved.

| Path | What it is | Committed? |
|---|---|---|
| `docker-compose.yml` | `engine`, `dashboard` and the one-shot `provision` / `cleanup` / `check-config` tasks | yes |
| `config/` | **your** configuration and credentials, mounted read-only at `/config` | templates yes, secrets no |
| `provision-users.sh` | creates the OpenProject accounts and collects one token per account | yes |
| `config-sync.sh` | stages `~/.pi/agent`, `~/.gitconfig` and `~/.config/opencode` into `config/` (checkout only) | yes |
| `Dockerfile`, `entrypoint.sh` | how the image is built and started — built by CI, not by you | yes |
| `version.json` | the released tag; must equal `project.version` in `pyproject.toml` | yes |

`config/workflow.yaml` is explained in [docs/CONFIG.md](docs/CONFIG.md); the
repository root's `workflow.example.yaml` is the starting point. With the
repository checked out, `./config-sync.sh` fills in `config/` from your own `~`
and seeds `workflow.yaml` and `engine.env` for you.

Image internals, upgrading and troubleshooting: [docs/DOCKER.md](docs/DOCKER.md).

## `config/engine.env`

Read by compose **on the host** and passed to the container as environment
variables, so it may stay mode `0600` and is never mounted.

### Required

| Variable | Meaning |
|---|---|
| `OP_BASE_URL` | your OpenProject URL, e.g. `https://openproject.example.com` |
| `OP_ENGINE_TOKEN` | API token of the runtime user (must be an administrator) |
| `OP_PROVISION_TOKEN` | API token of an administrator, used **only** by `./provision-users.sh` and `docker compose run --rm provision`; can be removed afterwards |
| `OP_TOKEN_*` | one token per agent account, referenced from `workflow.yaml` as `${OP_TOKEN_*}` (e.g. `OP_TOKEN_FRONTEND`) |
| `AGENT` | which agent CLI the agents run: `pi` or `opencode`; `workflow.yaml` uses it as `runner: "${AGENT}"`, so switching is one line here |

### Paths

Defaults come from the image, so they usually do not belong in `engine.env`.
They follow the volume mounts in `docker-compose.yml`.

| Variable | Container default | Meaning |
|---|---|---|
| `ENGINE_CONFIG` | `/config/workflow.yaml` | the workflow document |
| `WORKSPACE_ROOT` | `/home/agent/agents` | agent workspaces (`agents/` from the image, plus the git clones) |
| `DATA_DIR` | `/home/agent/.local/share/op-agent-engine` | SQLite store, run directories, heartbeat, lock |
| `PROMPTS_DIR` | `/opt/op-agent-engine/prompts` | prompt templates shipped in the image |

### Limits and timing

| Variable | Default | Meaning |
|---|---|---|
| `POLL_INTERVAL_SECONDS` | `60` | how often OpenProject is polled |
| `MAX_CONCURRENT_RUNS` | `1` | agent runs in parallel |
| `MAX_REJECTIONS` | `3` | rejections before an agent stops retrying a work package |
| `MAX_CRASH_RETRIES` | `2` | retries after a crashed run |
| `RUN_TIMEOUT_SECONDS` | `3600` | hard limit per run |
| `STOP_GRACE_SECONDS` | `60` | shutdown budget; `stop_grace_period` in compose is 90 s |
| `RUN_RETENTION_DAYS` | `30` | `cleanup` removes run directories older than this |
| `ENGINE_PAUSED` | `false` | `true` stops new runs without stopping the container |
| `LOG_LEVEL` | `INFO` | `DEBUG` for verbose logs |

### Dashboard

| Variable | Container default | Meaning |
|---|---|---|
| `DASHBOARD_HOST` | `0.0.0.0` | bind address inside the container |
| `DASHBOARD_PORT` | `8765` | must stay `8765`: that is the port the compose mapping publishes |

### Optional

| Variable | Meaning |
|---|---|
| `GH_TOKEN` | GitHub token for the agents' `git`/`gh`; without it, pushes need another credential |
| `OPENROUTER_API_KEY` | not read by the engine — see "Agent provider credentials" below |

### Read by compose, not by the engine

Set these in your shell or in a `.env` file next to `docker-compose.yml`:

| Variable | Default | Meaning |
|---|---|---|
| `OP_AGENT_ENGINE_TAG` | `latest` | image tag to run, e.g. `0.1.0` to pin a release |
| `DASHBOARD_BIND` | `127.0.0.1` | host interface that publishes `DASHBOARD_PORT`; the dashboard has no authentication |

## Agent configuration (`config/`, not env)

| Path | Seeded to | Notes |
|---|---|---|
| `workflow.yaml` | `ENGINE_CONFIG` (read in place) | required: statuses, projects, agents, runners |
| `gitconfig` | `~/.gitconfig` | recommended, otherwise `git commit` in a run fails |
| `pi/settings.json` | `~/.pi/agent/settings.json` | default provider/model and pi preferences |
| `pi/auth.json` | `~/.pi/agent/auth.json` | pi provider keys |
| `pi/skills/` | `~/.pi/agent/skills/` | skills available to every run |
| `opencode/opencode.json` | `~/.config/opencode/opencode.json` | only with `INSTALL_OPENCODE=true` |
| `opencode/auth.json` | `~/.local/share/opencode/auth.json` | where opencode keeps credentials |

These files are templates with placeholder values: edit them in place, nothing
has to be copied. Permissions matter: the container runs as its own user (uid
10001), so everything here must be world-readable (`0644`) — except
`engine.env`, which the host side reads and which may stay `0600`.
`config-sync.sh` sets this up correctly.

`.gitignore` keeps `engine.env`, `workflow.yaml`, `pi/skills/` and
`opencode/auth.json` out of git, but the templates above *are* tracked, so
`git status` will show your edits once you paste a real key into
`config/pi/auth.json`. Never commit that: this directory is yours, not the
project's.

### Agent provider credentials

The agent CLIs need a model provider key. Two ways, both without baking
anything into the image:

1. `config/pi/auth.json` (`AGENT=pi`) or the `apiKey` in
   `config/opencode/opencode.json` (`AGENT=opencode`) — edit the template that
   is already there.
2. An environment variable in the runner's `env:` block in
   `config/workflow.yaml`, e.g. `env: { OPENROUTER_API_KEY: "${OPENROUTER_API_KEY}" }`
   with the value in `config/engine.env`. The engine only forwards a fixed set
   of variables (`PATH`, `HOME`, `USER`, `LOGNAME`, `TZ`, `LANG`, `LC_ALL`) to
   the agent, so a key in `engine.env` alone is not enough.

## Day-to-day commands

```bash
docker compose logs -f engine                # follow the engine
docker compose run --rm check-config         # validate config without polling
docker compose run --rm provision --apply    # create/refresh OpenProject users
docker compose run --rm cleanup              # retention sweep
docker compose restart                       # after editing workflow.yaml or pi/*
docker compose up -d                         # after editing engine.env
docker compose pull && docker compose up -d  # upgrade to the published image
docker compose down                          # stop; volumes survive
docker compose down -v                       # stop and delete all state (re-clones repos)
```

`up -d`, not `restart`, after editing `config/engine.env`: a changed env file is
only picked up when the container is recreated.

Schedule the retention sweep on the host:

```cron
0 4 * * * cd /opt/op-agent-engine && docker compose run --rm cleanup
```

## Uninstall

```bash
docker compose down -v    # stops the stack and deletes workspaces, runs and state
```

Then delete the deployment directory. The OpenProject accounts and their tokens
remain; remove or lock them in **Administration → Users**.

## Documentation

| Doc | Contents |
|---|---|
| [docs/DOCKER.md](docs/DOCKER.md) | image internals: what is baked in, config staging, volumes, releases, troubleshooting |
| [docs/OPENPROJECT_SETUP.md](docs/OPENPROJECT_SETUP.md) | one-time instance setup: statuses, `AGENT` field, Agent Worker role, admin engine user, tokens |
| [docs/CONFIG.md](docs/CONFIG.md) | `workflow.yaml`, environment variables, CLI reference |
| [docs/DASHBOARD.md](docs/DASHBOARD.md) | read-only web UI: engine status, agents, sessions, totals |
| [docs/RUNBOOK.md](docs/RUNBOOK.md) | status, pause, run inspection, `On Hold` handling, recovery, security |
| [docs/ACCEPTANCE.md](docs/ACCEPTANCE.md) | fake-agent test and manual acceptance checklist |

Design rationale and the workflow state machine:
[docs/PLAN.md](docs/PLAN.md). Facts verified against real tooling:
[docs/VERIFICATION.md](docs/VERIFICATION.md).
