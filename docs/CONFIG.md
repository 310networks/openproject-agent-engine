# Configuration

There is no configuration file. Everything deployment-specific is an
environment variable, read from `docker/.env` (which compose passes to the
containers) or from your shell. The agent team, the pi runner and the
OpenProject object names have defaults baked into the code, so a deployment
only supplies its URL, project(s) and tokens.

`docker/agent-init.sh` writes the required variables for you; the
commented reference is [`docker/.env.example`](../docker/.env.example). The
[root README](../README.md) covers getting started; this page is the full
reference.

## Required

| Variable | Meaning |
|---|---|
| `OP_BASE_URL` | OpenProject URL |
| `OP_PROJECT` | project identifier(s): one, a comma-separated list, or `*` for every active project with `GIT_REPO` set |
| `OP_ENGINE_TOKEN` | API token of the administrator runtime user |
| `OP_TOKEN_FRONTEND` | token of `frontendEngineer` |
| `OP_TOKEN_BACKEND` | token of `backendEngineer` |
| `OP_TOKEN_DESIGNER` | token of `leadDesigner` |
| `OP_TOKEN_REVIEWER` | token of `codeReviewer` |
| `GH_TOKEN` | GitHub token used to clone, push and look up pull requests |

`OP_PROVISION_TOKEN` is an administrator token used only by
`agent-init.sh` and `docker compose run --rm provision`.

## GitHub identity

Every agent normally uses the shared `GH_TOKEN`. Some protected repositories
require the approving or merging account to differ from the account that
authored the pull request. The reviewer can use a second GitHub account:

| Variable | Default | Meaning |
|---|---|---|
| `GH_TOKEN_REVIEWER` | unset (shared `GH_TOKEN`) | GitHub token for `codeReviewer`; also used for its clone/fetch remote and as `GITHUB_TOKEN` |
| `GIT_REVIEWER_NAME` | `codeReviewer` when the token is set | git author/committer name for reviewer runs |
| `GIT_REVIEWER_EMAIL` | `codeReviewer@OP_EMAIL_DOMAIN` when the token is set | git author/committer email for reviewer runs |

When `GH_TOKEN_REVIEWER` is set, the reviewer process receives it as
`GH_TOKEN`/`GITHUB_TOKEN` and its workspace is cloned/fetched with that
account. The engine itself (polling and PR lookups) keeps using `GH_TOKEN`. The
reviewer standard then allows a formal approving review before the merge when
the account differs from the PR author. If the two tokens belong to the same
account, branch protection that requires a different approver still blocks the
merge; `agent-init.sh` warns in that case.

`GH_TOKEN_REVIEWER`, `GIT_REVIEWER_NAME` and `GIT_REVIEWER_EMAIL` are optional;
leaving them unset preserves the single shared GitHub identity.

## What is baked in

The team is fixed, one OpenProject account and one token each:

| `AGENT` field value | Login | Kind | Token |
|---|---|---|---|
| `FRONTEND` | `frontendEngineer` | implementer | `OP_TOKEN_FRONTEND` |
| `BACKEND` | `backendEngineer` | implementer | `OP_TOKEN_BACKEND` |
| `LEAD_DESIGNER` | `leadDesigner` | implementer | `OP_TOKEN_DESIGNER` |
| `REVIEWER` | `codeReviewer` | reviewer | `OP_TOKEN_REVIEWER` |

Exactly one agent is the reviewer. The implementers and the reviewer all run
`pi`:

- `pi -p --approve --no-extensions --no-context-files --no-session
  --model {model} @{prompt_file} ...`

`AGENT_MODEL` sets the model for every agent; when it is unset the `{model}`
placeholder and the flag in front of it are dropped, so pi's own default is
used. The value must be one token (no spaces), in pi's own format —
`provider/id` works.

The provider is the part before the first `/`, and it selects the credential
variable as well: `openrouter/deepseek/deepseek-v4.1-flash-1017` is the
`openrouter` provider serving the model `deepseek/deepseek-v4.1-flash-1017`,
authenticated by `OPENROUTER_API_KEY`. `agent-init.sh` asks for the provider
and the model separately for exactly that reason; writing `.env` by hand
means joining them yourself.

`AGENT_MODEL_<AGENT>` overrides the shared model for one agent
(`AGENT_MODEL_FRONTEND`, `AGENT_MODEL_BACKEND`, `AGENT_MODEL_DESIGNER`,
`AGENT_MODEL_REVIEWER`); without one, the agent uses `AGENT_MODEL`. A
deployment can therefore name a model per agent and leave `AGENT_MODEL` unset
entirely — an agent with neither uses pi's own default. The deployment
still has **one provider**, because it stores one provider key: a model
override may name a different model but not a different provider.
`check-config` rejects a mix (`one model provider per deployment`), and
`agent-init.sh` asks for the provider once and prefixes every model with it.

Status names come from `OP_STATUS_*` and are matched case- and
whitespace-insensitively, so `In Progress` also matches OpenProject's default
`In progress`.

## Repositories

Each project supplies its GitHub repository through the **project attribute**
named `GIT_REPO`. The value may be `owner/name` or a github.com URL; the
engine stores the short form. At startup the engine looks up the attribute's
`customField<N>` id in `GET /api/v3/projects/schema` and reads its value from
`GET /api/v3/projects/{id}`, so `OP_PROJECT=*` monitors every active project
that has it set. Names are matched case- and separator-insensitively, so
`GIT_REPO`, `GIT REPO`, and `git-repo` are equivalent.

Create it once in OpenProject under **Administration → Projects → Project
attributes**, choose a text format, and make it available for **all
projects** (the API cannot create it). One attribute is assigned to every
project, so there is no per-project enablement — the same manual step as the
`AGENT` work-package field; see
[OPENPROJECT_SETUP.md](OPENPROJECT_SETUP.md#3-project-attribute-git_repo).

`GITHUB_REPO_DIR` is a single-project clone-directory override. For multiple
projects the directory is derived from the repository name and made unique.

The default branch is discovered when the workspace is prepared (`git
symbolic-ref` / `git ls-remote --symref`). Set `GITHUB_DEFAULT_BRANCH` only to
override that discovery.

### Applying project changes

The `GIT_REPO` project attribute and the project list are resolved when the engine starts. After
adding a project or changing a project's `GIT_REPO` / `AGENT` settings in
OpenProject, restart the engine process:

```bash
docker compose restart engine
```

If you changed `docker/.env` instead (for example `OP_PROJECT` or
`OP_AGENT_FIELD`), recreate the container so compose picks up the new
environment:

```bash
docker compose up -d engine
```

## Object names

| Variable | Default | Meaning |
|---|---|---|
| `GITHUB_REPO_DIR` | the repository name | single-project clone directory under `WORKSPACE_ROOT` |
| `GITHUB_DEFAULT_BRANCH` | auto-discovered | override the branch work branches are based on |
| `OP_AGENT_FIELD` | `AGENT` | custom field that routes a work package to an agent |
| `OP_ENGINE_USER` | `agent-engine` | OpenProject login of the runtime user |
| `OP_AGENT_ROLE` | `Agent Worker` | role granted to the agent users |
| `OP_EMAIL_DOMAIN` | `agent-engine.local` | email domain for generated accounts |
| `OP_STATUS_NEW` | `New` | |
| `OP_STATUS_IN_PROGRESS` | `In Progress` | |
| `OP_STATUS_READY_FOR_REVIEW` | `Ready for Review` | |
| `OP_STATUS_IN_TESTING` | `In Testing` | |
| `OP_STATUS_ON_HOLD` | `On Hold` | |
| `OP_STATUS_REJECTED` | `Rejected` | |
| `OP_STATUS_CLOSED` | `Closed` | |

## Provider credentials

pi reads its model provider key from the environment; no `auth.json` or
`settings.json` is mounted. `agent-init.sh` asks for the key belonging to the
provider in `AGENT_MODEL`, asks for a GitHub token, and writes both to
`docker/.env`, so neither is a manual step. If you write `.env` yourself, set
the variable for your provider (the table below is
[`src/op_agent_engine/providers.py`](../src/op_agent_engine/providers.py)):

`ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN`, `ANTHROPIC_BASE_URL`,
`OPENAI_API_KEY`, `OPENAI_BASE_URL`, `AZURE_OPENAI_API_KEY`,
`AZURE_OPENAI_ENDPOINT`, `AZURE_OPENAI_API_VERSION`, `GEMINI_API_KEY`,
`GOOGLE_API_KEY`, `GOOGLE_CLOUD_PROJECT`, `GOOGLE_CLOUD_LOCATION`,
`GOOGLE_APPLICATION_CREDENTIALS`, `OPENROUTER_API_KEY`, `XAI_API_KEY`,
`DEEPSEEK_API_KEY`, `GROQ_API_KEY`, `CEREBRAS_API_KEY`, `MISTRAL_API_KEY`,
`TOGETHER_API_KEY`, `FIREWORKS_API_KEY`, `HF_TOKEN`, `NVIDIA_API_KEY`,
`OPENCODE_API_KEY`, `MOONSHOT_API_KEY`, `KIMI_API_KEY`, `ZAI_API_KEY`,
`MINIMAX_API_KEY`, `COPILOT_GITHUB_TOKEN`, `LLAMA_BASE_URL`,
`LLAMA_API_KEY`, `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`,
`AWS_SESSION_TOKEN`, `AWS_REGION`, `AWS_PROFILE`,
`AWS_BEARER_TOKEN_BEDROCK`, `GH_TOKEN` and `GITHUB_TOKEN`.

The engine forwards exactly these variables to the agent process and keeps the
rest of its own environment (the OpenProject tokens) out of it.

### llama.cpp

Pi has a built-in provider for a local [llama.cpp](https://github.com/ggml-org/llama.cpp)
router. Set the provider in the model reference and point the engine at the
router:

```dotenv
AGENT_MODEL=llama.cpp/qwen2.5-coder-7b
LLAMA_BASE_URL=http://192.168.1.10:8080
# Optional: only when llama-server was started with --api-key.
# LLAMA_API_KEY=secret
```

`LLAMA_BASE_URL` is required: it selects the server, so an API key alone is not
enough. `LLAMA_API_KEY` is optional because Pi supplies the literal key
`local` to an unauthenticated server. The model id is the router's model name
(one of the ids in the router's `/models` response); start `llama-server` in
router mode and load or preset the model. `agent-init.sh` asks for the URL as
plain text and offers the optional API key.

The engine runs Pi in print mode (`pi -p`), which does not discover a
llama.cpp router's model list over the network. For a llama.cpp run it writes a
per-run `models.json` into a private agent directory and points
`PI_CODING_AGENT_DIR` at it, so the configured `AGENT_MODEL`/per-agent model is
available without an interactive login or a shared model cache. The file
contains the model id, the OpenAI-compatible `/v1` URL derived from
`LLAMA_BASE_URL`, and an environment reference (`$LLAMA_API_KEY`) rather than
the key value; an unauthenticated server gets the literal key `local`.

The URL is resolved from inside the engine container, not from the host.
`127.0.0.1` therefore means the container itself; use an address the container
can reach, such as the host's LAN address or a llama.cpp server on the same
Docker network.

## Runtime variables

| Variable | Default | Meaning |
|---|---|---|
| `POLL_INTERVAL_SECONDS` | `60` | how often OpenProject is polled |
| `MAX_CONCURRENT_RUNS` | `1` | agent runs in parallel across all agents |
| `MAX_RUNS_PER_AGENT` | `1` | concurrent runs per agent; the global limit still applies |
| `MAX_REJECTIONS` | `3` | rejections before an agent stops retrying a work package |
| `MAX_CRASH_RETRIES` | `2` | retries after a crashed run |
| `RUN_TIMEOUT_SECONDS` | `3600` | hard limit per run |
| `AGENT_TIMEOUT_SECONDS` | `3600` | hard limit per agent run (wins over the above) |
| `STOP_GRACE_SECONDS` | `60` | shutdown budget; compose allows 90 s |
| `RUN_RETENTION_DAYS` | `30` | `cleanup` removes older run directories |
| `ENGINE_PAUSED` | `false` | `true` stops new runs without stopping the container |
| `LOG_LEVEL` | `INFO` | `DEBUG` for verbose logs |
| `WORKSPACE_ROOT` | image | agent workspaces and git clones |
| `DATA_DIR` | image | SQLite store, run directories, heartbeat, lock |
| `PROMPTS_DIR` | image | prompt templates |
| `DASHBOARD_HOST` | image | bind address inside the container |
| `DASHBOARD_PORT` | `8765` | port the compose mapping publishes |
| `GIT_AUTHOR_NAME` | `OP_ENGINE_USER` | identity for the commits the agents make |
| `GIT_AUTHOR_EMAIL` | `OP_ENGINE_USER@OP_EMAIL_DOMAIN` | author email for those commits |

The image sets the container paths and the dashboard bind address, so `.env`
normally only holds the deployment values, the provider key and the limits.

## Compose-only

Set these in `.env` or your shell; the engine never reads them.

| Variable | Default | Meaning |
|---|---|---|
| `OP_AGENT_ENGINE_TAG` | `latest` | image tag to run, e.g. `0.2.4` to pin a release |
| `DASHBOARD_BIND` | `127.0.0.1` | host interface that publishes `DASHBOARD_PORT`; the dashboard has no authentication |

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
| `verify-token` | check that `OP_VERIFY_TOKEN` authenticates as `--login` (used by `agent-init.sh`) |
| `cleanup` | remove run directories older than `RUN_RETENTION_DAYS` |

`--log-level LEVEL` is accepted before or after the subcommand. `provision`
prints generated passwords only with `--passwords`, and `verify-token` never
prints the token.
