# Current state

What the repository contains today: it describes the implemented system, not a
roadmap.

- **Version:** see [`docker/version.json`](../docker/version.json), mirrored in
  `pyproject.toml`.
- **License:** MIT (see [`LICENSE`](../LICENSE)); open source.
- **Package:** `op_agent_engine` (Python 3.12), installed into a container image
  and published as `kookster310/op-agent-engine`.
- **Deployment:** Docker Compose only. Nothing is built or installed on the
  host, and nothing is mounted from the host.
- **Configuration:** environment variables only (`docker/.env`). There is no
  workflow YAML and no agent config/auth file to mount.
- **Instance model:** one engine process against one or more OpenProject
  projects. Each project's GitHub repository comes from its `GIT_REPO` project
  custom field; `OP_PROJECT=*` monitors every active project that has it set.
  A file lock (`DATA_DIR/engine.lock`) keeps it single-instance.

## Runtime topology

`docker/docker-compose.yml` starts the long-lived and one-shot services from
the same image:

| Service | Command | Purpose |
|---|---|---|
| `engine` | `run` | poll, claim, execute and verify runs |
| `dashboard` | `dashboard` | read-only web UI on port 8765 |
| `cleanup` | `cleanup` | retention sweep, run from host cron |
| `provision` | `provision --apply --json` | create/reconcile OpenProject users |
| `check-config` | `check-config` | validate the environment and resolve IDs |

Named volumes hold all mutable state; there are no bind mounts:

| Volume | Mount | Contents |
|---|---|---|
| `agent-workspace` | `/home/agent/agents` | `AGENTS.md` standards, one clone per agent, per-run worktrees |
| `engine-data` | `/home/agent/.local/share/op-agent-engine` | SQLite store, `runs/<id>/`, heartbeat, pause flag, engine lock |
| `pi-agent` | `/home/agent/.pi/agent` | pi sessions, logs, model cache, downloaded tools |

`docker/entrypoint.sh` validates the required variables, creates the state
directories, syncs the baked agent standards into `WORKSPACE_ROOT`, writes the
agents' git identity from `GIT_AUTHOR_NAME` / `GIT_AUTHOR_EMAIL`, registers the
git-lfs filters (`git lfs install --skip-repo`, needed for repositories that
track `*.wav`/`*.png`/`*.fbx` assets), and execs the engine. Syncing on every start adds agents introduced by an upgrade to an
existing workspace volume and refreshes the other `AGENTS.md` files; clones and
worktrees are untouched. Provider credentials are environment variables; no
credential file is seeded.

## Control flow

One loop iteration:

1. **Resolve** the workflow against OpenProject (statuses, `AGENT` field,
   users, types, projects, and each project's `GIT_REPO`). Resolution is cached
   for the process.
2. **Poll** (`poller.py`): list candidate work packages by dispatch priority,
   then apply the eligibility rules. Cheap rules run first; blockers and
   children are fetched only when they can change the decision.
3. **Schedule** (`scheduler.py`): move a New ticket with unfinished blockers
   to `Blocked` and post an engine comment, then re-check the slot rules
   against the store and claim work packages with an optimistic-locking
   `PATCH` (status, assignee, `lockVersion`). A claim inserts a `runs` row
   first; a database conflict means another claimant won.
4. **Dispatch** (`dispatcher.py`): prepare the workspace, render the prompt,
   run pi, verify the outcome externally, apply the status transition and
   engine comment, and write the run record.
5. **Recover/retain**: failed runs feed the crash-retry counter; `recovery.py`
   marks stale running rows interrupted on startup; `cleanup` removes run
   directories older than `RUN_RETENTION_DAYS`.

The poller is read-only; only the scheduler writes claims and `Blocked`
transitions; only the dispatcher writes run outcomes and the remaining
OpenProject transitions. Agents never change status.

## Ticket flow

| Observed state | Engine action | Run kind | Verified outcome | Transition |
|---|---|---|---|---|
| `New`, eligible, `AGENT` set | claim, `In Progress` | implement | open PR | `Ready for Review` |
| `New`, unfinished blocker(s) | block, `Blocked` + comment | — | — | `Blocked` |
| `Blocked`, every blocker `Closed` | claim, `In Progress` | implement | open PR | `Ready for Review` |
| implement run reports `BLOCKED` | claim, `In Progress` | implement/fix | — | `On Hold` + comment |
| `Ready for Review`, open PR | claim, `In Testing` | review | PR merged | `Closed` |
| `Ready for Review`, open PR | claim, `In Testing` | review | `REJECTED` + reviewer comment | `Rejected` |
| `Ready for Review`, no PR | — | — | — | `On Hold` + comment |
| `Rejected`, eligible | claim, `In Progress` | fix | open PR | `Ready for Review` |

`On Hold` is terminal until a human moves the ticket. Branch names are
`op-<projectId>-<workPackageId>`; every engine comment starts with `ENGINE:`.

A rejection accepted past `MAX_REJECTIONS` moves the ticket to `On Hold` with a
summary comment instead of `Rejected`. A run that keeps failing increments the
crash counter; past `MAX_CRASH_RETRIES` the ticket moves to `On Hold` with the
log tail in an escalation comment. An existing open PR is authoritative for
implement/fix runs (no retry), and a merged PR is authoritative for reviews.

## Concurrency

Three independent limits apply:

| Limit | Variable | Default | Enforced by |
|---|---|---|---|
| one run per work package | — | always | unique partial index in SQLite |
| runs per agent | `MAX_RUNS_PER_AGENT` | `1` | scheduler (and eligibility) |
| runs in total | `MAX_CONCURRENT_RUNS` | `1` | scheduler and eligibility |

Every run gets its own git worktree under the agent workspace
(`worktrees/<branch>`); the agent's shared clone stays detached, so a killed
run cannot leave uncommitted changes in the clone used by later runs. Workspace
preparation is serialized per agent. A verified, successful run removes its
worktree; a failed run's state is captured into `runs/<runId>/worktree`
(status, diffs, unpushed commits, untracked files) and the worktree itself is
kept until `cleanup` expires it (`KEEP_FAILED_WORKTREES`,
`FAILED_WORKTREE_MAX_MB`). A dirty worktree on a reused branch is moved aside
as `<branch>.kept-<utc>`. `cleanup` also sweeps worktrees left behind by an
engine crash.

## Agents and pi runner

The team is fixed in code and in the image (`agents/*/AGENTS.md`, baked at
`/opt/op-agent-engine/agents` and synced into each workspace volume at start):

| `AGENT` field value | OpenProject login | Kind | Token variable |
|---|---|---|---|
| `FRONTEND` | `frontendEngineer` | implementer | `OP_TOKEN_FRONTEND` |
| `BACKEND` | `backendEngineer` | implementer | `OP_TOKEN_BACKEND` |
| `LEAD_DESIGNER` | `leadDesigner` | implementer | `OP_TOKEN_DESIGNER` |
| `VFX` | `vfxArtist` | implementer | `OP_TOKEN_VFX` |
| `AUDIO` | `audioDesigner` | implementer | `OP_TOKEN_AUDIO` |
| `REVIEWER` | `codeReviewer` | reviewer | `OP_TOKEN_REVIEWER` |

Every agent runs pi:

- `pi --mode json --approve --no-extensions --no-context-files --no-session --model ...`
  (JSONL events are parsed per line; the last assistant message lands in
  `runs/<id>/last_message.txt` for the dashboard, `RESULT:` still comes from
  the final assistant message)

`AGENT_MODEL` is `<provider>/<model>`, and `AGENT_MODEL_<AGENT>` overrides it
for one agent (same provider, one key); the provider selects the credential
variable. The runner forwards the agent's OpenProject CLI credentials plus the
provider/GitHub environment variables of
[`providers.py`](../src/op_agent_engine/providers.py) (`ANTHROPIC_API_KEY`,
`OPENROUTER_API_KEY`, `GH_TOKEN`, ...), and `check-config` fails when the
provider of the configured model has no credentials; see
[CONFIG.md](CONFIG.md#provider-credentials). The local llama.cpp provider uses
`LLAMA_BASE_URL` (required) and the optional `LLAMA_API_KEY`, so the same
environment-only rule covers a self-hosted router; for those runs the runner
also writes a per-run Pi `models.json` (pointed to by `PI_CODING_AGENT_DIR`),
because `pi` in JSON mode does not load the router's model catalog over the network.
The engine's own tokens are not forwarded. The reviewer additionally receives `GH_TOKEN_REVIEWER` as its
`GH_TOKEN`/`GITHUB_TOKEN` when configured, and its workspace clone uses that
token, so it can act as a different GitHub approver under branch protection.

Agent results are read from the last `RESULT:` line of the log
(`BLOCKED ...`, `REJECTED ...`, or a successful completion). The prompt
templates live in `prompts/` and are rendered per run.

## Configuration

Everything is an environment variable; `docker/agent-init.sh` writes the
OpenProject and model values to `docker/.env`, and the GitHub token(s) are
added by hand. The documented template is `docker/.env.example`. The full
reference, including provider variables, object names, limits and compose-only
settings, is [CONFIG.md](CONFIG.md).

Required values: `OP_BASE_URL`, `OP_PROJECT`, `OP_ENGINE_TOKEN`, the six
`OP_TOKEN_*` agent tokens, one model provider key and `GH_TOKEN`. The
`GIT_REPO` project and `AGENT` work-package custom fields are each created once and
assigned to all projects, so each monitored project only needs its own
`GIT_REPO` value.
`OP_PROVISION_TOKEN` is an administrator token used only for provisioning; the
setup script asks for it and does not store it, and `verify-token` uses a
transient `OP_VERIFY_TOKEN`.

The image sets container paths (`WORKSPACE_ROOT`, `DATA_DIR`, `PROMPTS_DIR`)
and the dashboard bind address, so a normal `.env` holds only deployment
values, credentials and deliberate tuning.

## Provisioning and tokens

`docker/agent-init.sh` is the supported setup path:

1. prompts for the OpenProject URL and project(s), the model provider and its
   key (for llama.cpp, the router URL and optional API key);
2. asks for an OpenProject administrator token (used once, never written);
3. runs `provision --apply --json --passwords` in the container, which creates
   or reconciles the engine user, the six agent users and the `Agent Worker`
   role/memberships, and shows each new password in its own block;
4. prompts for one token per account and verifies it with
   `verify-token --login ...` (the engine token is also checked for admin);
5. writes `docker/.env` (mode `0600`) and runs `check-config`, which also
   checks that the provider key (or, for llama.cpp, the router URL) is present;
6. finishes by reminding the operator to add `GH_TOKEN` (and the optional
   dedicated reviewer credentials) to `docker/.env` before starting the stack.

The engine user must be an OpenProject administrator; the agent users only get
the project-scoped `Agent Worker` role. The script does not ask for GitHub
credentials. Re-running the script is safe and is the supported way to fix a
wrong OpenProject token (`--reprompt-tokens`). It pulls the configured image
every time, so the same re-run also provisions agent users introduced by an
upgrade instead of reusing a stale image with the old agent team. The next
engine start syncs the matching agent standards into the existing workspace
volume, so a newly added agent such as VFX or Audio also gets its `AGENTS.md`
without deleting the volume's clones.

## State and artifacts

`DATA_DIR` (volume `engine-data`) contains:

- `engine.db` — `runs` and `crash_counts` tables;
- `runs/<id>/` — `prompt.txt`, `stdout.log`, `stderr.log`, `meta.json`;
- `heartbeat` — last completed poll timestamp (dashboard staleness);
- `PAUSE` — present means no new claims;
- `engine.lock` — single-instance `flock`.

`meta.json` records the run identity, argv, cwd, redacted env, state, outcome,
transition, PR URL and timings. The store keeps run history after the
artifacts are cleaned up; `cleanup` removes old run directories and stale
worktrees.

Crash recovery kills leftover process groups (using PID + start-time identity
to survive PID reuse), marks their runs interrupted, and lets the normal
retry/eligibility path decide what happens next.

## Dashboard

The `dashboard` service reads the store and run metadata without writing. It
serves a read-only page at `/`, a JSON snapshot at `/api/status`, and
`/healthz`. It has no
authentication: compose publishes it on `127.0.0.1` only. See
[DASHBOARD.md](DASHBOARD.md).

## CLI

| Command | Purpose |
|---|---|
| `run` | start the engine loop |
| `once` | one poll, claim and execute pass |
| `dry-run` | one poll and log the plan without writing |
| `check-config` | validate the environment and resolve OpenProject IDs |
| `status` | heartbeat, active runs, recent runs |
| `dashboard` | serve the read-only UI |
| `provision` | create/reconcile users and memberships |
| `verify-token` | check `OP_VERIFY_TOKEN` via `/api/v3/users/me` |
| `cleanup` | retention sweep |

All commands read the same environment; there is no `--config` option.
`--log-level` is accepted before or after the subcommand.

## Repository map

| Path | Contents |
|---|---|
| `src/op_agent_engine/config.py` | `EngineSettings`, the fixed team, workflow construction from env |
| `src/op_agent_engine/engine.py` | loop, heartbeat, pause, signal handling |
| `src/op_agent_engine/poller.py`, `eligibility.py` | candidate fetch and dispatch rules |
| `src/op_agent_engine/scheduler.py` | slot rules, claim protocol |
| `src/op_agent_engine/dispatcher.py` | workspace, prompt, run, verify, transition, record |
| `src/op_agent_engine/runners/` | generic command runner and process supervision |
| `src/op_agent_engine/workspace.py` | clone/fetch, per-run worktree, retention |
| `src/op_agent_engine/outcomes.py` | implementer/reviewer result verification |
| `src/op_agent_engine/store.py` | SQLite run store and engine lock |
| `src/op_agent_engine/recovery.py` | startup crash recovery |
| `src/op_agent_engine/retention.py` | run-directory cleanup |
| `src/op_agent_engine/openproject/` | API client, resolution, activities, work packages |
| `src/op_agent_engine/provision.py` | user/role reconciliation |
| `src/op_agent_engine/dashboard.py`, `dashboard_assets/` | read-only UI |
| `prompts/` | prompt templates |
| `agents/` | the engineering standard per agent workspace |
| `docker/` | image, compose file, entrypoint and setup script |
| `docs/` | configuration, deployment, setup, runbook, dashboard, acceptance, current state |

## Tests and release

- `pytest` covers eligibility tables, scheduler/slot rules, the claim protocol,
  dispatcher outcomes, workspace preparation (including per-run worktrees and retention),
  store/recovery/retention, the CLI, dashboard, provisioning and Docker
  packaging. The end-to-end test drives a fake agent through a full ticket
  cycle with fake OpenProject and GitHub backends.
- `.github/workflows/docker.yml` runs the suite on pull requests and publishes
  the image when image inputs change on `main`. `docker/version.json` must equal
  the package version; `tests/test_docker.py` enforces that, along with the
  workflow only publishing on push events.
- `.github/CODEOWNERS` lists `@Kookster310` and `@KookyAgentReviewer` for
  `.github/`, `docker/` and `pyproject.toml`, so changes that can affect CI
  credentials, the image build or the release version need a named review, and
  the reviewer account can still merge them on its own. `SECURITY.md` is the
  private reporting path; `tests/test_open_source.py` keeps both in place.
- Releases bump both version files, tag `vX.Y.Z`, and push; the workflow refuses
  a tag that disagrees with `docker/version.json`.

## Known limits

- Multiple OpenProject projects per deployment, but one GitHub repository per
  project. The `GIT_REPO` project attribute and `AGENT` work-package custom
  field are created once and assigned to all projects; a newly added project
  only needs its `GIT_REPO` value set.
- The team is fixed; adding a member is a code change, not configuration.
- The dashboard has no authentication and no write actions.
- Every run uses a per-run worktree; the default per-agent limit is 1.
- A single engine instance is assumed; the lock prevents a second one.
