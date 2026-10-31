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

The engine reads each work package's agent from a work-package custom field
named `AGENT` (configurable as `openproject.agent_field`). It must be a
single-select **List** field whose option titles match the `agents` keys in
`workflow.yaml`.

1. **Administration → Custom fields → Work packages → New custom field**.
2. **Name**: `AGENT`. **Format**: **List** (single value, not multi-select).
3. Add one option per agent key — default `FRONTEND`, `BACKEND`,
   `LEAD_DESIGNER`, `REVIEWER`. The titles are case-sensitive and must match
   the config keys exactly. Save.
4. Open the field's **Settings** tab and enable it for:
   - the eligible types (`Task`, `Bug`, `Feature` by default), and
   - the monitored project(s) (or all projects).

   `AGENT` must be enabled for every eligible type/project pair, otherwise
   startup fails with `custom field 'AGENT' not found in schema
   <project>-<type>`. `REVIEWER` is never used for routing but must exist as
   an option: startup resolves one option per agent key.
5. Verify with `op-agent-engine check-config`; it should resolve the field to
   a `customField<N>` id and list one option id per agent. `provision-users.sh`
   runs this check as its last step.

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
`OP_PROVISION_TOKEN` in `config/engine.env`. `op-agent-engine provision --apply`
(run by `provision-users.sh`, or manually) creates the five users
(`frontendEngineer`, `backendEngineer`, `leadDesigner`, `codeReviewer`,
`agent-engine`) and one membership per agent per scoped project. The engine
user is created as a regular user; **make it an administrator** as described
in [section 4](#4-roles) before using its token. It never deletes,
deactivates or renames logins, and it exits successfully while the agent
tokens are still placeholders — it just lists the variables to fill in.

The users' passwords are randomly generated. Administrators cannot mint API
tokens for other users, so log in as each user and create the token under
**My account → Access tokens**.

`./provision-users.sh` (in the deployment directory) shows each newly created
account's generated password at that user's token prompt, asks for the token one
at a time — the `agent-engine` runtime token plus one per agent — verifies it
against `/api/v3/users/me`, and writes it to `config/engine.env` (mode `600`).
The passwords are shown once and not saved, so record them if you may need to
sign in again; a later run cannot recover them for an existing account, so a
forgotten password has to be reset in **Administration → Users**.

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
