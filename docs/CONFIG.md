# Configuration

There is no configuration file. Everything deployment-specific is an
environment variable, read from `docker/.env` (which compose passes to the
containers) or from your shell. The agent team, the runners and the
OpenProject object names have defaults baked into the code, so a deployment
only supplies its URL, project, repository and tokens.

`docker/provision-users.sh` writes the required variables for you; the
commented reference is [`docker/.env.example`](../docker/.env.example). The
full variable tables are in the [root README](../README.md#environment-variables).

## Required

| Variable | Meaning |
|---|---|
| `OP_BASE_URL` | OpenProject URL |
| `OP_PROJECT` | project identifier the engine works on |
| `GITHUB_REPO` | repository as `owner/name` |
| `OP_ENGINE_TOKEN` | API token of the administrator runtime user |
| `AGENT` | `pi` or `opencode` |
| `OP_TOKEN_FRONTEND` | token of `frontendEngineer` |
| `OP_TOKEN_BACKEND` | token of `backendEngineer` |
| `OP_TOKEN_DESIGNER` | token of `leadDesigner` |
| `OP_TOKEN_REVIEWER` | token of `codeReviewer` |

`OP_PROVISION_TOKEN` is an administrator token used only by
`provision-users.sh` and `docker compose run --rm provision`.

## What is baked in

The team is fixed, one OpenProject account and one token each:

| `AGENT` option | Login | Kind | Token |
|---|---|---|---|
| `FRONTEND` | `frontendEngineer` | implementer | `OP_TOKEN_FRONTEND` |
| `BACKEND` | `backendEngineer` | implementer | `OP_TOKEN_BACKEND` |
| `LEAD_DESIGNER` | `leadDesigner` | implementer | `OP_TOKEN_DESIGNER` |
| `REVIEWER` | `codeReviewer` | reviewer | `OP_TOKEN_REVIEWER` |

Exactly one agent is the reviewer. The implementers and the reviewer run the
same CLI selected by `AGENT`:

- `pi`: `pi -p --approve --no-extensions --no-context-files --no-session
  --model {model} @{prompt_file} ...`
- `opencode`: `opencode run --auto --model {model} --file {prompt_file} ...`

`AGENT_MODEL` sets the model for every agent; when it is unset the `{model}`
placeholder and the flag in front of it are dropped, so the CLI's own default
is used. The value must be one token (no spaces), in the CLI's own format —
`provider/id` works for both.

Status names come from `OP_STATUS_*` and are matched case- and
whitespace-insensitively, so `In Progress` also matches OpenProject's default
`In progress`.

## Runtime variables

`POLL_INTERVAL_SECONDS`, `MAX_CONCURRENT_RUNS`, `MAX_REJECTIONS`,
`MAX_CRASH_RETRIES`, `RUN_TIMEOUT_SECONDS`, `STOP_GRACE_SECONDS`,
`RUN_RETENTION_DAYS`, `ENGINE_PAUSED`, `LOG_LEVEL`, `GH_TOKEN`,
`WORKSPACE_ROOT`, `DATA_DIR`, `PROMPTS_DIR`, `DASHBOARD_HOST` and
`DASHBOARD_PORT` work as before; see the README for defaults. The image sets
the paths and the dashboard bind address, so `.env` normally only holds the
deployment values and the limits.

## Dispatching rules

- A work package is eligible when it is in a dispatch status, has an eligible
  type (`Task`, `Bug`, `Feature`), sets the `AGENT` field to a team option and
  is not assigned to a human.
- Work packages with children are skipped. Dispatch order is
  `Ready for Review`, `Rejected`, `New` (finish work before starting new work).
- A ticket routed to an out-of-scope agent is skipped with a warning; every
  agent is scoped to the one configured project.

## CLI reference

Run inside the container (`docker compose run --rm <task>` or
`docker compose exec engine op-agent-engine ...`):

| Command | Purpose |
|---|---|
| `run` | start the polling engine loop |
| `once` | a single poll and claim |
| `dry-run` | a single poll that logs the plan and writes nothing |
| `check-config` | validate the environment and resolve OpenProject IDs (`--json`, `--offline`) |
| `status` | heartbeat age, active runs, recent runs (`--json`) |
| `dashboard` | serve the read-only web UI |
| `provision` | create/reconcile users and memberships (`--apply`, `--json`, `--passwords`) |
| `verify-token` | check that `OP_VERIFY_TOKEN` authenticates as `--login` (used by `provision-users.sh`) |
| `cleanup` | remove run directories older than `RUN_RETENTION_DAYS` |

`--log-level LEVEL` is accepted before or after the subcommand. `provision`
prints generated passwords only with `--passwords`, and `verify-token` never
prints the token.
