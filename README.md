# openproject-agent-engine

Polls OpenProject work packages and dispatches CLI coding agents (pi, OpenCode,
or any configured command) to work on them. OpenProject is the system of
record: the engine claims work, prepares a per-agent clone, renders a prompt,
runs the agent, verifies the outcome against GitHub and OpenProject, and
performs **every** status transition itself. Agents never change status.

The implementation plan, workflow state machine and design rationale live in
[`docs/PLAN.md`](docs/PLAN.md). Live-instance findings (API shapes, CLI flags,
permission requirements) are in [`docs/VERIFICATION.md`](docs/VERIFICATION.md).
The one-time manual OpenProject role setup is in
[`docs/OPENPROJECT_ADMIN_SETUP.md`](docs/OPENPROJECT_ADMIN_SETUP.md).

## Contents

- [How a ticket flows](#how-a-ticket-flows)
- [Requirements](#requirements)
- [Quick start](#quick-start)
- [OpenProject setup from an empty instance](#openproject-setup-from-an-empty-instance)
- [Configure the engine](#configure-the-engine)
- [Provision users and project scope](#provision-users-and-project-scope)
- [Install as a systemd service](#install-as-a-systemd-service)
- [Runbook](#runbook)
- [Acceptance test with a fake agent](#acceptance-test-with-a-fake-agent)
- [Security notes](#security-notes)
- [Development](#development)
- [Verified external behavior](#verified-external-behavior)

## How a ticket flows

| Observed state | Engine action before the run | Run | Verified outcome | Transition |
|---|---|---|---|---|
| `New`, eligible, `AGENT` set | Claim, set `In Progress`, assign the implementer | implement | open PR exists / `BLOCKED` | `Ready for Review` / `On Hold` + `ENGINE:` comment |
| `Ready for Review`, PR exists | Claim, set `In Testing`, assign `codeReviewer` | review | PR merged / `REJECTED` + reviewer comment since run start | `Closed` / `Rejected` (or `On Hold` at the rejection cap) |
| `Ready for Review`, no PR | none | none | none | `On Hold` + `ENGINE: no PR found` |
| `Rejected`, eligible | Claim, set `In Progress`, assign the implementer | fix | same as implement | same as implement |
| `In Progress` / `In Testing`, agent-assigned, no live run | Claim | resume / re-review | same as above | same as above |
| `On Hold`, `Closed`, human-assigned | ignored | none | none | none |

Rules that matter in practice:

- Branch name is `op-<projectId>-<workPackageId>`; one branch per ticket.
- One active run per agent and `MAX_CONCURRENT_RUNS` globally (default 1).
- Dispatch order is `ready_for_review`, then `rejected`, then `new`; within a
  status, highest OpenProject priority first, then lowest work package ID.
- The engine never edits ticket descriptions, never executes ticket or PR
  content, and never creates statuses, types, custom fields or workflows.
- A failed run is retried up to `MAX_CRASH_RETRIES` (default 2): an
  implementer gets a `resume` run, while the reviewer re-runs the review with
  the ticket staying `In Testing`. Beyond the cap the ticket moves to `On Hold`
  and the engine posts the failure kind, exit code and the last 40 log lines.
- The rejection count comes from OpenProject history. With `MAX_REJECTIONS=3`
  the first three rejections are applied; the fourth moves the ticket to
  `On Hold` with an `ENGINE: rejection cap reached` comment.
- Every engine comment starts with `ENGINE:` so humans can tell it apart from
  agent and human comments.

## Requirements

- Python 3.12 (the package requires `>=3.12`).
- A host that already has the agent CLIs, their configuration, `gh`
  authentication and the git identity. The engine runs as that host user so
  runs see the same environment as a manual run.
- OpenProject 17 (API v3), a project, and administrator access once for
  setup.
- A GitHub repository per project that the service user can clone, push to and
  merge into (or a reviewer `GH_TOKEN` with that access).
- `git` and `gh` on the service `PATH`. If agent CLIs are installed through a
  Node version manager, add that bin directory to the unit's `PATH`.

## Quick start

```bash
# 1. Install the engine (see "Install as a systemd service" for a
#    production layout).
git clone <engine-repo> ~/op-agent-engine
cd ~/op-agent-engine
python3.12 -m venv ~/.local/share/op-agent-engine/venv
~/.local/share/op-agent-engine/venv/bin/pip install -e '.[dev]'

# 2. Copy and edit configuration.
mkdir -p ~/.config/op-agent-engine
cp config/workflow.example.yaml ~/.config/op-agent-engine/workflow.yaml
cp deploy/engine.env.example ~/.config/op-agent-engine/engine.env
chmod 600 ~/.config/op-agent-engine/engine.env
# edit workflow.yaml (project, repo, statuses) and engine.env (URLs, tokens)

# 3. Set up OpenProject (next section), then validate and provision.
source ~/.local/share/op-agent-engine/venv/bin/activate
set -a; . ~/.config/op-agent-engine/engine.env; set +a
op-agent-engine check-config --config ~/.config/op-agent-engine/workflow.yaml
op-agent-engine provision --config ~/.config/op-agent-engine/workflow.yaml
op-agent-engine provision --apply --config ~/.config/op-agent-engine/workflow.yaml

# 4. Inspect the dispatch plan without writing anything.
op-agent-engine dry-run --config ~/.config/op-agent-engine/workflow.yaml

# 5. Start the service (production) or run one poll (development).
op-agent-engine once --config ~/.config/op-agent-engine/workflow.yaml
```

## OpenProject setup from an empty instance

Do the steps below once. Names are matched **case-sensitively** by
`check-config`; either create exactly these names or change `workflow.yaml` to
match the instance (`In progress` vs `In Progress` is a common difference).

### 1. Statuses

**Administration → Work packages → Statuses.** Ensure these seven exist (or
configure your own names under `openproject.statuses`):

| Engine key | Default name | Notes |
|---|---|---|
| `new` | `New` | candidates for implementation |
| `in_progress` | `In Progress` | claimed by an implementer |
| `ready_for_review` | `Ready for Review` | implementation finished |
| `in_testing` | `In Testing` | claimed by `codeReviewer` |
| `on_hold` | `On Hold` | terminal for the engine; a human decides what next |
| `rejected` | `Rejected` | reviewer requested changes |
| `closed` | `Closed` | merged and done |

### 2. Types

The default `openproject.eligible_types` is `Task`, `Bug`, `Feature`. Enable
them in the project, or list the instance's actual type names. `check-config`
resolves each eligible type per project.

### 3. The `AGENT` custom field

**Administration → Custom fields → Work packages → New custom field → List.**
Name it `AGENT`, add options for every agent key in `workflow.yaml`, and enable
it for the eligible types and the monitored projects.

For the example configuration the options are:

```
FRONTEND
BACKEND
LEAD_DESIGNER
REVIEWER
```

`REVIEWER` is not used for routing (`Ready for Review` always goes to the
reviewer), but startup resolves an option for **every** configured agent key,
so it must exist. Ticket routing uses `FRONTEND`, `BACKEND` or
`LEAD_DESIGNER`. `check-config` prints the resolved field, for example
`agent field: AGENT -> customField3`, and fails if an option is missing.

### 4. Roles and permissions

Create exactly two project roles in **Administration → Users and permissions →
Roles and permissions**. All four agent users share `Agent Worker`;
`agent-engine` gets `Agent Engine`. The full click-through is in
[`docs/OPENPROJECT_ADMIN_SETUP.md`](docs/OPENPROJECT_ADMIN_SETUP.md).

| Role | Permissions | Workflow transitions |
|---|---|---|
| `Agent Worker` | view work packages, add comments, become assignee | **none** |
| `Agent Engine` | view/edit work packages, change status, add comments | the engine transitions below |

Allowing no transitions for agents enforces "the engine owns transitions" at
the permission level. OpenProject 17 exposes roles read-only through the API
(`POST /api/v3/roles` returns 404 and permissions are not returned), so this
step is manual by design.

### 5. Workflow transitions

**Administration → Workflows.** For each eligible type (`Task`, `Bug`,
`Feature`) and the `Agent Engine` role, allow:

| From | To |
|---|---|
| `New` | `In Progress`, `On Hold` |
| `In Progress` | `Ready for Review`, `On Hold` |
| `Ready for Review` | `In Testing`, `On Hold` |
| `In Testing` | `Closed`, `Rejected`, `On Hold` |
| `Rejected` | `In Progress`, `On Hold` |

For the same types and the `Agent Worker` role, leave every transition
unchecked. `On Hold` is terminal for the engine, so no transitions out of it
are needed.

Some instances do not enforce workflows server-side (see
`docs/VERIFICATION.md` section 9); the engine still enforces them client-side.

### 6. Users, tokens and the engine identity

`provision --apply` creates the five users and their project memberships:

```
frontendEngineer, backendEngineer, leadDesigner, codeReviewer, agent-engine
```

It never deletes, deactivates or locks a user, never changes an existing login,
and updates only a differing display name/email. API tokens cannot be minted
through the API: sign in as each user and open **My account → Access tokens →
API → Add token**. Put them in `engine.env`:

| Variable | Owner | Notes |
|---|---|---|
| `OP_ENGINE_TOKEN` | `agent-engine` | runtime token; used for all polling and transitions |
| `OP_PROVISION_TOKEN` | an administrator | only `provision` and `check-config`; falls back to `OP_ENGINE_TOKEN` only if that token is an admin |
| `OP_TOKEN_FRONTEND` | `frontendEngineer` | passed to the agent through the runner/agent `env` |
| `OP_TOKEN_BACKEND` | `backendEngineer` | same |
| `OP_TOKEN_DESIGNER` | `leadDesigner` | same |
| `OP_TOKEN_REVIEWER` | `codeReviewer` | same |

The runtime token needs the global `manage_user` permission (or administrator)
because startup resolves agent logins through `GET /api/v3/users`. This is
broader than the transition-only role; keep the token on the service host only.
`check-config` calls `/api/v3/users/me` for every configured agent token and
fails if a token belongs to a different login. `provision` reports which tokens
are still missing.

## Configure the engine

Two files and the process environment drive everything. Secrets live only in
`engine.env` (mode `0600`); `workflow.yaml` references them as `${VAR}`.

### `workflow.yaml`

```yaml
openproject:
  statuses:            # exact, case-sensitive OpenProject names
    new: "New"
    in_progress: "In Progress"
    ready_for_review: "Ready for Review"
    in_testing: "In Testing"
    on_hold: "On Hold"
    rejected: "Rejected"
    closed: "Closed"
  agent_field: "AGENT"          # resolved to customField<N> at startup
  engine_user: "agent-engine"   # login used for transitions and comments
  provisioning:                 # only provision/check-config read this
    enabled: true
    agent_role: "Agent Worker"
    engine_role: "Agent Engine"
    email_domain: "agent-engine.local"
  dispatch_priority: [ready_for_review, rejected, new]
  eligible_types: ["Task", "Bug", "Feature"]
  skip_work_packages_with_children: true
  skip_if_assigned_to_human: true

projects:
  - identifier: "example-project"        # OpenProject project identifier
    repo:
      github: "owner/example-project"
      dir: "example-project"   # clone dir under <workspace>/repositories/
      default_branch: "main"
    enabled: true

agents:
  FRONTEND:                       # routing key = AGENT custom field option
    name: frontendEngineer
    kind: implementer             # exactly one agent must be 'reviewer'
    op_login: "frontendEngineer"
    workspace: "frontendEngineer" # directory under WORKSPACE_ROOT
    projects: ["*"]               # ["*"] = every enabled project
    runner: pi
    model: null                   # optional, substituted as {model}
    timeout_seconds: 3600         # optional per-agent timeout
    env:
      # The OpenProject CLI (`op`) reads OP_CLI_HOST and OP_CLI_TOKEN.
      OP_CLI_HOST: "${OP_BASE_URL}"
      OP_CLI_TOKEN: "${OP_TOKEN_FRONTEND}"

runners:
  pi:
    argv: ["pi", "-p", "--approve", "@{prompt_file}", "Follow the instructions in the attached prompt file."]
    cwd: "{repo_dir}"
    env: {}
    inject_standard: false        # pi discovers AGENTS.md from parent dirs
```

Field notes:

- `agents` keys are the `AGENT` custom field option names. Values
  `FRONTEND`, `BACKEND`, `LEAD_DESIGNER` route work; `REVIEWER` exists only to
  satisfy option resolution.
- `projects` on an agent limits the projects the engine may dispatch to it.
  `["*"]` expands to every project with `enabled: true`; an explicit list may
  only name declared projects. A ticket routed to an out-of-scope agent is
  skipped with a warning and left unmodified.
- `runners` are generic command templates. Adding an agent CLI means adding a
  `runners` entry; no code change. Placeholders usable in `argv`, `cwd`,
  `stdin_file` and `env`: `{prompt_file}`, `{repo_dir}`, `{workspace_dir}`,
  `{run_dir}`, `{model}`, `{branch}`, `{work_package_id}`.
- `inject_standard: true` prepends the workspace `AGENTS.md` to the prompt.
  Leave it `false` for pi (it discovers parent-directory standards itself) and
  `true` for CLIs that do not.
- `${VAR}` is resolved from the environment before validation; a missing
  variable is a startup error.
- The verified command shapes are `pi -p --approve @{prompt_file} "..."` and
  `opencode run --auto --file {prompt_file} "..."`. Re-check with `--help`
  after upgrading a CLI.

### Environment variables

| Variable | Default | Purpose |
|---|---|---|
| `OP_BASE_URL` | required | OpenProject base URL |
| `OP_ENGINE_TOKEN` | required | API token of the `agent-engine` user; runtime only |
| `OP_PROVISION_TOKEN` | optional | admin token for `provision`/`check-config` |
| `POLL_INTERVAL_SECONDS` | `60` | poll interval |
| `MAX_CONCURRENT_RUNS` | `1` | global cap on simultaneous runs |
| `MAX_REJECTIONS` | `3` | rejections applied before `On Hold` |
| `MAX_CRASH_RETRIES` | `2` | failed/timeout retries before `On Hold` |
| `RUN_TIMEOUT_SECONDS` | `3600` | default per-run timeout; an agent may override |
| `STOP_GRACE_SECONDS` | `60` | wait for running agents on shutdown |
| `RUN_RETENTION_DAYS` | `30` | `cleanup` removes older run directories |
| `ENGINE_CONFIG` | `~/.config/op-agent-engine/workflow.yaml` | workflow file |
| `DATA_DIR` | `~/.local/share/op-agent-engine` | SQLite, run dirs, heartbeat, lock |
| `WORKSPACE_ROOT` | required | absolute path to the checkout's `agents/` directory |
| `GH_TOKEN` | optional | overrides the host's `gh`/git authentication; normally unset |
| `ENGINE_PAUSED` | `false` | stop new dispatches; running runs finish |
| `LOG_LEVEL` | `INFO` | log level |

Per-agent tokens are referenced from `workflow.yaml` by name, not fixed
environment names.

### CLI

| Command | Purpose |
|---|---|
| `run` | long-running service loop |
| `once` | one poll; claims and executes within the slot rules |
| `dry-run` | one poll; logs the dispatch plan and writes nothing |
| `check-config` | validate the file and resolve IDs (`--offline`, `--json`) |
| `status` | heartbeat age, pause state, active and last 10 runs (`--json`) |
| `provision` | reconcile users and scoped memberships; dry-run unless `--apply` |
| `cleanup` | remove `DATA_DIR/runs/<id>` older than the retention period (`--days`, `--dry-run`, `--json`) |

`--config` and `--log-level` work before or after the subcommand.

### Validate

```bash
set -a; . ~/.config/op-agent-engine/engine.env; set +a
op-agent-engine check-config --config ~/.config/op-agent-engine/workflow.yaml
```

`check-config` validates the whole file (interpolation, runner references,
placeholder names, unique workspaces, exactly one reviewer) and then resolves
every status, user, project, eligible type, role and `AGENT` option. It prints
each resolved ID, exits 1 on the first problem, and never prints token values.
Use `--offline` to validate the file without contacting OpenProject.

## Provision users and project scope

```bash
set -a; . ~/.config/op-agent-engine/engine.env; set +a
op-agent-engine provision --config ~/.config/op-agent-engine/workflow.yaml          # plan
op-agent-engine provision --apply --config ~/.config/op-agent-engine/workflow.yaml  # write
```

- Creates missing users, never changes a login, never deletes/deactivates.
- Creates or corrects one membership per agent per scoped project, and the
  engine membership. A human-added membership is never touched.
- `["*"]` expands to enabled projects only; an explicit list must name declared
  projects (configuration error otherwise).
- Roles cannot be created through the API, so `provision` requires the two
  roles to exist and fails with a specific message when one is missing. It
  prints a manual-verification note for role permissions.
- A second `--apply` run performs no writes. `--dry-run` (the default) writes
  nothing.

## Install as a systemd service

Use a dedicated checkout of this repository, kept on `main`, separate from any
development clone. `WORKSPACE_ROOT` points at its `agents/` directory; the live
agents read `agents/<workspace>/AGENTS.md` from that checkout.

```bash
# 1. Install the engine and its Python dependencies as the service user.
git clone <engine-repo> ~/op-agent-engine
cd ~/op-agent-engine
python3.12 -m venv ~/.local/share/op-agent-engine/venv
~/.local/share/op-agent-engine/venv/bin/pip install -e .

# 2. Configuration and secrets.
mkdir -p ~/.config/op-agent-engine
cp config/workflow.example.yaml ~/.config/op-agent-engine/workflow.yaml
cp deploy/engine.env.example ~/.config/op-agent-engine/engine.env
chmod 700 ~/.config/op-agent-engine
chmod 600 ~/.config/op-agent-engine/engine.env
# edit both files: project/repo, status names, WORKSPACE_ROOT, tokens

# 3. Validate and provision before enabling the loop.
source ~/.local/share/op-agent-engine/venv/bin/activate
set -a; . ~/.config/op-agent-engine/engine.env; set +a
op-agent-engine check-config --config ~/.config/op-agent-engine/workflow.yaml
op-agent-engine provision --apply --config ~/.config/op-agent-engine/workflow.yaml
op-agent-engine dry-run --config ~/.config/op-agent-engine/workflow.yaml

# 4. Install the unit and the retention timer (replace <host-user>).
sudo sed "s/<host-user>/$USER/g" deploy/op-agent-engine.service \
  | sudo tee /etc/systemd/system/op-agent-engine.service >/dev/null
sudo sed "s/<host-user>/$USER/g" deploy/op-agent-engine-cleanup.service \
  | sudo tee /etc/systemd/system/op-agent-engine-cleanup.service >/dev/null
sudo sed "s/<host-user>/$USER/g" deploy/op-agent-engine-cleanup.timer \
  | sudo tee /etc/systemd/system/op-agent-engine-cleanup.timer >/dev/null
sudo systemctl daemon-reload
systemd-analyze verify /etc/systemd/system/op-agent-engine.service

# 5. Start and follow logs.
sudo systemctl enable --now op-agent-engine
sudo systemctl enable --now op-agent-engine-cleanup.timer
journalctl -u op-agent-engine -f
```

The unit's `PATH` must contain every agent CLI and `gh`/`git`; systemd does not
read shell profiles. Verified reference locations are `pi` in
`~/.pi/agent/bin` (or `~/.local/bin/pi`) and `opencode` in `~/.opencode/bin`.
`HOME` comes from `User=`, so `~/.pi`, `~/.gitconfig` and `gh` credentials
resolve as in a normal login. `KillMode=control-group` ensures a stop kills the
whole agent process tree; `TimeoutStopSec=90` exceeds `STOP_GRACE_SECONDS` plus
the runner's 20 second kill grace.

Optional watchdog: uncomment `Type=notify` and `WatchdogSec=` (three poll
intervals) in the unit. The engine sends `READY=1` after startup resolution and
`WATCHDOG=1` after every poll when `NOTIFY_SOCKET` is present.

Update a deployment with:

```bash
cd ~/op-agent-engine && git pull --ff-only
sudo systemctl restart op-agent-engine
```

The checkout's checked-out branch is the live standard for agents: changing it
changes what the agents read.

## Runbook

Commands below assume the service venv is active
(`source ~/.local/share/op-agent-engine/venv/bin/activate`) or use the full
path `~/.local/share/op-agent-engine/venv/bin/op-agent-engine`.

### Status and heartbeat

```bash
op-agent-engine status
op-agent-engine status --json
```

The heartbeat file (`DATA_DIR/heartbeat`) is written after every poll. A stale
heartbeat means the service is stopped or stuck; check
`journalctl -u op-agent-engine --since '10 min ago'`.

### Pause and resume

- Create an empty `DATA_DIR/PAUSE` file: the next poll stops claiming. Running
  runs finish. Delete the file to resume. No restart needed.
- Set `ENGINE_PAUSED=true` in `engine.env` and restart for a persistent pause.
- Per project, set `enabled: false`; per ticket, move it to `On Hold` (the
  engine ignores `On Hold` in every poll).

### Inspect a run

Every run has `DATA_DIR/runs/<runId>/` with `prompt.txt`, `stdout.log`,
`stderr.log` and `meta.json`. `meta.json` records the plan it executed, state,
outcome, transition, exit code and a **redacted** environment snapshot.

```bash
op-agent-engine status --json          # find the run id and outcome
RID=42
less ~/.local/share/op-agent-engine/runs/$RID/meta.json
tail -n 80 ~/.local/share/op-agent-engine/runs/$RID/stdout.log
journalctl -u op-agent-engine --since today | grep "\"run_id\": $RID"
```

Run history is in SQLite (`DATA_DIR/engine.db`). The database is disposable:
deleting it loses history and crash counters but never corrupts the workflow.
A quick query without the `sqlite3` CLI:

```bash
python3 - <<'PY'
import os, sqlite3
path = os.path.expanduser("~/.local/share/op-agent-engine/engine.db")
conn = sqlite3.connect(path)
for row in conn.execute(
    "SELECT id, work_package_id, agent, kind, state, outcome, finished_at "
    "FROM runs ORDER BY id DESC LIMIT 20"
):
    print(row)
PY
```

### Handle `On Hold`

Read the latest comments on the ticket. The engine comment names the reason:

| Comment | Meaning | Human action |
|---|---|---|
| `ENGINE: blocked` | agent reported a blocker | remove the blocker, then move the ticket to `Rejected` (fix pass) or `New` |
| `ENGINE: no PR found` | `Ready for Review` but no PR for `op-<project>-<wp>` | create/push the PR, then move back to `Ready for Review` |
| `ENGINE: escalated` | crash/timeout retry cap reached; comment contains exit code and last 40 log lines | inspect the run directory, fix the cause, move the ticket back to `In Progress`/`New` |
| `ENGINE: rejection cap reached` | next rejection would exceed `MAX_REJECTIONS` | review the feedback yourself, then move to `On Hold` -> `Rejected`/`New` when ready |

The engine ignores `On Hold`, so moving a ticket out of it is always an
explicit human decision. Crash counters persist in `DATA_DIR/engine.db`; after
fixing a failure, clear the ticket's counter if you want the full retry budget
again:

```bash
python3 - <<'PY'
import os, sqlite3
path = os.path.expanduser("~/.local/share/op-agent-engine/engine.db")
conn = sqlite3.connect(path)
wp_id = 123                      # ticket id from the URL
conn.execute("DELETE FROM crash_counts WHERE work_package_id = ?", (wp_id,))
conn.commit()
PY
```

### Recovery and shutdown

On startup the engine takes the exclusive `DATA_DIR/engine.lock`, marks every
`running` row `interrupted` (without counting it as a crash), kills leftover
agent process groups only when the recorded process start time still matches
(PID-reuse safe), and recovers `In Progress` / `In Testing` tickets as `resume`
/ re-review runs. A clean `SIGTERM` stops polling and dispatching, waits
`STOP_GRACE_SECONDS`, then terminates the remaining groups and marks them
`interrupted` for the next start.

### Retention job

Run directories are pruned by the daily timer
(`deploy/op-agent-engine-cleanup.timer`) or manually:

```bash
op-agent-engine cleanup --dry-run        # show what would be removed
op-agent-engine cleanup                  # remove directories older than RUN_RETENTION_DAYS
op-agent-engine cleanup --days 7 --json  # ad-hoc retention / machine-readable report
```

Only numeric directories under `DATA_DIR/runs` are touched; SQLite history is
kept. Without systemd, add a cron entry:

```cron
15 3 * * * . $HOME/.config/op-agent-engine/engine.env; $HOME/.local/share/op-agent-engine/venv/bin/op-agent-engine cleanup >> $HOME/.local/share/op-agent-engine/cleanup.log 2>&1
```

### Secret handling

`OP_*` and `GH_TOKEN` values are registered at logging setup and replaced with
`***` in log events and extra fields. The `meta.json` environment snapshot is
redacted the same way. Never paste tokens into ticket or PR text (that content
is forwarded to agents as untrusted prompt text), and keep `engine.env` at mode
`0600`.

## Acceptance test with a fake agent

### Automated end-to-end (no OpenProject or GitHub needed)

```bash
.venv/bin/pytest tests/test_e2e.py
```

This test drives one ticket from `New` to `Ready for Review` to `Closed` in two
polls using the real runner to spawn
[`tests/fakes/fake_agent.sh`](tests/fakes/fake_agent.sh), with fake OpenProject
and GitHub backends. It also asserts that a configured `GH_TOKEN` never appears
in the run directory or SQLite history.

### Exercise the fake agent through a runner

Point a runner at the script to smoke-test prompt rendering, process handling
and result parsing without a real agent:

```yaml
runners:
  fake:
    argv: ["/bin/sh", "/home/<host-user>/op-agent-engine/tests/fakes/fake_agent.sh", "{prompt_file}"]
    cwd: "{repo_dir}"
    env:
      FAKE_AGENT_BEHAVIOR: "blocked"   # or leave unset: prompt-based approve/pr_opened
    inject_standard: false
```

The script does not push commits, open PRs or post OpenProject comments, so
the external verifier will reject an unbacked success. Use the automated test
for a full cycle, or back the run with a real branch/PR when testing live.

### Manual acceptance checklist (real instance, throwaway repo)

- [ ] Follow [OpenProject setup](#openproject-setup-from-an-empty-instance):
      statuses, types, `AGENT` options, both roles, workflow transitions.
- [ ] `check-config` resolves every status, user, project, type, role and
      `AGENT` option.
- [ ] `provision --apply` creates the five users and one membership per agent
      per scoped project; a second run makes no changes.
- [ ] `dry-run` logs a plausible plan and writes nothing.
- [ ] Create a `New` ticket with `AGENT=FRONTEND`: `once` claims it (`In
      Progress`), the agent pushes `op-<project>-<wp>`, and the ticket moves to
      `Ready for Review` only after an open PR exists.
- [ ] The reviewer run claims it (`In Testing`); an approving/merged review
      moves it to `Closed`.
- [ ] A review with a reviewer ticket comment moves it to `Rejected`; the fix
      pass updates the same branch/PR. The fourth rejection moves it to
      `On Hold` with `ENGINE: rejection cap reached`.
- [ ] A missing PR on `Ready for Review` produces `On Hold` and
      `ENGINE: no PR found`.
- [ ] Kill the engine mid-run and restart: the ticket resumes from `In
      Progress` / `In Testing`; the interrupted run does not consume a crash
      retry.
- [ ] `PAUSE` stops new claims; deleting it resumes. `status` shows heartbeat
      age, active runs and recent outcomes.
- [ ] `journalctl -u op-agent-engine` and `DATA_DIR/runs/*` contain no token
      value.

## Security notes

- Ticket descriptions, comments and PR content are untrusted input. The engine
  never executes them; they only appear inside prompt text.
- Agents run with the privileges of the service user. Stricter systemd
  sandboxing (`ProtectSystem=strict`, `ProtectHome`) can break agent toolchains;
  a dedicated Unix user is the recommended later hardening step.
- Branch protection: the reference target repository is private on a plan where
  the protection API is unavailable (`HTTP 403`), so the same account can merge
  its own PR. If branch protection is enabled later, require a dedicated
  reviewer account via the reviewer agent's `env` `GH_TOKEN`; otherwise the
  reviewer's merge is blocked by its own approving review.
- The runtime token is broader than the role (`manage_user` for user lookup).
  Treat it as a host secret; do not share it with agents. Agents get their own
  `OP_TOKEN_*` values only.
- Deleting `DATA_DIR/engine.db` sacrifices crash counters and history, not
  workflow integrity: OpenProject remains the system of record.

## Development

```bash
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
.venv/bin/pytest
.venv/bin/ruff check .
.venv/bin/mypy
```

Layout (see `docs/PLAN.md` section 17 for the full tree): `src/op_agent_engine/`
(client, resolution, poller, eligibility, scheduler, dispatcher, workspace,
runner, outcomes, recovery, provisioning, retention), `prompts/*.j2`,
`config/workflow.example.yaml`, `deploy/`, `tests/` with fakes.

Never modify `agents/*/AGENTS.md` from this engine; they are the worker
standards and are version-controlled separately from the engine code.

## Verified external behavior

Facts confirmed against real tooling are recorded in
[`docs/VERIFICATION.md`](docs/VERIFICATION.md): pi/OpenCode non-interactive
flags, the OpenProject CLI environment variables, API endpoint/filter shapes and
optimistic locking, `gh` PR detection, activity/journal parsing, rejection
counting, process identity and shutdown, user/role/membership API limits, and
the M9 systemd/systemd-unit/AGENTS.md/branch-protection checks.

Deployment items that still need a human on the target host:

- Replace `<host-user>` placeholders and install the unit (requires sudo).
- Mint the five API tokens in the OpenProject UI and set `OP_TOKEN_*`.
- Create the `AGENT` custom field and options (verified live filters wait for
  it) and align live status names if they differ in case.
- Create the two roles manually and check their permission sets in the UI.
- If the target repositories contain their own `AGENTS.md` (the reference
  repository does), confirm the workspace standard takes precedence in worker
  behavior; the prompt always names `agents/<workspace>/AGENTS.md`.
