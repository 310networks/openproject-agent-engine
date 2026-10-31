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

## 3. Project attribute (`GIT_REPO`)

The GitHub repository is a **project attribute** — OpenProject 14+ calls
project custom fields this, and the API still exposes them as
`customField<N>`. The engine reads it from `GET /api/v3/projects/{id}`, so it
is set **once per project**; there is nothing to select on each work package.

1. **Administration → Projects → Project attributes → + Add → Project
   attribute** (the API cannot create it).
2. **Name**: `GIT_REPO`. **Format**: **Text**. Save.
3. Make it available for **all projects**.
4. In each monitored project's settings, set `GIT_REPO` to its repository
   (`owner/name` or `https://github.com/owner/name`).

The name is matched exactly. If your attribute is named differently — for
example `GIT REPO` — set `OP_GIT_REPO_FIELD` to that exact name.

With `OP_PROJECT=*` the engine monitors every active project with a non-empty
`GIT_REPO`; explicitly named projects must have one set.

## 4. Work-package custom field (`AGENT`)

The routing agent is a **work-package** custom field (work-package custom
fields are still under Custom fields): **Administration → Custom fields → Work
packages → New custom field**, format **List** (single value), options
`FRONTEND`, `BACKEND`, `LEAD_DESIGNER`, `REVIEWER` (case-sensitive), choose
**For all projects** and all types. It must be available for `Task`, `Bug`,
`Feature`, otherwise startup fails with
`custom field 'AGENT' not found in schema <project>-<type>`. `REVIEWER` is
never used for routing but must exist as an option.

After adding a project or changing either the project attribute or the `AGENT`
field, restart the engine so it resolves them again:

```bash
docker compose restart engine
```

If you changed `docker/.env` instead, recreate the container:
`docker compose up -d engine`. `provision-users.sh` finishes with
`check-config`, which verifies both and one option id per agent.

## 5. Roles

Create one project role — **Administration → Users and permissions → Roles
and permissions** (the API is read-only for roles):

- **Agent Worker** (shared by all four agent users): *View work packages*,
  *Add comments*, *Become assignee/responsible*. No other write permissions —
  agents can never change status. Leave every workflow transition unchecked.

The `agent-engine` user is an **administrator** instead of a project role:
*Administration → Users → agent-engine → Administrator*. An administrator
has every project permission and can list users, so it needs no project role
and no project membership. `provision` creates only the agents'
memberships.

The engine and agent accounts, their tokens and `docker/.env` are created by
`./provision-users.sh`; see the [README quick start](../README.md#quick-start).
