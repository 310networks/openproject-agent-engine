# openproject-agent-engine

Polls OpenProject work packages and dispatches CLI coding agents (currently supports pi,
OpenCode) to work on them. OpenProject is the
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

- Docker
- OpenProject 17 (API v3) with a project, plus one-time administrator access.
- A GitHub token with access to the repositories being worked on. Must be able to push and merge.

## Quick start

```bash
git clone <engine-repo> && cd openproject-agent-engine/docker
./provision-users.sh             # asks for the deployment values, the admin
                                 # token, then creates the users and tokens
vim config/pi/auth.json          # provider key: pi (or config/opencode/opencode.json for opencode)
docker compose up -d
```

Before the first run, complete the one-time instance setup — statuses, the
`AGENT` custom field and the Agent Worker role — in
[docs/OPENPROJECT_SETUP.md](docs/OPENPROJECT_SETUP.md).

`./provision-users.sh` is the whole setup: it asks for the OpenProject URL,
project identifier and repository, asks for an OpenProject administrator token
(used once, never stored), creates the engine runtime user plus one account per
agent, shows each generated password once at that account's token prompt, and
verifies every token before writing `docker/.env` (mode `0600`). It finishes
with `check-config`. Re-running is safe and is how you fix a token that belongs
to the wrong account; `--reprompt-tokens` asks for all of them again.

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
| `.env` | **your** deployment values and API tokens; written by `provision-users.sh` | template yes, secrets no |
| `config/` | agent CLI credentials, settings, skills and git identity, mounted read-only at `/config` | templates yes, secrets no |
| `provision-users.sh` | interactive setup: creates the accounts, collects one token each and writes `.env` | yes |
| `config-sync.sh` | stages `~/.pi/agent`, `~/.gitconfig` and `~/.config/opencode` into `config/` (checkout only) | yes |
| `Dockerfile`, `entrypoint.sh` | how the image is built and started — built by CI, not by you | yes |
| `version.json` | the released tag; must equal `project.version` in `pyproject.toml` | yes |

Compose reads `.env` automatically and passes it to the containers; the file is
never mounted. The repository root's `docker/.env.example` is the commented
reference. With the repository checked out, `./config-sync.sh` fills in
`config/` from your own `~` and seeds `.env` for you.

Image internals, upgrading and troubleshooting: [docs/DOCKER.md](docs/DOCKER.md).

## Environment variables

The engine has no configuration file: the team, the runners and the status
names are baked into the image, and everything deployment-specific is an
environment variable in `docker/.env`. `provision-users.sh` writes the required
ones for you.

### Required

| Variable | Meaning |
|---|---|
| `OP_BASE_URL` | your OpenProject URL, e.g. `https://openproject.example.com` |
| `OP_PROJECT` | identifier of the project the engine works on |
| `GITHUB_REPO` | repository as `owner/name`, e.g. `310networks/example-project` |
| `OP_ENGINE_TOKEN` | API token of the runtime user (must be an administrator) |
| `AGENT` | which agent CLI the agents run: `pi` or `opencode` |
| `OP_TOKEN_FRONTEND` | API token of `frontendEngineer` |
| `OP_TOKEN_BACKEND` | API token of `backendEngineer` |
| `OP_TOKEN_DESIGNER` | API token of `leadDesigner` |
| `OP_TOKEN_REVIEWER` | API token of `codeReviewer` |

`OP_PROVISION_TOKEN` is an administrator token used **only** by
`./provision-users.sh` and `docker compose run --rm provision`. The script asks
for it at run time and does not store it.

### Agents

The team is fixed in the image, one OpenProject account and one token each:

| `AGENT` option | Login | Kind | Token |
|---|---|---|---|
| `FRONTEND` | `frontendEngineer` | implementer | `OP_TOKEN_FRONTEND` |
| `BACKEND` | `backendEngineer` | implementer | `OP_TOKEN_BACKEND` |
| `LEAD_DESIGNER` | `leadDesigner` | implementer | `OP_TOKEN_DESIGNER` |
| `REVIEWER` | `codeReviewer` | reviewer | `OP_TOKEN_REVIEWER` |

| Variable | Default | Meaning |
|---|---|---|
| `AGENT_MODEL` | the CLI's default | model for every agent, e.g. `anthropic/claude-sonnet-4-5`; one token, no spaces |
| `AGENT_TIMEOUT_SECONDS` | `3600` | hard limit per agent run |

### Repository

| Variable | Default | Meaning |
|---|---|---|
| `GITHUB_REPO_DIR` | the repository name | clone directory under `WORKSPACE_ROOT` |
| `GITHUB_DEFAULT_BRANCH` | `main` | branch the work branches are based on |
| `GH_TOKEN` | — | GitHub token for the agents' `git`/`gh`; without it, pushes need another credential |

### OpenProject object names

Defaults match a stock OpenProject instance. Override only if your instance
renames them.

| Variable | Default |
|---|---|
| `OP_AGENT_FIELD` | `AGENT` |
| `OP_ENGINE_USER` | `agent-engine` |
| `OP_AGENT_ROLE` | `Agent Worker` |
| `OP_EMAIL_DOMAIN` | `agent-engine.local` |
| `OP_STATUS_NEW` | `New` |
| `OP_STATUS_IN_PROGRESS` | `In Progress` |
| `OP_STATUS_READY_FOR_REVIEW` | `Ready for Review` |
| `OP_STATUS_IN_TESTING` | `In Testing` |
| `OP_STATUS_ON_HOLD` | `On Hold` |
| `OP_STATUS_REJECTED` | `Rejected` |
| `OP_STATUS_CLOSED` | `Closed` |

Status names are matched case- and whitespace-insensitively, so `In Progress`
also matches OpenProject's default `In progress`.

### Paths

Defaults come from the image, so they usually do not belong in `.env`. They
follow the volume mounts in `docker-compose.yml`.

| Variable | Container default | Meaning |
|---|---|---|
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

### Read by compose, not by the engine

Set these in your shell or in `.env` next to `docker-compose.yml`:

| Variable | Default | Meaning |
|---|---|---|
| `OP_AGENT_ENGINE_TAG` | `latest` | image tag to run, e.g. `0.1.0` to pin a release |
| `DASHBOARD_BIND` | `127.0.0.1` | host interface that publishes `DASHBOARD_PORT`; the dashboard has no authentication |

## Agent configuration (`config/`, not env)

| Path | Seeded to | Notes |
|---|---|---|
| `gitconfig` | `~/.gitconfig` | recommended, otherwise `git commit` in a run fails |
| `pi/settings.json` | `~/.pi/agent/settings.json` | default provider/model and pi preferences |
| `pi/auth.json` | `~/.pi/agent/auth.json` | pi provider keys |
| `pi/skills/` | `~/.pi/agent/skills/` | skills available to every run |
| `opencode/opencode.json` | `~/.config/opencode/opencode.json` | only with `INSTALL_OPENCODE=true` |
| `opencode/auth.json` | `~/.local/share/opencode/auth.json` | where opencode keeps credentials |

These files are templates with placeholder values: edit them in place, nothing
has to be copied. Permissions matter: the container runs as its own user (uid
10001), so everything here must be world-readable (`0644`). `config-sync.sh`
sets this up correctly.

`.gitignore` keeps `docker/.env`, `pi/skills/` and `opencode/auth.json` out of
git, but the templates above *are* tracked, so `git status` will show your edits
once you paste a real key into `config/pi/auth.json`. Never commit that: this
directory is yours, not the project's.

### Agent provider credentials

The agent CLIs need a model provider key. Edit the template that is already
there: `config/pi/auth.json` (`AGENT=pi`) or the `apiKey` in
`config/opencode/opencode.json` (`AGENT=opencode`). Nothing is baked into the
image.

## Day-to-day commands

```bash
docker compose logs -f engine                # follow the engine
docker compose run --rm check-config         # validate the environment without polling
docker compose run --rm provision             # create/refresh OpenProject users
docker compose run --rm cleanup              # retention sweep
docker compose restart                       # after editing config/pi/*
docker compose up -d                         # after editing .env, or to upgrade
docker compose pull && docker compose up -d  # upgrade to the published image
docker compose down                          # stop; volumes survive
docker compose down -v                       # stop and delete all state (re-clones repos)
```

`up -d`, not `restart`, after editing `docker/.env`: a changed environment is
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
| [docs/CONFIG.md](docs/CONFIG.md) | environment variables, the agent team, CLI reference |
| [docs/DASHBOARD.md](docs/DASHBOARD.md) | read-only web UI: engine status, agents, sessions, totals |
| [docs/RUNBOOK.md](docs/RUNBOOK.md) | status, pause, run inspection, `On Hold` handling, recovery, security |
| [docs/ACCEPTANCE.md](docs/ACCEPTANCE.md) | fake-agent test and manual acceptance checklist |

Design rationale and the workflow state machine:
[docs/PLAN.md](docs/PLAN.md). Facts verified against real tooling:
[docs/VERIFICATION.md](docs/VERIFICATION.md).
