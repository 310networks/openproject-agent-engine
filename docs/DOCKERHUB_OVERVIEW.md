# OpenProject Agent Engine

Polls OpenProject work packages and dispatches the [pi](https://github.com/badlogic/pi-mono)
coding agent to work on them.

OpenProject is the system of record. The engine claims work, prepares a
per-agent clone, renders a prompt, runs the agent, verifies the outcome against
GitHub and OpenProject, and performs **every** status transition itself. Agents
never change status: a human (or the engine) still owns the board.

- Source, docs and issues: <https://github.com/310networks/openproject-agent-engine>
- License: MIT
- Security reports: see [SECURITY.md](https://github.com/310networks/openproject-agent-engine/blob/main/SECURITY.md) — do not open a public issue

## Image

```
docker pull kookster310/op-agent-engine
```

| Tag | Meaning |
|---|---|
| `latest` | newest build from `main` |
| `<version>`, e.g. `0.2.7` | a release, pinned and reproducible |
| `sha-<short>` | one commit, useful for bisecting |

`linux/amd64` only. The engine itself is architecture independent, but the
`op`, `node` and `gh` builds baked into the image are not cross-built, so no
`arm64` image is published.

The image is a runtime, not a deployment by itself: it ships the engine, the
prompts and the agent definitions, and expects a `docker/docker-compose.yml`
next to a `.env` you provide. That compose file lives in the repository, not in
the image.

## Quick start

Requires Docker, OpenProject 17 (API v3) and a GitHub repository the agents can
push to and merge in.

1. **One-time OpenProject setup** — statuses, the `GIT_REPO` and `AGENT`
   project/work-package fields, the Agent Worker role:
   [docs/OPENPROJECT_SETUP.md](https://github.com/310networks/openproject-agent-engine/blob/main/docs/OPENPROJECT_SETUP.md)

2. **Get the deployment directory** (it is the whole deployment — copy
   anywhere, for example `/opt/op-agent-engine`):

   ```bash
   git clone https://github.com/310networks/openproject-agent-engine
   cd openproject-agent-engine/docker
   ```

3. **Run the interactive setup.** It asks for the OpenProject URL and token,
   the six agent tokens, the model and the provider key, and writes `.env`.
   Re-running is safe. It pulls the current image first, so it also creates
   the agent users added by an upgrade:

   ```bash
   ./agent-init.sh
   ```

4. **Add the GitHub token(s)** to `.env` — the setup script does not ask for
   them:

   ```bash
   GH_TOKEN=github_pat_...
   # optional, see "Branch protection" below
   GH_TOKEN_REVIEWER=github_pat_...
   GIT_REVIEWER_NAME=...
   GIT_REVIEWER_EMAIL=...
   ```

5. **Start it** and watch the log:

   ```bash
   docker compose up -d
   docker compose logs -f engine
   ```

6. After adding a project, or changing its `GIT_REPO` / `AGENT` values in
   OpenProject: `docker compose restart engine`. After editing `.env`:
   `docker compose up -d engine` — `restart` keeps the old environment.

Pin a release in `.env` instead of tracking `latest`:

```
OP_AGENT_ENGINE_TAG=0.2.7
```

## Required environment variables

| Variable | Meaning |
|---|---|
| `OP_BASE_URL` | your OpenProject URL |
| `OP_PROJECT` | project identifier: one, a comma list, or `*` for every project that has `GIT_REPO` |
| `OP_ENGINE_TOKEN` | API token of the runtime user (administrator) |
| `AGENT_MODEL` | model for every agent as `<provider>/<model>`; the provider selects the credential variable |
| `AGENT_MODEL_<AGENT>` | optional per-agent override: `AGENT_MODEL_FRONTEND`, `..._BACKEND`, `..._DESIGNER`, `..._VFX`, `..._AUDIO`, `..._REVIEWER` |
| `OP_TOKEN_FRONTEND` / `_BACKEND` / `_DESIGNER` / `_VFX` / `_AUDIO` / `_REVIEWER` | one token per agent account |
| `OPENROUTER_API_KEY` (or another provider key) | model provider credential |
| `LLAMA_BASE_URL` | local llama.cpp router URL, instead of a hosted provider |
| `GH_TOKEN` | GitHub token used to clone, push and look up pull requests |

Everything else — concurrency, timings, status names, paths, git identity — has
a working default. The full list, including limits and the llama.cpp provider,
is in
[docs/CONFIG.md](https://github.com/310networks/openproject-agent-engine/blob/main/docs/CONFIG.md).

Each monitored project needs a project attribute named `GIT_REPO` holding its
GitHub repository (`owner/name` or a github.com URL).

## The agent team

Fixed in the image, one OpenProject account and one token each:

| `AGENT` field value | Login | Kind |
|---|---|---|
| `FRONTEND` | `frontendEngineer` | implementer |
| `BACKEND` | `backendEngineer` | implementer |
| `LEAD_DESIGNER` | `leadDesigner` | implementer |
| `VFX` | `vfxArtist` | implementer |
| `AUDIO` | `audioDesigner` | implementer |
| `REVIEWER` | `codeReviewer` | reviewer |

## How a ticket flows

| Observed state | Engine action | Run | Verified outcome | Transition |
|---|---|---|---|---|
| `New`, eligible, `AGENT` set | claim, `In Progress` | implement | open PR / `BLOCKED` | `Ready for Review` / `On Hold` |
| `New`, unfinished blocker(s) | block, `Blocked` + comment | — | — | `Blocked` |
| `Blocked`, every blocker `Closed` | claim, `In Progress` | implement | open PR / `BLOCKED` | `Ready for Review` / `On Hold` |
| `Ready for Review`, PR exists | claim, `In Testing` | review | PR merged / `REJECTED` | `Closed` / `Rejected` |
| `Ready for Review`, no PR | — | — | — | `On Hold` + comment |
| `Rejected`, eligible | claim, `In Progress` | fix | as implement | as implement |

`On Hold` is terminal until a human moves the ticket. Branches are
`op-<project>-<wp>`, one per ticket. Every run uses its own worktree, and at
most `MAX_RUNS_PER_AGENT` runs per agent (default 1) share the agent's clone.
Every engine comment starts with `ENGINE:`.

## Branch protection

If your repository requires an approving review from an account **other than
the pull request's author**, give `codeReviewer` its own GitHub account and set
`GH_TOKEN_REVIEWER`. The reviewer then switches `gh`/git to that account and can
submit an approving review before merging. With a single `GH_TOKEN`, every agent
is the same account, cannot approve its own PR, and merges stall.

If reviewers are also required for specific paths, list both the human and the
reviewer account in `.github/CODEOWNERS` — an owner that is also the PR author
can never satisfy the requirement.

## What is in the container

| Baked in | Configured at runtime |
|---|---|
| Python 3.12, `git`, `gh`, `ripgrep`, `fd`, `op` | deployment values, limits, API tokens, provider keys (environment only) |
| Node 22 and pi | model, git identity |
| `agents/*/AGENTS.md` (seeds the workspace volume) | — |
| `prompts/` | — |

Runs as `uid 10001` (`agent`). Nothing is mounted from the host: settings and
credentials are environment variables, and all state lives in volumes.
Provider credentials are never written to credential files by the image.

| Volume | Mount point | Holds |
|---|---|---|
| `agent-workspace` | `/home/agent/agents` | seeded `agents/` tree plus one clone per agent workspace |
| `engine-data` | `/home/agent/.local/share/op-agent-engine` | SQLite store, `runs/<id>/`, heartbeat, pause flag, lock |
| `pi-agent` | `/home/agent/.pi/agent` | pi sessions, logs, model cache, installed tools |

## Dashboard

The compose stack also starts a read-only dashboard on
`http://127.0.0.1:8765` (loopback only). **It has no authentication** — keep it
on loopback or put it behind your own reverse proxy. Details:
[docs/DASHBOARD.md](https://github.com/310networks/openproject-agent-engine/blob/main/docs/DASHBOARD.md).

## Documentation

| Doc | Contents |
|---|---|
| [docs/DOCKER.md](https://github.com/310networks/openproject-agent-engine/blob/main/docs/DOCKER.md) | image internals, volumes, releases, troubleshooting |
| [docs/OPENPROJECT_SETUP.md](https://github.com/310networks/openproject-agent-engine/blob/main/docs/OPENPROJECT_SETUP.md) | statuses, `GIT_REPO`, `AGENT`, Agent Worker role, admin engine user |
| [docs/CONFIG.md](https://github.com/310networks/openproject-agent-engine/blob/main/docs/CONFIG.md) | every environment variable, provider keys, agent team, CLI |
| [docs/RUNBOOK.md](https://github.com/310networks/openproject-agent-engine/blob/main/docs/RUNBOOK.md) | status, pause, run inspection, `On Hold` handling, recovery |
| [docs/ACCEPTANCE.md](https://github.com/310networks/openproject-agent-engine/blob/main/docs/ACCEPTANCE.md) | fake-agent test and manual acceptance checklist |

## License

MIT. See [LICENSE](https://github.com/310networks/openproject-agent-engine/blob/main/LICENSE).
