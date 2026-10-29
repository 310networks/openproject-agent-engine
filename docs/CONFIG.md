# Configuration

Two files drive everything: `~/.config/op-agent-engine/workflow.yaml`
(created by `init`; the fully commented reference is
[`workflow.example.yaml`](../workflow.example.yaml)) and
`~/.config/op-agent-engine/engine.env` (secrets only). `workflow.yaml`
references secrets as `${VAR}`, resolved from the environment before
validation; a missing variable is a startup error.

With the interactive installer, edit `workflow.yaml` and `engine.env` in the
checkout root; `install.sh` copies them into `~/.config/op-agent-engine/`
before provisioning.

## `workflow.yaml` skeleton

```yaml
openproject:
  statuses: {new: "New", in_progress: "In Progress", ready_for_review: "Ready for Review",
             in_testing: "In Testing", on_hold: "On Hold", rejected: "Rejected", closed: "Closed"}
  agent_field: "AGENT"
  engine_user: "agent-engine"
  eligible_types: ["Task", "Bug", "Feature"]
projects:
  - identifier: "my-project"
    repo: {github: "owner/repo", dir: "repo", default_branch: "main"}
    enabled: true
agents:
  FRONTEND:
    name: frontendEngineer
    kind: implementer          # exactly one agent must be 'reviewer'
    op_login: frontendEngineer
    workspace: frontendEngineer
    projects: ["*"]            # ["*"] = every enabled project
    runner: pi
    env: {OP_CLI_HOST: "${OP_BASE_URL}", OP_CLI_TOKEN: "${OP_TOKEN_FRONTEND}"}
runners:
  pi:
    argv: ["pi", "-p", "--approve", "@{prompt_file}",
           "Follow the instructions in the attached prompt file."]
    cwd: "{repo_dir}"
    inject_standard: false     # pi discovers AGENTS.md from parent dirs
```

Field notes:

- `agents` keys are the `AGENT` field options. `FRONTEND`/`BACKEND`/
  `LEAD_DESIGNER` route work; `REVIEWER` exists only so option resolution
  succeeds.
- A ticket routed to an out-of-scope agent is skipped with a warning.
- `runners` are generic command templates — adding an agent CLI means adding
  a `runners` entry, no code change. Placeholders: `{prompt_file}`,
  `{repo_dir}`, `{workspace_dir}`, `{run_dir}`, `{model}`, `{branch}`,
  `{work_package_id}`.
- `inject_standard: true` prepends the workspace `AGENTS.md` to the prompt;
  keep `false` for pi.

## Environment variables (`engine.env`)

| Variable | Default | Purpose |
|---|---|---|
| `OP_BASE_URL` | required | OpenProject base URL |
| `OP_ENGINE_TOKEN` | required | `agent-engine` API token; runtime |
| `OP_PROVISION_TOKEN` | optional | admin token for `provision`/`check-config` |
| `POLL_INTERVAL_SECONDS` | `60` | poll interval |
| `MAX_CONCURRENT_RUNS` | `1` | global run cap |
| `MAX_REJECTIONS` | `3` | rejections before `On Hold` |
| `MAX_CRASH_RETRIES` | `2` | failed-run retries before `On Hold` |
| `RUN_TIMEOUT_SECONDS` | `3600` | default per-run timeout |
| `STOP_GRACE_SECONDS` | `60` | shutdown grace for running agents |
| `RUN_RETENTION_DAYS` | `30` | `cleanup` retention |
| `ENGINE_CONFIG` | `~/.config/op-agent-engine/workflow.yaml` | workflow file |
| `DATA_DIR` | `~/.local/share/op-agent-engine` | SQLite, run dirs, heartbeat, lock |
| `WORKSPACE_ROOT` | required | absolute path to the checkout's `agents/` dir |
| `GH_TOKEN` | optional | overrides host `gh`/git auth; normally unset |
| `ENGINE_PAUSED` | `false` | stop new dispatches |
| `LOG_LEVEL` | `INFO` | log level |

## CLI

| Command | Purpose |
|---|---|
| `setup` | interactive installer: init, provision, token prompts, service, dry run |
| `uninstall` | remove local files and OpenProject users (dry-run by default) |
| `init` | scaffold config and env files (idempotent) |
| `run` | long-running service loop |
| `once` | one poll; claims and executes within the slot rules |
| `dry-run` | one poll; logs the dispatch plan, writes nothing |
| `check-config` | validate the file and resolve IDs (`--offline`, `--json`) |
| `status` | heartbeat age, pause state, active and last 10 runs (`--json`) |
| `provision` | reconcile users and scoped memberships (`--apply`) |
| `install-service` | render/install the systemd units (`--apply`) |
| `cleanup` | remove run dirs older than the retention period (`--days`, `--json`) |

`--config` and `--log-level` work before or after the subcommand.
`check-config` never prints token values. `provision --json` includes the
generated passwords for users it created (so the installer can display them
one at a time); treat that output as secret.
