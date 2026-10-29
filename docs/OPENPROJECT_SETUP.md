# OpenProject setup (one-time)

Do this once per instance. `provision --apply` handles users and
memberships; the API cannot create roles, custom fields or statuses, so the
rest is manual in the UI. The Agent Worker role name must match
`openproject.provisioning` in `workflow.yaml`.

## 1. Statuses

**Administration → Work packages → Statuses** — ensure these seven exist.
Names are matched case- and whitespace-insensitively, so OpenProject's
default sentence case (`In progress`) also matches the title case below:

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

Create one project role — **Administration → Users and permissions → Roles
and permissions** (the API is read-only for roles):

- **Agent Worker** (shared by all four agent users): *View work packages*,
  *Add comments*, *Become assignee/responsible*. No other write permissions —
  agents can never change status.

The `agent-engine` user is an **administrator** instead of a project role:
*Administration → Users → agent-engine → Administrator*. An administrator
has every project permission and can list users, so it needs no project role
and no project membership. `provision` creates only the agents'
memberships.

## 5. Workflow transitions

Nothing to configure for the engine user: it is an administrator and is not
restricted by project-role workflows. The engine enforces its own status
transitions client-side, and `On Hold` is terminal for it. For the **Agent
Worker** role leave every transition unchecked so agents cannot change
status.

## 6. Users and tokens

Only an admin token is needed before this step: set `OP_BASE_URL` and
`OP_PROVISION_TOKEN` in `engine.env`. `op-agent-engine provision --apply`
(run by the installer, or manually) creates the five users
(`frontendEngineer`, `backendEngineer`, `leadDesigner`, `codeReviewer`,
`agent-engine`) and one membership per agent per scoped project. The engine
user is created as a regular user; **make it an administrator** as described
in [section 4](#4-roles) before using its token. It never deletes,
deactivates or renames logins, and it exits successfully while the agent
tokens are still placeholders — it just lists the variables to fill in.

The users' passwords are randomly generated. Administrators cannot mint API
tokens for other users, so log in as each user and create the token under
**My account → Access tokens**.

The interactive installer (`./install.sh`, or `op-agent-engine setup`) shows
each newly created account's generated password at that user's token prompt,
asks for the token one at a time — the
`agent-engine` runtime token plus one per agent — verifies it against
`/api/v3/users/me`, and writes it to `engine.env` (mode `600`). If you install
manually, use `op-agent-engine provision --json` to obtain the generated
passwords, then put the token values in `engine.env` yourself. The passwords
are shown once and not saved, so record them if you may need to sign in
again; a later installer run cannot recover them for an existing account.

| Variable | Owner |
|---|---|
| `OP_ENGINE_TOKEN` | `agent-engine` — runtime polling and transitions |
| `OP_PROVISION_TOKEN` | an administrator — `provision`/`check-config` only |
| `OP_TOKEN_FRONTEND` / `OP_TOKEN_BACKEND` / `OP_TOKEN_DESIGNER` / `OP_TOKEN_REVIEWER` | the four agent users — passed into agent runs |

`OP_ENGINE_TOKEN` belongs to the administrator `agent-engine` user, which is
what lets startup list agent logins via `GET /api/v3/users`; keep it on the
service host only. Because that token is admin-capable, `OP_PROVISION_TOKEN`
may be omitted after setup and `provision`/`check-config` fall back to it.
`check-config` verifies every token belongs to the right login.
