# `docker/` — the whole deployment

Everything Docker-related lives here, and this directory **is** the deployment:
copy it to `/opt/op-agent-engine` (or anywhere else), put your configuration in
`config/`, and run compose. Nothing is built; the image is pulled from Docker
Hub (`kookster310/op-agent-engine`).

| Path | What it is | Committed? |
|---|---|---|
| `docker-compose.yml` | `engine`, `dashboard` and the one-shot `provision` / `cleanup` / `check-config` tasks | yes |
| `config/` | **your** configuration and credentials, mounted read-only at `/config` | no — skeleton only |
| `config.example/` | non-personal examples of the optional agent files | yes |
| `provision-users.sh` | creates the OpenProject accounts and collects one token per account | yes |
| `config-sync.sh` | stages `~/.pi/agent`, `~/.gitconfig` and `~/.config/opencode` into `config/` (checkout only) | yes |
| `Dockerfile`, `entrypoint.sh` | how the image is built and started — built by CI, not by you | yes |
| `version.json` | the released tag; must equal `project.version` in `pyproject.toml` | yes |

Full documentation: [../docs/DOCKER.md](../docs/DOCKER.md).

## Quick start

```bash
cd /opt/op-agent-engine          # wherever you put this directory
$EDITOR config/engine.env        # OP_BASE_URL + OP_PROVISION_TOKEN (see below)
$EDITOR config/workflow.yaml     # projects, repositories, agents
./provision-users.sh             # create the accounts, mint one token each
docker compose run --rm --no-deps check-config
docker compose pull && docker compose up -d
docker compose logs -f engine
```

`config/workflow.yaml` is the same document a systemd install uses
([../docs/CONFIG.md](../docs/CONFIG.md)); `workflow.example.yaml` in the
repository root is the starting point. If you have the repository checked out,
`./config-sync.sh` fills in `config/` from your own `~` and seeds
`workflow.yaml` and `engine.env` for you.

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

Copy the shapes from `config.example/`, then edit. Permissions matter: the
container runs as its own user (uid 10001), so everything here must be
world-readable (`0644`) — except `engine.env`, which the host side reads and
which may stay `0600`. `config-sync.sh` sets this up correctly.

### Agent provider credentials

The agent CLIs need a model provider key. Two ways, both without baking
anything into the image:

1. `config/pi/auth.json` (and `config/opencode/auth.json` for opencode) — see
   `config.example/pi/auth.json`.
2. An environment variable in the runner's `env:` block in
   `config/workflow.yaml`, e.g. `env: { OPENROUTER_API_KEY: "${OPENROUTER_API_KEY}" }`
   with the value in `config/engine.env`. The engine only forwards a fixed set
   of variables (`PATH`, `HOME`, `USER`, `LOGNAME`, `TZ`, `LANG`, `LC_ALL`) to
   the agent, so a key in `engine.env` alone is not enough.

## Image build arguments (CI only)

These are repository variables for `.github/workflows/docker.yml`, not
container environment variables. Users pull; they do not build.

| Variable | Default | Meaning |
|---|---|---|
| `PI_VERSION` | `1.4.0` | pi release baked into the image |
| `GH_VERSION` | `2.80.0` | GitHub CLI version |
| `OP_CLI_REF` | `259f293a` | OpenProject CLI commit (`op`) built from source |
| `INSTALL_OPENCODE` | `false` | `true` also installs opencode |

## Everyday commands

```bash
docker compose ps                            # what is running
docker compose logs -f engine                # follow the engine
docker compose run --rm check-config         # validate config, no polling
docker compose run --rm cleanup              # retention sweep (replaces the timer)
docker compose run --rm provision --apply    # re-provision users, no interview
docker compose restart                       # after editing workflow.yaml, pi/, opencode/, gitconfig
docker compose up -d                         # after editing engine.env (recreates)
docker compose pull && docker compose up -d  # upgrade
docker compose down                          # stop; volumes survive
docker compose down -v                       # stop and delete all state (re-clones repos)
```

Nightly cleanup instead of the systemd timer:

```cron
0 4 * * * cd /opt/op-agent-engine && docker compose run --rm cleanup
```
