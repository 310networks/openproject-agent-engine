# PLAN.md — OpenProject Agent Engine

Implementation plan for an engine that monitors OpenProject work packages and dispatches coding agents (pi, OpenCode, or any CLI agent) to work on them. This document is the handoff for the building agent. Follow it in milestone order. Items marked **VERIFY** depend on external tool behavior that must be confirmed against the real tool (`--help`, API docs, a live instance) before relying on it.

---

## 1. Goal and scope

**Goal.** Poll OpenProject every 60 seconds. For each eligible work package, run the right agent in the right workspace with a rendered prompt, verify the outcome against external state (GitHub, OpenProject), and apply the status transition. OpenProject is the system of record.

**In scope:** polling, eligibility, claiming, agent dispatch, outcome verification, status transitions, retry and escalation, crash recovery, logging, dry-run mode, systemd service packaging, agent provisioning (users, roles, memberships) and per-agent project scoping.

**Out of scope (v1):** webhooks, multi-host operation, a web UI, agent session resume, automatic sub-ticket creation, notifications, and creating OpenProject statuses, types, workflow transitions and custom fields (they are validated, not created).

## 2. Locked decisions

| Area | Decision |
|---|---|
| Runtime | Python 3.12, asyncio, SQLite (stdlib `sqlite3`). Runs as a systemd service on the same host as the agents (pi and others). Not containerized. |
| Polling | 60 seconds, configurable (`POLL_INTERVAL_SECONDS`). |
| Agent routing | OpenProject custom field `AGENT` (list type) with values `FRONTEND`, `BACKEND`, `LEAD_DESIGNER`. |
| Statuses | `New`, `In Progress`, `Ready for Review`, `In Testing`, `On Hold`, `Rejected`, `Closed`. Names are configurable. `On Hold` is ignored by the engine. `In Testing` is set when a work package is claimed by `codeReviewer`. |
| Transitions | The engine performs all status transitions. Agents never change status. |
| Agent-agnostic | Agents run through a runner abstraction (section 6). Any CLI agent works through configuration. |
| Prompts | Rendered to a `.txt` file per run and passed to the agent CLI. |
| Session resume | Not used. A fix pass is a new run with a templated prompt containing the reviewer feedback. |
| Concurrency | Hard limit of one active run per agent. Global cap `MAX_CONCURRENT_RUNS` (default 1). |
| Retry cap | `MAX_REJECTIONS` default 3. Crash retry cap `MAX_CRASH_RETRIES` default 2. |
| Branch name | `op-<projectId>-<workPackageId>`. |
| Git host | GitHub. Agents use the shared global GitHub identity and the global GitHub PR skill. Branch protection is disabled on target repositories, so the same account can merge. A dedicated reviewer account may be added later (section 7, per-agent `env`). |
| Workspaces | One clone per agent per repository, under `agents/<workspace>/repositories/` in the engine repository (gitignored). No git worktrees (section 14). |
| Standards | `agents/<workspace>/AGENTS.md`, version-controlled in the engine repository (files provided separately), is the standard for all repositories. |
| OpenProject identities | One user per agent, plus one `agent-engine` user for transitions and engine comments. |
| Human-assigned tickets | Ignored by the engine in every status. |

## 3. Architecture

```
                 +---------------------------------------------------------------+
                 |            op-agent-engine process (systemd service)          |
                 |                                                               |
 OpenProject <---+--  OpenProjectClient  <--  Poller (60s)                       |
   API v3        |                              |                                |
                 |                              v                                |
                 |                        Eligibility  --> Scheduler (slots)     |
                 |                                              |                |
                 |                                              v                |
                 |   SQLite (runs)  <--  Dispatcher: claim -> prepare workspace  |
                 |                          -> render prompt -> Runner           |
                 |                                              |                |
                 |                                              v                |
 GitHub <--------+--  gh CLI / REST  <--  Outcome verifier --> Transitions       |
                 |                                                               |
                 +---------------------------------------------------------------+
        host access: agent workspaces, agent CLIs and their config, gh and git credentials
```

Components:

- **Poller:** queries OpenProject on an interval for candidate work packages.
- **Eligibility:** filters candidates (section 8).
- **Scheduler:** enforces per-agent and global limits and dispatch priority.
- **Dispatcher:** claims a work package, prepares the workspace, renders the prompt, starts the runner.
- **Runner:** starts the agent CLI as a subprocess, captures output, enforces timeouts.
- **Outcome verifier:** inspects the run result and external state (GitHub PR, OpenProject comments), then applies the transition.
- **Recovery:** reconciles state on startup.

## 4. Workflow state machine

Statuses as configured. The `AGENT` field routes `New` and `Rejected` work packages to an implementer. `Ready for Review` always routes to `codeReviewer`.

| Trigger (engine observes) | Engine action before run | Run | Outcome verification | Transition after run |
|---|---|---|---|---|
| `New`, eligible, `AGENT` set | Claim. Status to `In Progress`. Assignee to the agent user. | Implement | RESULT `BLOCKED`: blocked. Otherwise an open PR exists for the branch: success. | Success: `Ready for Review`. Blocked: `On Hold` plus engine comment. |
| `Ready for Review`, PR exists for branch | Claim. Status to `In Testing`. Assignee to the `codeReviewer` user. | Review | PR merged: approved. RESULT `REJECTED` and a new comment by the reviewer user since run start: rejected. | Approved: `Closed`. Rejected: `Rejected` (or `On Hold` if the cap is reached, with engine comment). |
| `Ready for Review`, no PR for branch | None | None | None | `On Hold` plus engine comment ("no PR found for branch"). |
| `Rejected`, eligible | Claim. Status to `In Progress`. Assignee to the implementer agent user. | Fix pass | Same as implement | Same as implement |
| `In Progress`, assigned to an agent user, no running run (after restart or crash) | Claim. | Resume | Same as implement | Same as implement. Counts toward crash retries. |
| `In Testing`, assigned to the `codeReviewer` user, no running run (after restart or crash) | Claim. | Review | Same as review | Same as review. Counts toward crash retries. |
| `On Hold`, `Closed` | None | None | None | None |

Failure handling for any run (non-zero exit, timeout, missing or invalid RESULT with no valid external evidence, verification failure):

1. Record the run as `failed` or `timeout`.
2. If the crash retry count for the work package is below `MAX_CRASH_RETRIES`, leave the work package where it is and dispatch a `resume` run on a later poll (implementers), or re-run the review while the work package stays `In Testing` (reviewer).
3. Otherwise set `On Hold` and post an engine comment containing the failure kind, exit code, and the last 40 lines of the run log.

Rejection cap: the rejection count is derived from OpenProject activity (the number of status changes to `Rejected` in the work package history). When a review would move the work package to `Rejected` and the count (including this one) exceeds `MAX_REJECTIONS`, set `On Hold` instead and post an engine comment summarizing the cap.

Rule: external verification is authoritative. The RESULT line provides the `BLOCKED` reason and diagnostics. A merged PR means approval. An open PR means the implementation finished.

## 5. Configuration

### 5.1 Environment variables

Provided through the systemd `EnvironmentFile` (section 15).

| Variable | Default | Purpose |
|---|---|---|
| `OP_BASE_URL` | required | OpenProject base URL. |
| `OP_ENGINE_TOKEN` | required | API token of the `agent-engine` OpenProject user. |
| `OP_PROVISION_TOKEN` | optional | Admin-capable token used only by `provision` and `check-config` (M8). Falls back to `OP_ENGINE_TOKEN` when that token is admin. Never used for runtime dispatch. |
| `POLL_INTERVAL_SECONDS` | 60 | Poll interval. |
| `MAX_CONCURRENT_RUNS` | 1 | Global cap on simultaneous runs. |
| `MAX_REJECTIONS` | 3 | Rejection cap per work package. |
| `MAX_CRASH_RETRIES` | 2 | Crash retry cap per work package. |
| `RUN_TIMEOUT_SECONDS` | 3600 | Default per-run timeout. Overridable per agent. |
| `STOP_GRACE_SECONDS` | 60 | Time to let running runs finish on shutdown. |
| `ENGINE_CONFIG` | `~/.config/op-agent-engine/workflow.yaml` | Path to the workflow file. |
| `DATA_DIR` | `~/.local/share/op-agent-engine` | SQLite database, run directories, logs. |
| `WORKSPACE_ROOT` | required | Absolute path to the `agents/` directory of the engine checkout the service runs from (section 15). |
| `GH_TOKEN` | optional | Overrides the host's existing `gh` and git authentication. Normally unset. |
| `ENGINE_PAUSED` | false | Stop dispatching new runs. Running runs finish. |
| `LOG_LEVEL` | INFO | Log level. |

Per-agent OpenProject tokens are referenced from `workflow.yaml` (section 5.2), not as fixed variable names.

### 5.2 `workflow.yaml`

```yaml
openproject:
  statuses:
    new: "New"
    in_progress: "In Progress"
    ready_for_review: "Ready for Review"
    in_testing: "In Testing"
    on_hold: "On Hold"
    rejected: "Rejected"
    closed: "Closed"
  agent_field: "AGENT"            # custom field name; resolved to customField<N> at startup
  engine_user: "agent-engine"     # login of the engine user
  provisioning:                   # M8; used only by provision/check-config
    enabled: true
    agent_role: "Agent Worker"     # role name on this instance
    engine_role: "Agent Engine"
  dispatch_priority: [ready_for_review, rejected, new]   # finish work before starting new work
  eligible_types: ["Task", "Bug", "Feature"]
  skip_work_packages_with_children: true
  skip_if_assigned_to_human: true

projects:
  - identifier: "example-project"   # OpenProject project identifier
    repo:
      github: "owner/example-project"
      dir: "example-project"   # clone directory name under <workspace>/repositories/
      default_branch: "main"
    enabled: true

agents:
  FRONTEND:
    name: frontendEngineer
    kind: implementer
    op_login: "frontendEngineer"
    workspace: "frontendEngineer"          # under WORKSPACE_ROOT
    projects: ["*"]                        # M8 scope; ["*"] = every enabled project
    runner: pi
    model: null                            # optional, passed to runner as {model}
    timeout_seconds: 3600
    env:
      OPENPROJECT_API_KEY: "${OP_TOKEN_FRONTEND}"   # VERIFY the variable names the OpenProject skill reads
      OPENPROJECT_URL: "${OP_BASE_URL}"
  BACKEND:
    name: backendEngineer
    kind: implementer
    op_login: "backendEngineer"
    workspace: "backendEngineer"
    runner: pi
    env: { OPENPROJECT_API_KEY: "${OP_TOKEN_BACKEND}", OPENPROJECT_URL: "${OP_BASE_URL}" }
  LEAD_DESIGNER:
    name: leadDesigner
    kind: implementer
    op_login: "leadDesigner"
    workspace: "leadDesigner"
    runner: pi
    env: { OPENPROJECT_API_KEY: "${OP_TOKEN_DESIGNER}", OPENPROJECT_URL: "${OP_BASE_URL}" }
  REVIEWER:
    name: codeReviewer
    kind: reviewer
    op_login: "codeReviewer"
    workspace: "codeReviewer"
    runner: pi
    env: { OPENPROJECT_API_KEY: "${OP_TOKEN_REVIEWER}", OPENPROJECT_URL: "${OP_BASE_URL}" }
    # Future: add GH_TOKEN here to give the reviewer a dedicated GitHub account.

runners:
  pi:
    argv: ["pi", "-p", "@{prompt_file}", "Follow the instructions in the attached prompt file."]   # VERIFY
    cwd: "{repo_dir}"
    env: {}                        # pi uses the host's default ~/.pi/agent configuration
    inject_standard: false         # pi loads AGENTS.md from parent directories
  opencode:
    argv: ["opencode", "run", "--file", "{prompt_file}", "Follow the instructions in the attached prompt file."]   # VERIFY
    cwd: "{repo_dir}"
    env: {}
    inject_standard: true          # do not rely on parent-directory discovery
  generic_stdin:
    argv: ["my-agent", "--non-interactive"]
    stdin_file: "{prompt_file}"
    cwd: "{repo_dir}"
    inject_standard: true
```

Rules:

- `${VAR}` placeholders resolve from the engine's environment. A missing variable is a startup error.
- Any agent may use any runner. Mixing runners is supported (for example, implementers on OpenCode and the reviewer on pi).
- Per-agent `env` entries override the inherited environment for that agent's runs only.
- `projects` limits the projects the engine may dispatch to that agent. `["*"]` means every project with `enabled: true`; every other identifier must be declared in `projects`. The field defaults to `["*"]`.
- `openproject.provisioning` is read only by `provision` and `check-config` (M8); runtime dispatch never uses the provisioning token.
- `check-config` validates the whole file and every reference (section 18, M1).

## 6. Runner abstraction (agent-agnostic)

The engine knows nothing about specific agents. It only starts a command, feeds it a prompt file, captures output, and reads the result.

```python
class RunRequest:   # inputs to a runner
    run_id: int
    argv: list[str]          # after placeholder substitution
    cwd: Path
    env: dict[str, str]
    stdin_file: Path | None
    timeout_seconds: int
    log_dir: Path            # stdout.log, stderr.log written here

class RunResult:
    exit_code: int | None    # None if killed
    timed_out: bool
    stdout_tail: str         # last N KB for parsing
    result_line: str | None  # last line matching ^RESULT:\s+(PR_OPENED|APPROVED|REJECTED|BLOCKED)\b
    duration_seconds: float
```

Requirements:

- Placeholders available in `argv`, `cwd`, `env`, `stdin_file`: `{prompt_file}`, `{repo_dir}`, `{workspace_dir}`, `{run_dir}`, `{model}`, `{branch}`, `{work_package_id}`.
- Start the process in its own process group (new session). On timeout, send SIGTERM to the group, wait a 20 second grace period, then SIGKILL.
- Stream stdout and stderr to files in the run directory as they arrive. Keep the full transcript. Parse the result line from the tail of stdout.
- Child processes inherit only a minimal environment: `PATH`, `HOME`, `USER`, `TZ`, `GH_TOKEN` (when set), plus the runner's and agent's `env` entries. The service `PATH` must include the directory of every agent CLI (for example a Node version manager's bin directory).
- `inject_standard: true` prepends the contents of the workspace `AGENTS.md` to the prompt file under a heading. The prompt always names the standard file path as well.
- Non-interactive runs must never block on approval prompts. **VERIFY** per agent that the configured flags disable confirmation prompts, and add them to `argv` where needed.
- Adding a new agent requires only a new `runners` entry. No code change.

Known command shapes (**VERIFY** with `--help` before use): pi accepts `-p` for print mode and `@file` arguments, and loads `AGENTS.md` from its global directory, parent directories, and the current directory. OpenCode offers a non-interactive `run` command and file attachment.

## 7. OpenProject integration

**VERIFY** every endpoint and filter against the live instance (`/api/docs`).

- **Auth:** HTTP Basic with username `apikey` and the token as the password.
- **Startup resolution (fail fast if anything is missing):**
  - `GET /api/v3/statuses`: map configured status names to IDs.
  - `GET /api/v3/users` (or by login filter): resolve agent users and `agent-engine`.
  - Custom field: resolve the `AGENT` field name to `customField<N>` and its option IDs through the work package schema (`/api/v3/work_packages/schemas/<projectId>-<typeId>`).
  - `GET /api/v3/projects/<identifier>`: resolve project IDs.
- **List candidates:** `GET /api/v3/work_packages` with the `filters` parameter (JSON array), for example:
  - project: `{"project":{"operator":"=","values":["<id>"]}}`
  - status: `{"status":{"operator":"=","values":["<id>"]}}`
  - agent field: `{"customField<N>":{"operator":"=","values":["<optionId>"]}}`
  - Use `pageSize` and follow pagination until exhausted. Sort by priority, then ID.
- **Update status and assignee:** `PATCH /api/v3/work_packages/<id>` with the current `lockVersion` and `_links.status` / `_links.assignee`. On HTTP 409 (stale lock), re-read the work package and retry once. If the status is no longer the expected one, abandon the claim.
- **Comments:** `POST /api/v3/work_packages/<id>/activities` with body `{"comment": {"raw": "<markdown>"}}`.
- **Activities:** `GET /api/v3/work_packages/<id>/activities`. Each entry carries the author link, the comment, and the change details (used for rejection counting and comment filtering).
- **Relations:** `GET /api/v3/work_packages/<id>/relations`. A work package is blocked if any relation of type `blocked` (blocked by) points to a work package whose status is not `Closed`.
- **Children:** a work package with children is a container and is skipped when `skip_work_packages_with_children` is true.
- **Permissions model (provisioned by M8, documented in the README):**
  - Agent users: project member with a role that can view work packages and add comments (notes), and has **no workflow transitions**. This enforces "engine owns transitions" at the permission level. Agent users must be assignable so the engine can set the assignee.
  - `agent-engine` user: project member with a role that can edit work packages (status, assignee) and comment. Configure workflow transitions for this role and each eligible type: `New` to `In Progress`, `In Progress` to `Ready for Review`, `Ready for Review` to `In Testing`, `In Testing` to `Closed`, `In Testing` to `Rejected`, `Rejected` to `In Progress`, and every status the engine handles to `On Hold`. Workflow transitions are configured per role and type in OpenProject.
- **Errors:** retry transient failures (network errors, HTTP 429 and 5xx) with exponential backoff and jitter, maximum 5 attempts. A failed poll never crashes the engine. The next poll retries.

## 8. Eligibility rules

A work package is dispatched only if all conditions hold:

1. Its project is listed in `projects` with `enabled: true`.
2. Its type is in `eligible_types`.
3. Its status is `New`, `Rejected`, `Ready for Review`, or (recovery only) `In Progress` or `In Testing` assigned to an agent user.
4. For `New` and `Rejected`: the `AGENT` field has a configured value.
5. It is not assigned to a human (any user other than the configured agent users and `agent-engine`) when `skip_if_assigned_to_human` is true. Unassigned work packages are eligible.
6. The project is inside the target agent's `projects` scope (M8). A routing value that points at an agent without access to the project is skipped with a warning and never claimed.
7. It has no unfinished blockers (relations) and no children (when configured).
8. It has no active run in the `runs` table.
9. The target agent has no active run (one active run per agent).
10. A global slot is available (`MAX_CONCURRENT_RUNS`).

Work packages that cannot run because no slot is free are not claimed. They remain unchanged and are reconsidered on the next poll.

Dispatch order: by `dispatch_priority` (status order), then OpenProject priority (highest first), then work package ID (lowest first).

## 9. Claiming and idempotency

1. Insert a `runs` row with state `running`. Unique partial indexes on `work_package_id` and on `agent` (where state is `running`) make a double claim and a second run for the same agent impossible. If the insert fails, skip.
2. For implementers, patch the status to `In Progress` and the assignee to the agent user. For the reviewer, patch the status to `In Testing` and the assignee to the `codeReviewer` user.
3. If the patch fails, mark the run `cancelled` and skip.
4. Only then prepare the workspace and start the runner.

The engine runs as a single instance. Acquire an exclusive file lock on `DATA_DIR/engine.lock` at startup and exit if it is held.

## 10. Prompt templates

Use Jinja2 with `StrictUndefined`. Templates live in `prompts/` and are rendered to `DATA_DIR/runs/<runId>/prompt.txt`. Each template begins with a standard-file reference and ends with the result-line reminder.

`prompts/implement.txt.j2`:

```
You are the {{ agent.name }} agent. Follow the engineering standard in {{ standard_path }} exactly.

Task
- Ticket: OP#{{ wp.id }} - {{ wp.subject }}
- Type: {{ wp.type }}    Project: {{ project.identifier }}
- Ticket URL: {{ wp.url }}
- Repository: {{ repo.github }}    Directory: {{ repo_dir }}
- Branch: {{ branch }}    Base branch: {{ repo.default_branch }}

Description:
{{ wp.description }}

Begin now. Read the ticket through the OpenProject skill if you need more context (comments, relations).
Finish with the RESULT line required by the standard.
```

`prompts/fix.txt.j2` (status `Rejected`):

```
You are the {{ agent.name }} agent. Follow the engineering standard in {{ standard_path }} exactly.

This is fix pass {{ attempt }} of {{ max_rejections }} for OP#{{ wp.id }} - {{ wp.subject }}.
Repository: {{ repo.github }}    Directory: {{ repo_dir }}
Branch: {{ branch }} (an open PR already exists for it)

The code reviewer rejected the PR. Latest reviewer feedback ({{ feedback.author }}, {{ feedback.created_at }}):

{{ feedback.text }}

{% for c in later_human_comments %}
Additional comment from {{ c.author }} ({{ c.created_at }}):
{{ c.text }}

{% endfor %}
Address every blocking point, push to the same branch, and update the existing PR. Do not open a new PR.
Finish with the RESULT line required by the standard.
```

`prompts/resume.txt.j2` (crash recovery): same header as `implement`, plus:

```
A previous run on this ticket ended unexpectedly. Inspect the branch state (git status, git log, any open PR) and continue from there. Do not repeat completed work and do not discard existing commits.
```

`prompts/review.txt.j2`:

```
You are the {{ agent.name }} agent. Follow the review standard in {{ standard_path }} exactly.

Review OP#{{ wp.id }} - {{ wp.subject }}.
Repository: {{ repo.github }}    Directory: {{ repo_dir }}
Branch: {{ branch }}    PR: {{ pr.url }}    Base branch: {{ repo.default_branch }}
Review attempt: {{ review_number }}

Ticket description:
{{ wp.description }}

Review the whole PR against the ticket and the standard. Approve and merge, or reject with a single self-contained ticket comment.
Finish with the RESULT line required by the standard.
```

Comment selection for `fix.txt.j2`:

- `feedback`: the latest comment authored by the `codeReviewer` user.
- `later_human_comments`: comments authored by humans (users who are neither agent users nor `agent-engine`), posted after `feedback`. Cap at 5, newest last.
- Exclude all other agent and engine comments. Label every comment with its author and role.

## 11. Workspace preparation (before every run)

Layout: `WORKSPACE_ROOT/<workspace>/repositories/<repo.dir>` holds one clone per agent per repository. `WORKSPACE_ROOT/<workspace>/AGENTS.md` is the agent's standard. The `repositories/` directories are gitignored in the engine repository (`agents/*/repositories/`).

1. Create `repositories/` if it does not exist. If the clone is missing, `gh repo clone <repo.github> <repo.dir>`.
2. Verify the clone: `git rev-parse --show-toplevel`, run inside the clone directory, must equal the clone path. If it does not, fail the run as an infrastructure failure. This prevents git commands from falling through to the engine repository's own `.git`.
3. `git fetch --prune origin`.
4. If the working tree is dirty (leftovers from a crashed run), save `git diff` and `git status --porcelain` to the run directory, then `git reset --hard` and `git clean -fd`. Do not use `-x`, so ignored dependency directories remain.
5. Check out the branch:
   - Implementer, branch exists on origin: `git checkout -B <branch> origin/<branch>`.
   - Implementer, no branch: `git checkout -B <branch> origin/<default_branch>`.
   - Reviewer: `git checkout -B <branch> origin/<branch>`.
6. Any failure here marks the run `failed` (infrastructure) and counts toward crash retries.

**Context-file hygiene.** Pi, and agents that behave like it, load `AGENTS.md` and `CLAUDE.md` files from the global agent directory and from every parent directory of the working directory. Therefore:

- Do not place an `AGENTS.md` or `CLAUDE.md` file at the engine repository root or in any directory above `agents/`. Instructions for the agent that builds the engine belong under `docs/` or in its prompt.
- The global agent configuration (for example `~/.pi/agent/AGENTS.md`) also loads for every worker run. Keep it consistent with the workspace standards.
- Target repositories may contain their own `AGENTS.md`. The workspace standard states that it takes precedence.

## 12. Local state (SQLite, disposable)

```sql
CREATE TABLE runs (
  id INTEGER PRIMARY KEY,
  work_package_id INTEGER NOT NULL,
  project_id INTEGER NOT NULL,
  agent TEXT NOT NULL,
  kind TEXT NOT NULL,            -- implement | fix | resume | review
  state TEXT NOT NULL,           -- running | succeeded | failed | timeout | cancelled | interrupted
  attempt INTEGER NOT NULL,
  branch TEXT NOT NULL,
  pid INTEGER,
  pgid INTEGER,
  proc_start TEXT,               -- process start time, guards against PID reuse
  started_at TEXT NOT NULL,
  finished_at TEXT,
  exit_code INTEGER,
  result_line TEXT,
  outcome TEXT,                  -- transition applied, or reason
  run_dir TEXT NOT NULL
);
CREATE UNIQUE INDEX one_active_per_wp    ON runs(work_package_id) WHERE state = 'running';
CREATE UNIQUE INDEX one_active_per_agent ON runs(agent)           WHERE state = 'running';

CREATE TABLE crash_counts (
  work_package_id INTEGER PRIMARY KEY,
  count INTEGER NOT NULL DEFAULT 0
);
```

The database holds run history, active-run tracking, and crash counters only. Rejection counts and all workflow state come from OpenProject. Deleting the database loses history and crash counters but never corrupts the workflow.

## 13. Crash recovery and shutdown

**On startup:**

1. Acquire the engine lock.
2. Mark every `running` row `interrupted`. The service unit uses `KillMode=control-group`, so a normal stop or restart terminates all agent processes. After a hard crash of the engine, kill any leftover process group recorded for a run, but only after verifying that the process start time matches the value recorded at launch (guards against PID reuse).
3. Run one poll. Recover work packages as follows:
   - `In Progress`, assigned to an agent user, with no running run: dispatch a `resume` run.
   - `In Testing`, assigned to `codeReviewer`, with no running run: dispatch a `review` run.
   - An `interrupted` run does not increment the crash count. A `failed` or `timeout` run does.
4. Work packages in `Ready for Review` are picked up as normal.

**On SIGTERM:** stop polling, stop dispatching, wait up to `STOP_GRACE_SECONDS` for runs to finish, then terminate the remaining runs, mark them `interrupted`, and exit. The next startup recovers them.

**Pause:** `ENGINE_PAUSED=true` or a `PAUSE` file in `DATA_DIR` stops new dispatches. Running runs finish. Per project, `enabled: false` stops monitoring that project. Per ticket, `On Hold` stops the engine.

## 14. Workspaces: why no worktrees

A git worktree is an extra checked-out directory sharing one repository's history, which allows several branches to be checked out at once. It is needed only if one agent runs several tickets in parallel on the same repository. This design allows one active run per agent, and each agent has its own clone, so parallel work across agents is already isolated. The preparation step in section 11 provides clean state before every run. Do not implement worktrees.

## 15. Deployment (systemd)

The engine runs as a systemd service on the host where the agent CLIs, their configuration (skills, models, settings), `gh` authentication, and git identity already exist. It runs as that host user so agents see the same environment as a manual run.

**Installation layout:**

- Virtual environment: `~/.local/share/op-agent-engine/venv` (or `pipx`), with the engine installed from the repository.
- Engine checkout: a dedicated checkout of the engine repository, kept on `main` and separate from any development clone. `WORKSPACE_ROOT` points to its `agents/` directory. The live agents read their `AGENTS.md` files from this checkout, so changing its checked-out branch changes the live standard. Update it with `git pull` on `main` and restart the service.
- Configuration: `~/.config/op-agent-engine/workflow.yaml`.
- Secrets: `~/.config/op-agent-engine/engine.env`, mode `0600` (`OP_BASE_URL`, `OP_ENGINE_TOKEN`, optional `OP_PROVISION_TOKEN`, per-agent tokens such as `OP_TOKEN_FRONTEND`).
- Data: `~/.local/share/op-agent-engine/` (database, run directories).

**Unit file** `/etc/systemd/system/op-agent-engine.service` (provide as `deploy/op-agent-engine.service` with `<host-user>` placeholders):

```ini
[Unit]
Description=OpenProject Agent Engine
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=<host-user>
Group=<host-user>
WorkingDirectory=/home/<host-user>
EnvironmentFile=/home/<host-user>/.config/op-agent-engine/engine.env
# Must include the directories of every agent CLI (pi, opencode, gh, git, node).
Environment=PATH=/home/<host-user>/.local/bin:/usr/local/bin:/usr/bin:/bin
ExecStart=/home/<host-user>/.local/share/op-agent-engine/venv/bin/op-agent-engine run --config /home/<host-user>/.config/op-agent-engine/workflow.yaml
Restart=on-failure
RestartSec=10
KillMode=control-group
TimeoutStopSec=90
NoNewPrivileges=yes
PrivateTmp=yes

[Install]
WantedBy=multi-user.target
```

Notes:

- `TimeoutStopSec` must exceed `STOP_GRACE_SECONDS` plus the 20 second kill grace.
- If the agent CLIs are installed through a Node version manager, add its bin directory to `PATH` explicitly. A systemd service does not read shell profile files.
- The service sets `HOME` from `User=`, so `~/.pi`, `~/.gitconfig`, and `gh` configuration resolve as in a normal login.
- Agents run with the privileges of the host user. Stricter sandboxing options (for example `ProtectSystem=strict` or `ProtectHome`) can break agent toolchains. Document this in the README as an accepted risk, and note that a dedicated Unix user is a possible later hardening step.

**Operations (document in the README, editing with vim):**

```
sudo vim /etc/systemd/system/op-agent-engine.service
vim ~/.config/op-agent-engine/workflow.yaml
sudo systemctl daemon-reload
sudo systemctl enable --now op-agent-engine
sudo systemctl restart op-agent-engine
journalctl -u op-agent-engine -f
op-agent-engine status
```

**Health:** the engine writes a heartbeat timestamp to `DATA_DIR/heartbeat` after each poll. The `status` command prints the heartbeat age, active runs, and the last 10 finished runs. Optional: send `READY=1` and periodic `WATCHDOG=1` notifications through the `NOTIFY_SOCKET` (no extra dependency) and switch the unit to `Type=notify` with `WatchdogSec=` set to three poll intervals.

## 16. Observability and safety

- Structured JSON logging to stdout (collected by journald) with fields: `run_id`, `work_package_id`, `agent`, `event`.
- Per-run directory `DATA_DIR/runs/<runId>/` containing `prompt.txt`, `stdout.log`, `stderr.log`, and `meta.json`. Retention is configurable (default 30 days). A cleanup job removes older directories.
- Every transition and engine comment is logged with its reason.
- **Dry-run mode** (`--dry-run`): log every claim, transition, and spawn that would occur. Perform no write to OpenProject, no git operation beyond read-only checks, and no process start.
- **Engine comments:** every comment the engine posts begins with a line `ENGINE:` and a short event name, so humans can distinguish them. Comments are posted as the `agent-engine` user.
- Never log tokens. Redact `OP_*` and `GH_TOKEN` values from logs and from the `meta.json` environment snapshot.
- Ticket and PR content are untrusted input. The engine never executes content from them. They only appear inside prompt text.

## 17. Project layout

```
openproject-agent-engine/
├── .gitignore                     # includes: agents/*/repositories/
├── pyproject.toml
├── docs/
│   └── PLAN.md
├── agents/                        # WORKSPACE_ROOT
│   ├── frontendEngineer/AGENTS.md
│   ├── backendEngineer/AGENTS.md
│   ├── leadDesigner/AGENTS.md
│   ├── codeReviewer/AGENTS.md
│   └── <each>/repositories/       # gitignored, engine-managed clones
├── README.md                      # setup: OpenProject roles, statuses, workflows, custom field, tokens, systemd install
├── config/workflow.example.yaml
├── deploy/
│   ├── op-agent-engine.service    # unit file with <host-user> placeholders
│   └── engine.env.example
├── prompts/                       # *.j2 templates (section 10)
├── src/op_agent_engine/
│   ├── cli.py                     # run | once | check-config | status | dry-run
│   ├── config.py                  # pydantic models, env interpolation, validation
│   ├── logging.py
│   ├── store.py                   # SQLite access
│   ├── openproject/
│   │   ├── client.py              # httpx async client, retries, pagination
│   │   └── resolve.py             # status/user/custom-field/project resolution
│   ├── github.py                  # PR lookup and merge-state checks via gh
│   ├── poller.py
│   ├── eligibility.py
│   ├── scheduler.py               # slots, priority
│   ├── dispatcher.py              # claim, prepare, render, run
│   ├── workspace.py               # section 11
│   ├── prompts.py                 # rendering, comment selection
│   ├── provision.py               # users, roles, memberships (M8)
│   ├── runners/
│   │   ├── base.py                # RunRequest, RunResult, process handling
│   │   └── generic.py             # argv/stdin runner driven by config
│   ├── outcomes.py                # verification and transitions (section 4)
│   └── recovery.py
└── tests/
    ├── fakes/                     # FakeOpenProject, FakeGitHub, fake agent script
    └── ...
```

Libraries: `httpx`, `pydantic` v2, `pydantic-settings`, `PyYAML`, `Jinja2`, `structlog` (optional), `pytest`, `pytest-asyncio`, `respx`, `ruff`, `mypy`.

## 18. Milestones and acceptance criteria

Each milestone ends with passing tests, `ruff`, and `mypy`, and a commit.

**M0 - Skeleton.** Package, config models, logging, CLI entry points, example configuration, unit file and environment file examples. The `agents/` directory with the four `AGENTS.md` files and the `.gitignore` entry already exist in the repository (do not modify the `AGENTS.md` files).
Accept: `op-agent-engine check-config` loads the example file and reports validation errors clearly; the package installs into a fresh virtual environment and `op-agent-engine --help` runs.

**M1 - OpenProject client and resolution.** Client with auth, retries, pagination; startup resolution of statuses (including `In Testing`), users (including `agent-engine`), projects, and the custom field.
Accept: against a mocked API, resolution succeeds and fails fast with a specific message for each missing item; `check-config` reports each resolved ID.

**M2 - Poller, eligibility, dry-run.** Candidate queries, all rules in section 8 (including ignoring human-assigned work packages), dispatch ordering, dry-run logging.
Accept: table-driven tests cover every eligibility rule and ordering; `--dry-run` against a mock logs the expected dispatch plan and writes nothing.

**M3 - Store, claiming, scheduler.** SQLite schema, claim protocol, per-agent and global limits.
Accept: tests prove that a work package cannot be claimed twice, an agent cannot have two runs, the global cap is respected, and unclaimed work is not modified when no slot is free.

**M4 - Runner and implementer flow.** Generic runner with process groups, timeouts, logging, result parsing; workspace preparation (including the clone path check); prompt rendering; implementer dispatch; outcome verification for `PR_OPENED` and `BLOCKED`.
Accept: with a fake agent script, scenarios succeed or fail as expected (PR opened, blocked, non-zero exit, timeout, missing PR); the transitions in section 4 are applied; timeouts kill the process group.

**M5 - Reviewer flow.** Reviewer dispatch with the `In Testing` claim, PR merged detection, rejection verification, `Closed` and `Rejected` transitions.
Accept: fake scenarios cover approved, rejected with comment, rejected without comment (treated as failure), blocked, and `Ready for Review` without a PR.

**M6 - Rejection loop and comments.** Fix-pass dispatch, rejection counting from activities, cap escalation to `On Hold`, comment selection by author role.
Accept: tests cover the cap boundary (rejection 3 allowed, rejection 4 escalates with the defaults), latest-reviewer-comment selection, inclusion of later human comments, and exclusion of agent and engine comments.

**M7 - Recovery, shutdown, pause.** Startup reconciliation (including `In Testing` recovery), resume runs, crash retry caps, PID-reuse-safe cleanup of leftover processes, graceful shutdown, pause controls, heartbeat, `status` command, optional systemd watchdog.
Accept: tests simulate a restart with `In Progress` and `In Testing` work packages and verify resume and re-review dispatch; an interrupted run does not count toward the crash cap; a failed run does; shutdown marks runs `interrupted`.

**M8 - Agent provisioning and project scoping.** `op-agent-engine provision` reconciles the four agent users and the `agent-engine` user from `workflow.yaml`, the roles they need, and their project memberships; the engine then enforces the per-agent project scope declared in configuration. The configuration fields this milestone adds are in section 5.2.

Decisions:

- Only users, roles, memberships and scope are provisioned. Statuses, types, workflow transitions, the `AGENT` custom field and its options stay manual admin setup. `check-config` validates them (M1); `provision` does not create them.
- Provisioning needs an admin-capable token (`OP_PROVISION_TOKEN`, falling back to `OP_ENGINE_TOKEN` when that token is admin). Runtime dispatch keeps the least-privilege `OP_ENGINE_TOKEN`. The two tokens are never interchangeable.
- Tokens are not minted. Expected: OpenProject creates API tokens in the UI, not through the API (VERIFY, section 20 item 10). `provision` reports which tokens are still missing and `check-config` verifies each configured `OP_TOKEN_*` belongs to the expected login through `GET /api/v3/users/me`.
- Users: create when missing, never change an existing login; update the display name and email only when they differ. Never delete or deactivate.
- Roles: create when missing with the fixed permission sets from section 7 (agent role: view work packages, add notes, no transitions, assignable; engine role: view and edit work packages, add notes, configured transitions). If a role with the configured name exists with different permissions, fail and report the drift instead of rewriting it.
- Memberships: one membership per agent per project in its scope, carrying the configured role. A missing membership is created. An existing membership with the wrong role is updated to the configured role. A membership is never removed, and a human-added membership is never touched.
- `projects` on an agent defaults to `["*"]` (every project with `enabled: true`). A non-wildcard identifier that is not declared in `projects` is a configuration error. `["*"]` never reaches projects that are not declared.
- The engine applies the same scope when deciding eligibility: a work package in a project outside the routed agent's scope is skipped with a warning, never claimed, and left unmodified (not sent to `On Hold`).
- `provision` is dry-run by default and writes only with `--apply`. `check-config` performs the same lookups read-only and reports per-agent scope plus membership and role drift. Neither command runs in the polling loop.

Accept: with a fake OpenProject, `provision --apply` creates the five users, both roles and one membership per agent per scoped project; a second run performs no writes; the wildcard expands to enabled projects only; a role with drifted permissions fails with a specific message; a mis-scoped `AGENT` value is skipped without a status change; a token whose login does not match its agent fails startup; `--dry-run` writes nothing; no secret appears in logs or `meta.json`. On a live instance, verify the endpoints and permission requirements in section 20 items 9-11, then run the full cycle from an empty project with the five provisioned users.

**M9 - Documentation and hardening.** README with full OpenProject setup (roles, permissions, statuses, workflows including the `agent-engine` transitions in section 7, custom field, tokens), the provisioning and scoping workflow (M8), systemd installation steps, configuration reference, runbook (pause, inspect a run, handle `On Hold`), log retention job, secret redaction tests.
Accept: a person with an empty OpenProject instance can follow the README to provision the five users, the roles and the scoped memberships, then reach a working end-to-end run using a fake agent; no secret appears in logs.

## 19. Testing strategy

- **Unit tests** for eligibility (including project scoping), scheduler, outcome table (section 4), comment selection, prompt rendering, config validation, placeholder substitution, provisioning reconciliation and idempotency.
- **Fakes:** `FakeOpenProject` (in-memory work packages, activities, relations, optimistic locking, users, roles, memberships), `FakeGitHub` (PR existence and merge state), and a fake agent script (`tests/fakes/fake_agent.sh`) whose behavior is selected by an environment variable (`pr_opened`, `blocked`, `crash`, `hang`, `approve`, `reject_with_comment`, `reject_without_comment`).
- **HTTP tests** with `respx` for the real client's request shapes, retries, pagination, and 409 handling.
- **Process tests** for timeout and process-group termination.
- **End-to-end test** against fakes, running a full cycle `New` to `Closed`, and a cycle with two rejections.
- **Manual acceptance checklist** (in README) against a real OpenProject project with a throwaway repository and the real agents.

## 20. Items to verify before or during implementation

1. Exact pi non-interactive command shape (`-p`, `@file`, trailing message) and flags that disable approval prompts.
2. OpenCode (or any additional agent) non-interactive command shape and approval flags.
3. Environment variable names the OpenProject skill reads for URL and token.
4. OpenProject API details: custom field filter values for list fields, relation types as seen from each side, activity entry structure, permission names for adding comments.
5. That every agent CLI resolves under the systemd service `PATH` (for example when installed through a Node version manager), and that `gh` and git authenticate without an interactive login.
6. Whether repositories contain their own `AGENTS.md` files. The workspace standard states precedence, but removing conflicting files avoids confusion.
7. If branch protection is enabled later, a rule requiring an approving review blocks the reviewer's merge while all agents share one GitHub account. A dedicated reviewer account (through the reviewer's `env` `GH_TOKEN`) resolves this.
8. That no `AGENTS.md` or `CLAUDE.md` exists at the engine repository root or above `agents/`, and that the global agent configuration does not contradict the workspace standards (section 11).
9. OpenProject user, role and membership API: `POST /api/v3/users` request fields and permission requirements, `GET`/`POST /api/v3/roles` (including the `assignable` attribute), and the `POST /api/v3/memberships` payload shape; whether `manage_user` alone suffices or administrator is required for each.
10. Whether API tokens can be created programmatically (expected: no) and that `GET /api/v3/users/me` returns the login of the token owner, so a per-agent token can be verified before dispatch.
11. The permission names each role needs, and whether roles can be created through the API with those permissions: work package view/edit, `add_work_package_notes`, and the workflow transitions from section 7.
