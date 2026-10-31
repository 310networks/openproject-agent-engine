# OpenProject setup (one-time)

Do this once per instance. `provision --apply` handles users and
memberships; the API cannot create roles, custom fields or statuses, so the
rest is manual in the UI. The Agent Worker role name must match `OP_AGENT_ROLE`
(default `Agent Worker`).

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

Enable `Task`, `Bug`, and `Feature` in the project. The engine's eligible types
are fixed to those three.

## 3. The `GIT_REPO` project custom field

The engine reads each project's GitHub repository from a **project** custom
field named `GIT_REPO` (configurable as `OP_GIT_REPO_FIELD`). The API cannot
create custom fields, so create this one in the UI once:

1. **Administration → Custom fields → Projects → New custom field**.
2. **Name**: `GIT_REPO`. **Format**: **Text** (a plain text field; a full
github.com URL also works).
3. Save, then enable it for every project the engine should monitor.
4. In each project's settings, set `GIT_REPO` to its repository
   (`owner/name` or `https://github.com/owner/name`).

With `OP_PROJECT=*`, the engine lists the active projects and monitors every
one that has a non-empty `GIT_REPO`. Projects without it are skipped. If you
name the project identifiers explicitly, each one must have `GIT_REPO` set.

After adding a project or changing its `GIT_REPO` / `AGENT` setup, restart the
engine so it resolves the project list again:

```bash
docker compose restart engine
```

If you changed `docker/.env` instead, recreate the container:
`docker compose up -d engine`.

## 4. The `AGENT` custom field

The engine reads each work package's agent from a work-package custom field
named `AGENT` (configurable as `OP_AGENT_FIELD`). It must be a single-select
**List** field whose option titles match the fixed agent keys.

1. **Administration → Custom fields → Work packages → New custom field**.
2. **Name**: `AGENT`. **Format**: **List** (single value, not multi-select).
3. Add one option per agent key — `FRONTEND`, `BACKEND`, `LEAD_DESIGNER`,
   `REVIEWER`. The titles are case-sensitive and must match exactly. Save.
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

## 5. Roles

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

## 6. Workflow transitions

Nothing to configure for the engine user: it is an administrator and is not
restricted by project-role workflows. The engine enforces its own status
transitions client-side, and `On Hold` is terminal for it. For the **Agent
Worker** role leave every transition unchecked so agents cannot change
status.

## 7. Users and tokens

Only an admin token is needed before this step: `./provision-users.sh` asks for
it at run time (it is never stored). `op-agent-engine provision --apply` (run
by the script, or manually with `OP_PROVISION_TOKEN` set) creates the five users
(`frontendEngineer`, `backendEngineer`, `leadDesigner`, `codeReviewer`,
`agent-engine`) and one membership per agent in the configured project. The
engine user is created as a regular user; **make it an administrator** as
described in [section 5](#5-roles) before using its token. Provisioning never
deletes, deactivates or renames logins.

The users' passwords are randomly generated. Administrators cannot mint API
tokens for other users, so log in as each user and create the token under
**My account → Access tokens**.

`./provision-users.sh` (in the deployment directory) shows each newly created
account's generated password at that user's token prompt, asks for the token one
at a time — the `agent-engine` runtime token plus one per agent — verifies it
against `/api/v3/users/me`, and writes it to `docker/.env` (mode `0600`).
The passwords are shown once and not saved, so record them if you may need to
sign in again; a later run cannot recover them for an existing account, so a
forgotten password has to be reset in **Administration → Users**.

| Variable | Owner |
|---|---|
| `OP_ENGINE_TOKEN` | `agent-engine` — runtime polling and transitions |
| `OP_PROVISION_TOKEN` | an administrator — `provision`/`check-config` only (asked for at run time, not stored) |
| `OP_TOKEN_FRONTEND` / `OP_TOKEN_BACKEND` / `OP_TOKEN_DESIGNER` / `OP_TOKEN_REVIEWER` | the four agent users — passed into agent runs |

`OP_ENGINE_TOKEN` belongs to the administrator `agent-engine` user, which is
what lets startup list agent logins via `GET /api/v3/users`; keep it on the
service host only. Because that token is admin-capable, `OP_PROVISION_TOKEN`
may be omitted after setup and `provision`/`check-config` fall back to it.
`check-config` verifies every token belongs to the right login.
