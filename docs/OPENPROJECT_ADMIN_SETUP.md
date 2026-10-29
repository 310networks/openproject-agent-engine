# OpenProject admin setup (manual roles and workflows)

OpenProject 17's public API treats roles as read-only: `POST /api/v3/roles`
returns 404 and role permissions are not exposed. The `op` CLI has no role
commands either. The two roles, their permissions and the engine workflow
transitions therefore have to be created once in the OpenProject UI. Users,
memberships and project scopes are handled by `op-agent-engine provision`.

Do this once per OpenProject instance. Role names must match
`openproject.provisioning.agent_role` and `engine_role` in `workflow.yaml`
(the defaults are `Agent Worker` and `Agent Engine`).

Before the roles, create the `AGENT` list custom field described in the
[README](../README.md#3-the-agent-custom-field): `FRONTEND`, `BACKEND`,
`LEAD_DESIGNER` **and** `REVIEWER`. Startup resolves an option for every
configured agent key, so `REVIEWER` must exist even though `Ready for Review`
work packages always go to the reviewer and never use the field for routing.

Create **exactly two roles**, not one per agent. The four agent users
(`frontendEngineer`, `backendEngineer`, `leadDesigner`, `codeReviewer`) all
share the single `Agent Worker` role; `agent-engine` gets `Agent Engine`.
`provision --apply` creates the five users and one membership per user per
scoped project, so each project ends up with four `Agent Worker` memberships
and one `Agent Engine` membership.

## 1. Create the two project roles

Open **Administration → Users and permissions → Roles and permissions** and
click **New role**.

### Agent Worker

- Name: `Agent Worker`
- Under **Work packages and Gantt charts**, check only:
  - **View work packages**
  - **Add comments**
  - **Become assignee/responsible**
- Leave **Edit work packages**, **Change work package status** and every
  other write permission unchecked. This keeps status changes owned by the
  engine.
- Save.

### Agent Engine

- Name: `Agent Engine`
- Under **Work packages and Gantt charts**, check:
  - **View work packages**
  - **Edit work packages**
  - **Change work package status** (if your OpenProject version shows it)
  - **Add comments**
- Save.

Both roles are project roles; do not grant global administration permissions.

## 2. Configure workflow transitions

Open **Administration → Workflows**. For each eligible type (`Task`, `Bug`,
`Feature` — or whatever `openproject.eligible_types` contains), select the
type and the **Agent Engine** role and allow exactly these transitions:

| From | To |
|---|---|
| `New` | `In Progress`, `On Hold` |
| `In Progress` | `Ready for Review`, `On Hold` |
| `Ready for Review` | `In Testing`, `On Hold` |
| `In Testing` | `Closed`, `Rejected`, `On Hold` |
| `Rejected` | `In Progress`, `On Hold` |

Save each type.

Then select each type and the **Agent Worker** role and leave every
transition unchecked. A newly created role starts with no transitions; verify
this if the role was copied from another one.

`On Hold` is terminal for the engine (it ignores it), so no transitions out of
`On Hold` are needed.

## 3. Verify and finish provisioning

```bash
op-agent-engine check-config --config ~/.config/op-agent-engine/workflow.yaml
op-agent-engine provision --apply --config ~/.config/op-agent-engine/workflow.yaml
```

`check-config` should resolve both roles and report no missing roles.
`provision --apply` creates the five users and their scoped memberships.

Per-agent API tokens remain per-user self-service: sign in as each agent and
open **My account → Access tokens → API**, or let an administrator set a
password on the user and sign in as them. Put the tokens in `engine.env` as
`OP_TOKEN_FRONTEND`, `OP_TOKEN_BACKEND`, `OP_TOKEN_DESIGNER` and
`OP_TOKEN_REVIEWER`.
