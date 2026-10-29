# OpenProject setup (one-time)

Do this once per instance. `provision --apply` handles users and
memberships; the API cannot create roles, custom fields, statuses or
workflows, so the rest is manual in the UI. Role names must match
`openproject.provisioning` in `workflow.yaml`.

## 1. Statuses

**Administration → Work packages → Statuses** — ensure these seven exist
(case-sensitive; `check-config` resolves them):

| Key | Default name |
|---|---|
| `new` | `New` |
| `in_progress` | `In Progress` |
| `ready_for_review` | `Ready for Review` |
| `in_testing` | `In Testing` |
| `on_hold` | `On Hold` |
| `rejected` | `Rejected` |
| `closed` | `Closed` |

## 2. Types

Enable `Task`, `Bug`, `Feature` in the project (or set
`openproject.eligible_types` to the instance's actual names).

## 3. The `AGENT` custom field

**Administration → Custom fields → Work packages → New → List.** Name it
`AGENT`, add one option per agent key in `workflow.yaml` (default:
`FRONTEND`, `BACKEND`, `LEAD_DESIGNER`, `REVIEWER`), enable it for the
eligible types and the monitored projects. `REVIEWER` is never used for
routing but must exist: startup resolves one option per agent key.

## 4. Roles

Create exactly two project roles — **Administration → Users and permissions
→ Roles and permissions** (the API is read-only for roles):

- **Agent Worker** (shared by all four agent users): *View work packages*,
  *Add comments*, *Become assignee/responsible*. No other write permissions —
  agents can never change status.
- **Agent Engine** (the `agent-engine` user): *View work packages*, *Edit
  work packages*, *Change work package status*, *Add comments*.

## 5. Workflow transitions

**Administration → Workflows.** For each eligible type and the **Agent
Engine** role allow exactly:

| From | To |
|---|---|
| `New` | `In Progress`, `On Hold` |
| `In Progress` | `Ready for Review`, `On Hold` |
| `Ready for Review` | `In Testing`, `On Hold` |
| `In Testing` | `Closed`, `Rejected`, `On Hold` |
| `Rejected` | `In Progress`, `On Hold` |

For the **Agent Worker** role leave every transition unchecked. `On Hold` is
terminal for the engine. Some instances do not enforce workflows
server-side; the engine enforces them client-side either way.

## 6. Users and tokens

Only an admin token is needed before this step: set `OP_BASE_URL` and
`OP_PROVISION_TOKEN` in `engine.env`, then run
`op-agent-engine provision --apply`. It creates the five users
(`frontendEngineer`, `backendEngineer`, `leadDesigner`, `codeReviewer`,
`agent-engine`) and one membership per agent per scoped project. It never
deletes, deactivates or renames logins, and it exits successfully while the
agent tokens are still placeholders — it just lists the variables to fill in.

The users' passwords are randomly generated and never shown, so mint their
API tokens as an administrator: **Administration → Users → [user] → API
tokens** (one per user). Then put the values in `engine.env`:

| Variable | Owner |
|---|---|
| `OP_ENGINE_TOKEN` | `agent-engine` — runtime polling and transitions |
| `OP_PROVISION_TOKEN` | an administrator — `provision`/`check-config` only |
| `OP_TOKEN_FRONTEND` / `OP_TOKEN_BACKEND` / `OP_TOKEN_DESIGNER` / `OP_TOKEN_REVIEWER` | the four agent users — passed into agent runs |

`OP_ENGINE_TOKEN` needs `manage_user` (or admin) because startup resolves
agent logins via `GET /api/v3/users`; keep it on the service host only.
`check-config` verifies every token belongs to the right login.
