# openproject-agent-engine

Polls OpenProject work packages and dispatches CLI coding agents (currently
supports pi and OpenCode) to work on them. OpenProject is the system of record:
the engine claims work, prepares a per-agent clone, renders a prompt, runs the
agent, verifies the outcome against GitHub and OpenProject, and performs
**every** status transition itself. Agents never change status.

## How a ticket flows

| Observed state | Engine action | Run | Verified outcome | Transition |
|---|---|---|---|---|
| `New`, eligible, `AGENT` set | claim, `In Progress` | implement | open PR / `BLOCKED` | `Ready for Review` / `On Hold` |
| `Ready for Review`, PR exists | claim, `In Testing` | review | PR merged / `REJECTED` | `Closed` / `Rejected` |
| `Ready for Review`, no PR | — | — | — | `On Hold` + comment |
| `Rejected`, eligible | claim, `In Progress` | fix | as implement | as implement |

`On Hold` is terminal: the engine ignores the ticket until a human moves it.
Branches are `op-<project>-<wp>` (one per ticket), at most
`MAX_RUNS_PER_AGENT` runs per agent (default 1), and every engine comment
starts with `ENGINE:`.

## Requirements

- Docker
- OpenProject 17 (API v3) with a project, plus one-time administrator access.
- A GitHub repository, and a token that can push to it and merge pull
  requests.

## Quick start

1. Complete the one-time instance setup — statuses, the `AGENT` custom field
   and the Agent Worker role — in
   [docs/OPENPROJECT_SETUP.md](docs/OPENPROJECT_SETUP.md).

2. In `docker/`, run the interactive setup:

   ```bash
   cd docker
   ./provision-users.sh
   ```

   It asks for the OpenProject URL, project and repository, asks for an
   administrator token (used once, never stored), creates the engine user and
   one account per agent, prints each generated password at that account's
   token prompt, verifies every token and writes `docker/.env` (mode `0600`).
   It finishes with `check-config`. Re-running is safe; `--reprompt-tokens`
   asks for all tokens again.

3. Add the model provider key to `docker/.env`, for example:

   ```bash
   ANTHROPIC_API_KEY=sk-ant-...
   ```

   The agent CLI reads it from the environment; there is no `auth.json` or
   `settings.json` to mount. The full list of accepted provider variables is in
   [docs/CONFIG.md](docs/CONFIG.md#provider-credentials).

4. Start the stack:

   ```bash
   docker compose up -d
   docker compose logs -f engine
   ```

The dashboard is served at `http://127.0.0.1:8765`
(see [docs/DASHBOARD.md](docs/DASHBOARD.md)).

## Required environment variables

`provision-users.sh` writes the first nine; you add the provider key.

| Variable | Meaning |
|---|---|
| `OP_BASE_URL` | your OpenProject URL |
| `OP_PROJECT` | identifier of the project the engine works on |
| `GITHUB_REPO` | repository as `owner/name` |
| `OP_ENGINE_TOKEN` | API token of the runtime user (administrator) |
| `AGENT` | agent CLI to run: `pi` or `opencode` |
| `OP_TOKEN_FRONTEND` | token of `frontendEngineer` |
| `OP_TOKEN_BACKEND` | token of `backendEngineer` |
| `OP_TOKEN_DESIGNER` | token of `leadDesigner` |
| `OP_TOKEN_REVIEWER` | token of `codeReviewer` |
| `ANTHROPIC_API_KEY` etc. | one model provider key for the agent CLI |

The team is fixed in the image, one OpenProject account and one token each:

| `AGENT` option | Login | Kind |
|---|---|---|
| `FRONTEND` | `frontendEngineer` | implementer |
| `BACKEND` | `backendEngineer` | implementer |
| `LEAD_DESIGNER` | `leadDesigner` | implementer |
| `REVIEWER` | `codeReviewer` | reviewer |

Two optional variables are commonly changed:

| Variable | Meaning |
|---|---|
| `AGENT_MODEL` | model for every agent, e.g. `anthropic/claude-sonnet-4-5`; one token, no spaces |
| `GH_TOKEN` | GitHub token so the agents can push and open pull requests |

Everything else — concurrency, timings, status names, paths, git identity —
has a working default. See [docs/CONFIG.md](docs/CONFIG.md) and the commented
[`docker/.env.example`](docker/.env.example).

## Documentation

| Doc | Contents |
|---|---|
| [docs/DOCKER.md](docs/DOCKER.md) | image internals, volumes, releases, troubleshooting |
| [docs/OPENPROJECT_SETUP.md](docs/OPENPROJECT_SETUP.md) | statuses, `AGENT` field, Agent Worker role, admin engine user, tokens |
| [docs/CONFIG.md](docs/CONFIG.md) | every environment variable, provider keys, agent team, CLI reference |
| [docs/DASHBOARD.md](docs/DASHBOARD.md) | read-only web UI |
| [docs/RUNBOOK.md](docs/RUNBOOK.md) | status, pause, run inspection, `On Hold` handling, recovery |
| [docs/ACCEPTANCE.md](docs/ACCEPTANCE.md) | fake-agent test and manual acceptance checklist |
| [docs/CURRENT_STATE.md](docs/CURRENT_STATE.md) | implemented architecture, control flow, state and limits |
