"""Agent provisioning (M8).

Reconciles the OpenProject identities, project memberships and per-agent
project scopes declared in ``workflow.yaml``. Only users and memberships are
written: statuses, types, workflow transitions and the ``AGENT`` custom field
remain manual administrator setup.

Verified API constraints (docs/VERIFICATION.md items 9-11):

* ``GET /api/v3/roles`` lists roles but the roles endpoint is read-only; a
  role cannot be created through the API. ``provision`` therefore requires the
  configured roles to exist and reports a specific error when one is missing.
* Role permissions are not exposed by the API; the administrator is assumed
  to have created the roles with the required permissions.
* Users are created with ``POST /api/v3/users`` (administrator or
  ``manage_user``); memberships with ``POST /api/v3/memberships`` and updated
  with ``PATCH /api/v3/memberships/{id}``.
* API tokens are minted in the OpenProject UI. ``check_agent_tokens`` only
  verifies that a configured token belongs to the expected login through
  ``GET /api/v3/users/me``.
"""

from __future__ import annotations

import secrets
import string
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote

from op_agent_engine.config import EngineSettings, WorkflowConfig
from op_agent_engine.openproject.client import (
    OpenProjectAuthError,
    OpenProjectClient,
    OpenProjectError,
    OpenProjectHTTPError,
    OpenProjectNotFoundError,
    json_dumps,
)

TOKEN_ENV_PREFIX = "OP_TOKEN_"


class ProvisioningError(OpenProjectError):
    """Provisioning cannot proceed (missing role, token, or API capability)."""


class TokenVerificationError(OpenProjectError):
    """A configured agent token belongs to a different login."""


# ---------------------------------------------------------------------------
# Report models
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ProvisionAction:
    """One reconciliation step (planned in dry-run, applied with ``--apply``)."""

    action: str  # create_user | update_user | create_membership | update_membership
    target: str
    detail: str
    applied: bool = False


@dataclass(frozen=True)
class TokenStatus:
    """The result of checking one agent's token."""

    agent_key: str
    env_var: str | None
    login: str | None
    ok: bool
    message: str


@dataclass(frozen=True)
class TokenReport:
    statuses: tuple[TokenStatus, ...] = ()

    @property
    def missing(self) -> list[TokenStatus]:
        return [status for status in self.statuses if status.login is None and status.ok]

    @property
    def mismatches(self) -> list[TokenStatus]:
        """Configured tokens that are invalid or belong to the wrong login."""
        return [status for status in self.statuses if not status.ok]

    def summary(self) -> list[str]:
        lines: list[str] = []
        for status in self.statuses:
            location = status.env_var or "<no token>"
            lines.append(
                f"  - {status.agent_key}: {location}: {status.message}"
            )
        return lines


@dataclass
class ProvisionReport:
    """The outcome of a provisioning or audit run."""

    apply: bool
    actions: list[ProvisionAction] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    missing_roles: list[str] = field(default_factory=list)
    tokens: TokenReport = field(default_factory=TokenReport)
    # Passwords generated for users created by this run, keyed by login. They
    # are surfaced to the installer so the operator can log in and mint the
    # user's API token; they never appear in ``summary()`` or logs.
    generated_passwords: dict[str, str] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.errors and not self.missing_roles

    @property
    def writes(self) -> int:
        return sum(1 for action in self.actions if action.applied)

    def add(
        self,
        action: str,
        target: str,
        detail: str,
        *,
        applied: bool = False,
    ) -> None:
        self.actions.append(
            ProvisionAction(action, target, detail, applied=applied)
        )

    def summary(self) -> list[str]:
        mode = "apply" if self.apply else "dry-run"
        lines = [f"provisioning ({mode}): {len(self.actions)} planned change(s)"]
        for action in self.actions:
            state = "applied" if action.applied else "planned"
            lines.append(
                f"  - {action.action} {action.target}: {action.detail} [{state}]"
            )
        if not self.actions:
            lines.append("  (no changes)")
        if self.missing_roles:
            lines.append("missing roles:")
            lines.extend(f"  - {role}" for role in self.missing_roles)
        if self.errors:
            lines.append("errors:")
            lines.extend(f"  - {error}" for error in self.errors)
        if self.warnings:
            lines.append("warnings:")
            lines.extend(f"  - {warning}" for warning in self.warnings)
        if self.tokens.statuses:
            lines.append("agent tokens:")
            lines.extend(self.tokens.summary())
        return lines

    def as_dict(self) -> dict[str, Any]:
        return {
            "apply": self.apply,
            "ok": self.ok,
            "writes": self.writes,
            "actions": [
                {
                    "action": action.action,
                    "target": action.target,
                    "detail": action.detail,
                    "applied": action.applied,
                }
                for action in self.actions
            ],
            "missing_roles": list(self.missing_roles),
            "errors": list(self.errors),
            "warnings": list(self.warnings),
            "passwords": dict(self.generated_passwords),
            "tokens": [
                {
                    "agent": status.agent_key,
                    "env_var": status.env_var,
                    "login": status.login,
                    "ok": status.ok,
                    "message": status.message,
                }
                for status in self.tokens.statuses
            ],
        }


# ---------------------------------------------------------------------------
# Provisioning token selection
# ---------------------------------------------------------------------------


def provisioning_token(settings: EngineSettings) -> str:
    """Return the token used by ``provision``/``check-config``.

    ``OP_PROVISION_TOKEN`` wins. ``OP_ENGINE_TOKEN`` is accepted only as a
    fallback; the caller verifies that it is an administrator token. Runtime
    dispatch never uses the provisioning token.
    """
    if settings.op_provision_token is not None:
        value = settings.op_provision_token.get_secret_value()
        if value:
            return value
    if settings.op_engine_token is not None:
        value = settings.op_engine_token.get_secret_value()
        if value:
            return value
    raise ProvisioningError(
        "provisioning requires OP_PROVISION_TOKEN or an administrator OP_ENGINE_TOKEN"
    )


async def require_admin(client: OpenProjectClient) -> str:
    """Verify the token belongs to an administrator and return its login."""
    try:
        payload = await client.get_json("/api/v3/users/me")
    except OpenProjectError as error:
        raise ProvisioningError(
            f"cannot read /api/v3/users/me with the provisioning token: {error}"
        ) from error
    if not isinstance(payload, dict):
        raise ProvisioningError("unexpected /api/v3/users/me response")
    login = payload.get("login")
    admin = payload.get("admin")
    if admin is not True:
        raise ProvisioningError(
            "the provisioning token is not an administrator token; set "
            "OP_PROVISION_TOKEN to an administrator token (OP_ENGINE_TOKEN is "
            "only a fallback when that token is an administrator)"
        )
    return login if isinstance(login, str) else "<unknown>"


# ---------------------------------------------------------------------------
# Reconciliation
# ---------------------------------------------------------------------------


async def provision_workflow(
    client: OpenProjectClient,
    config: WorkflowConfig,
    *,
    apply: bool,
    environ: Mapping[str, str] | None = None,
    base_url: str | None = None,
    transport: Any = None,
) -> ProvisionReport:
    """Create/update users and scoped memberships.

    ``apply=False`` is a read-only dry-run: no POST or PATCH is performed.
    Missing roles are a hard error before any write because the API cannot
    create roles.
    """
    if not config.openproject.provisioning.enabled:
        raise ProvisioningError(
            "provisioning is disabled in workflow.yaml "
            "(openproject.provisioning.enabled: false)"
        )

    report = ProvisionReport(apply=apply)
    roles = await _fetch_roles(client)
    role_ids, missing_roles = _resolve_roles(config, roles)
    report.missing_roles = missing_roles
    if missing_roles:
        report.errors.append(
            "roles must exist before provisioning; the OpenProject roles API is "
            "read-only, so create them manually: " + ", ".join(missing_roles)
        )
        if apply:
            raise ProvisioningError(
                "missing OpenProject role(s): "
                + ", ".join(missing_roles)
                + ". Create them in the OpenProject UI (the roles API is read-only)."
            )

    project_ids = await _resolve_project_ids(client, config)
    users = await _reconcile_users(client, config, report, apply=apply)

    if not missing_roles:
        await _reconcile_memberships(
            client,
            config,
            role_ids,
            project_ids,
            users,
            report,
            apply=apply,
        )

    environ = environ or {}
    report.tokens = await check_agent_tokens(
        base_url or client.base_url,
        config,
        environ,
        transport=transport,
    )
    return report


async def audit_workflow(
    client: OpenProjectClient,
    config: WorkflowConfig,
    *,
    environ: Mapping[str, str] | None = None,
    base_url: str | None = None,
    transport: Any = None,
) -> ProvisionReport:
    """Read-only provisioning audit used by ``check-config``."""
    report = ProvisionReport(apply=False)
    if not config.openproject.provisioning.enabled:
        report.warnings.append("provisioning is disabled in workflow.yaml")
        return report

    roles = await _fetch_roles(client)
    role_ids, missing_roles = _resolve_roles(config, roles)
    report.missing_roles = missing_roles
    if missing_roles:
        report.errors.append(
            "missing OpenProject role(s): "
            + ", ".join(missing_roles)
            + "; the roles API is read-only, so create them manually"
        )
    project_ids = await _resolve_project_ids(client, config)
    users = await _reconcile_users(client, config, report, apply=False)
    if not missing_roles:
        await _reconcile_memberships(
            client,
            config,
            role_ids,
            project_ids,
            users,
            report,
            apply=False,
        )

    environ = environ or {}
    report.tokens = await check_agent_tokens(
        base_url or client.base_url,
        config,
        environ,
        transport=transport,
    )
    return report


# -- roles and projects ------------------------------------------------------


async def _fetch_roles(client: OpenProjectClient) -> dict[str, int]:
    roles: dict[str, int] = {}
    async for element in client.iter_collection("/api/v3/roles"):
        name = element.get("name")
        role_id = element.get("id")
        if isinstance(name, str) and isinstance(role_id, int):
            roles[name] = role_id
    return roles


def _resolve_roles(
    config: WorkflowConfig, roles: Mapping[str, int]
) -> tuple[dict[str, int], list[str]]:
    provisioning = config.openproject.provisioning
    wanted = {"agent": provisioning.agent_role}
    if provisioning.engine_role:
        wanted["engine"] = provisioning.engine_role
    resolved: dict[str, int] = {}
    missing: list[str] = []
    for kind, name in wanted.items():
        role_id = roles.get(name)
        if role_id is None:
            missing.append(name)
        else:
            resolved[kind] = role_id
    return resolved, missing


async def _resolve_project_ids(
    client: OpenProjectClient, config: WorkflowConfig
) -> dict[str, int]:
    ids: dict[str, int] = {}
    for project in config.enabled_projects():
        path = f"/api/v3/projects/{quote(project.identifier, safe='')}"
        try:
            payload = await client.get_json(path)
        except OpenProjectNotFoundError as error:
            raise ProvisioningError(
                f"OpenProject project not found: '{project.identifier}'"
            ) from error
        if not isinstance(payload, dict) or not isinstance(payload.get("id"), int):
            raise ProvisioningError(
                f"OpenProject project '{project.identifier}' has no integer id"
            )
        ids[project.identifier] = payload["id"]
    return ids


# -- users -------------------------------------------------------------------


async def _reconcile_users(
    client: OpenProjectClient,
    config: WorkflowConfig,
    report: ProvisionReport,
    *,
    apply: bool,
) -> dict[str, dict[str, Any]]:
    """Ensure the four agent users and the engine user exist.

    Returns a login -> user payload mapping for the users that exist (after
    applying, when requested).
    """
    provisioning = config.openproject.provisioning
    targets: list[tuple[str, str, str]] = [
        (
            agent.op_login,
            _humanize(agent.name),
            f"{_email_local(agent.op_login)}@{provisioning.email_domain}",
        )
        for agent in config.agents.values()
    ]
    engine_login = config.openproject.engine_user
    targets.append(
        (
            engine_login,
            _humanize(engine_login),
            f"{_email_local(engine_login)}@{provisioning.email_domain}",
        )
    )

    users: dict[str, dict[str, Any]] = {}
    for login, display_name, email in targets:
        existing = await _find_user(client, login)
        if existing is None:
            password = _generate_password()
            payload = _create_user_payload(
                login, display_name, email, password=password
            )
            if apply:
                created = await client.post_json("/api/v3/users", json_body=payload)
                if not isinstance(created, dict) or not isinstance(
                    created.get("id"), int
                ):
                    raise ProvisioningError(
                        f"OpenProject did not return the created user for '{login}'"
                    )
                users[login] = created
                report.generated_passwords[login] = password
                report.add(
                    "create_user",
                    login,
                    f"created user {display_name} <{email}>",
                    applied=True,
                )
            else:
                report.add(
                    "create_user",
                    login,
                    f"would create user {display_name} <{email}>",
                )
            continue

        users[login] = existing
        current_name = existing.get("name")
        current_email = existing.get("email")
        first_name, last_name = _split_name(display_name)
        expected_name = f"{first_name} {last_name}"
        updates: dict[str, Any] = {}
        if isinstance(current_name, str) and current_name != expected_name:
            updates["firstName"] = first_name
            updates["lastName"] = last_name
        if isinstance(current_email, str) and current_email != email:
            updates["email"] = email
        if not updates:
            continue

        detail = ", ".join(sorted(updates))
        if apply:
            user_id = existing.get("id")
            if not isinstance(user_id, int):
                raise ProvisioningError(
                    f"OpenProject user '{login}' has no integer id"
                )
            updated = await client.patch_json(
                f"/api/v3/users/{user_id}", json_body=updates
            )
            if isinstance(updated, dict):
                users[login] = updated
            report.add(
                "update_user",
                login,
                f"updated {detail}",
                applied=True,
            )
        else:
            report.add("update_user", login, f"would update {detail}")
    return users


def _generate_password() -> str:
    """Generate a random password satisfying the instance password policy.

    OpenProject requires at least one lowercase, one uppercase, one numeric
    and one special character; ``secrets.token_urlsafe()`` does not guarantee
    that. The installer surfaces generated passwords so the operator can log
    in as each user and mint its API token.
    """
    special = "!%*+=?"
    required = (
        secrets.choice(string.ascii_lowercase),
        secrets.choice(string.ascii_uppercase),
        secrets.choice(string.digits),
        secrets.choice(special),
    )
    alphabet = string.ascii_letters + string.digits + special
    chars = list(required) + [secrets.choice(alphabet) for _ in range(32)]
    secrets.SystemRandom().shuffle(chars)
    return "".join(chars)


def _create_user_payload(
    login: str,
    display_name: str,
    email: str,
    *,
    password: str | None = None,
) -> dict[str, Any]:
    first_name, last_name = _split_name(display_name)
    # OpenProject requires a password for an active user; tokens are minted
    # separately in the UI. The generated password is returned to the installer
    # in ProvisionReport.generated_passwords, never in logs or summary().
    return {
        "login": login,
        "email": email,
        "firstName": first_name,
        "lastName": last_name,
        "admin": False,
        "status": "active",
        "language": "en",
        "password": password or _generate_password(),
    }


async def _find_user(
    client: OpenProjectClient, login: str
) -> dict[str, Any] | None:
    filters = json_dumps([{"login": {"operator": "=", "values": [login]}}])
    async for element in client.iter_collection(
        "/api/v3/users", params={"filters": filters}
    ):
        if element.get("login") == login:
            return element
    return None


def _humanize(value: str) -> str:
    """Turn ``frontendEngineer`` / ``agent-engine`` into a display name."""
    import re

    spaced = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", value)
    spaced = re.sub(r"[-_.]+", " ", spaced)
    return " ".join(part for part in spaced.split() if part).title()


def _split_name(display_name: str) -> tuple[str, str]:
    parts = [part for part in display_name.split() if part]
    if not parts:
        return "Agent", "Worker"
    if len(parts) == 1:
        return parts[0], "Agent"
    return parts[0], " ".join(parts[1:])


def _email_local(login: str) -> str:
    import re

    safe = re.sub(r"[^A-Za-z0-9._-]+", "-", login).strip("-")
    return safe or "agent"


# -- memberships -------------------------------------------------------------


async def _reconcile_memberships(
    client: OpenProjectClient,
    config: WorkflowConfig,
    role_ids: Mapping[str, int],
    project_ids: Mapping[str, int],
    users: Mapping[str, dict[str, Any]],
    report: ProvisionReport,
    *,
    apply: bool,
) -> None:
    targets: list[tuple[str, str, str]] = []
    for agent in config.agents.values():
        for project in config.projects_for_agent(agent):
            targets.append((agent.op_login, project.identifier, "agent"))
    if config.openproject.provisioning.engine_role:
        for project in config.enabled_projects():
            targets.append(
                (config.openproject.engine_user, project.identifier, "engine")
            )

    for login, identifier, role_kind in targets:
        role_id = role_ids[role_kind]
        user = users.get(login)
        user_id = user.get("id") if isinstance(user, dict) else None
        project_id = project_ids.get(identifier)
        target = f"{login}@{identifier}"

        if not isinstance(user_id, int):
            report.add(
                "create_membership",
                target,
                "would create membership after the user is created",
            )
            continue
        if project_id is None:
            report.errors.append(f"project '{identifier}' has no resolved id")
            continue

        existing = await _find_membership(client, user_id, project_id)
        if existing is None:
            body = {
                "_links": {
                    "principal": {"href": f"/api/v3/users/{user_id}"},
                    "project": {"href": f"/api/v3/projects/{project_id}"},
                    "roles": [{"href": f"/api/v3/roles/{role_id}"}],
                },
                "_meta": {"sendNotification": False},
            }
            if apply:
                await client.post_json("/api/v3/memberships", json_body=body)
                report.add(
                    "create_membership",
                    target,
                    f"created membership with role id {role_id}",
                    applied=True,
                )
            else:
                report.add(
                    "create_membership",
                    target,
                    f"would create membership with role id {role_id}",
                )
            continue

        current_roles = _membership_role_ids(existing)
        if role_id in current_roles:
            continue

        membership_id = existing.get("id")
        if not isinstance(membership_id, int):
            raise ProvisioningError(
                f"membership for {target} has no integer id"
            )
        body = {"_links": {"roles": [{"href": f"/api/v3/roles/{role_id}"}]}}
        if apply:
            await client.patch_json(
                f"/api/v3/memberships/{membership_id}", json_body=body
            )
            report.add(
                "update_membership",
                target,
                f"set role id {role_id}",
                applied=True,
            )
        else:
            report.add(
                "update_membership",
                target,
                f"would set role id {role_id}",
            )


async def _find_membership(
    client: OpenProjectClient, user_id: int, project_id: int
) -> dict[str, Any] | None:
    filters = json_dumps(
        [
            {"principal": {"operator": "=", "values": [str(user_id)]}},
            {"project": {"operator": "=", "values": [str(project_id)]}},
        ]
    )
    async for element in client.iter_collection(
        "/api/v3/memberships", params={"filters": filters}
    ):
        if (
            _href_id(element.get("_links", {}).get("principal")) == user_id
            and _href_id(element.get("_links", {}).get("project")) == project_id
        ):
            return element
    return None


def _membership_role_ids(membership: dict[str, Any]) -> set[int]:
    links = membership.get("_links")
    if not isinstance(links, dict):
        return set()
    roles = links.get("roles")
    if not isinstance(roles, list):
        return set()
    return {
        role_id
        for role in roles
        if (role_id := _href_id(role)) is not None
    }


def _href_id(link: Any) -> int | None:
    if not isinstance(link, dict):
        return None
    href = link.get("href")
    if not isinstance(href, str):
        return None
    try:
        return int(href.rsplit("/", 1)[-1])
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Agent token verification
# ---------------------------------------------------------------------------


async def check_agent_tokens(
    base_url: str,
    config: WorkflowConfig,
    environ: Mapping[str, str],
    *,
    transport: Any = None,
) -> TokenReport:
    """Verify every token used by an agent belongs to that agent's login.

    A missing token is reported (provisioning cannot mint tokens). A token
    whose ``/users/me`` login differs from the configured ``op_login`` is a
    mismatch and fails startup.
    """
    candidates = _agent_token_envs(config, environ)
    statuses: list[TokenStatus] = []
    cache: dict[str, tuple[str | None, str]] = {}

    for agent_key, agent in config.agents.items():
        env_names = candidates.get(agent_key, [])
        if not env_names:
            statuses.append(
                TokenStatus(
                    agent_key=agent_key,
                    env_var=None,
                    login=None,
                    ok=True,
                    message="no token configured; create one in the OpenProject UI",
                )
            )
            continue
        for env_name in env_names:
            if env_name not in cache:
                cache[env_name] = await _token_login(
                    base_url, environ[env_name], transport=transport
                )
            login, message = cache[env_name]
            ok = login is not None and login == agent.op_login
            if login is None:
                message = f"{env_name}: {message}"
            elif ok:
                message = f"{env_name}: login {login} matches"
            else:
                message = (
                    f"{env_name}: belongs to login {login!r}, expected "
                    f"{agent.op_login!r}"
                )
            statuses.append(
                TokenStatus(
                    agent_key=agent_key,
                    env_var=env_name,
                    login=login,
                    ok=ok,
                    message=message,
                )
            )

    return TokenReport(statuses=tuple(statuses))


async def enforce_agent_tokens(
    base_url: str,
    config: WorkflowConfig,
    environ: Mapping[str, str],
    *,
    transport: Any = None,
) -> TokenReport:
    """Check agent tokens and raise on a login mismatch (startup guard)."""
    report = await check_agent_tokens(
        base_url, config, environ, transport=transport
    )
    if report.mismatches:
        raise TokenVerificationError(
            "agent token/login mismatch: "
            + "; ".join(status.message for status in report.mismatches)
        )
    return report


def _agent_token_envs(
    config: WorkflowConfig, environ: Mapping[str, str]
) -> dict[str, list[str]]:
    """Map agent key -> environment variables whose value the agent uses.

    The token variable name is not part of the configuration contract (an
    operator may name it ``OP_TOKEN_DESIGNER`` for ``LEAD_DESIGNER``), so the
    mapping is inferred from the resolved per-agent ``env`` values.
    """
    token_values = {
        name: value
        for name, value in environ.items()
        if name.startswith(TOKEN_ENV_PREFIX) and value
    }
    mapping: dict[str, list[str]] = {}
    for agent_key, agent in config.agents.items():
        used = set(agent.env.values())
        names = sorted(
            name for name, value in token_values.items() if value in used
        )
        mapping[agent_key] = names
    return mapping


async def _token_login(
    base_url: str, token: str, *, transport: Any = None
) -> tuple[str | None, str]:
    if not token:
        return None, "token is empty"
    async with OpenProjectClient(
        base_url, token, transport=transport
    ) as client:
        try:
            payload = await client.get_json("/api/v3/users/me")
        except OpenProjectAuthError as error:
            return None, f"token rejected with HTTP {error.status_code}"
        except OpenProjectHTTPError as error:
            return None, f"GET /users/me failed with HTTP {error.status_code}"
        except OpenProjectError as error:
            return None, f"GET /users/me failed: {error}"
    if not isinstance(payload, dict):
        return None, "unexpected /users/me response"
    login = payload.get("login")
    if not isinstance(login, str) or not login:
        return None, "/users/me did not return a login"
    return login, "ok"


def missing_tokens(report: TokenReport) -> list[str]:
    return [status.agent_key for status in report.missing]
