"""Startup resolution of OpenProject IDs referenced by ``workflow.yaml``.

Everything is resolved once at startup and fails fast: statuses, agent users,
the engine user, projects, eligible project types, and the ``AGENT`` custom
field with its option IDs.

Verified API details (see ``docs/VERIFICATION.md``):

* Statuses: ``GET /api/v3/statuses`` (paginated).
* Users: ``GET /api/v3/users`` with a ``login`` filter. Requires the global
  ``manage_user`` permission (or administrator).
* Projects: ``GET /api/v3/projects/{identifier}`` accepts the identifier.
* Project types: ``GET /api/v3/projects/{id}/types`` (paginated).
* Schema: ``GET /api/v3/work_packages/schemas/{projectId}-{typeId}`` returns a
  single object. A list custom field appears as ``customField<N>`` with
  ``name`` and ``_links.allowedValues`` entries carrying ``href`` and ``title``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

from op_agent_engine.config import StatusNames, WorkflowConfig
from op_agent_engine.openproject.client import (
    JSON,
    OpenProjectAuthError,
    OpenProjectClient,
    OpenProjectError,
    OpenProjectNotFoundError,
    json_dumps,
)

_CUSTOM_FIELD_KEY = re.compile(r"^customField(\d+)$")
_OPTION_ID_FROM_HREF = re.compile(r"/(\d+)/?$")


class OpenProjectResolutionError(OpenProjectError):
    """A configured OpenProject entity could not be resolved."""


# ---------------------------------------------------------------------------
# Resolved models
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ResolvedStatus:
    key: str
    name: str
    id: int


@dataclass(frozen=True)
class ResolvedUser:
    login: str
    id: int
    name: str


@dataclass(frozen=True)
class ResolvedType:
    name: str
    id: int


@dataclass(frozen=True)
class ResolvedProject:
    identifier: str
    id: int
    name: str
    types: dict[str, ResolvedType]


@dataclass(frozen=True)
class ResolvedAgentField:
    name: str
    field_id: str
    options: dict[str, int]


@dataclass(frozen=True)
class OpenProjectResolution:
    statuses: dict[str, ResolvedStatus]
    users: dict[str, ResolvedUser]
    engine_user: ResolvedUser
    projects: dict[str, ResolvedProject]
    agent_field: ResolvedAgentField
    warnings: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, JSON]:
        return {
            "statuses": {
                key: {"id": status.id, "name": status.name}
                for key, status in self.statuses.items()
            },
            "users": {
                login: {"id": user.id, "name": user.name}
                for login, user in self.users.items()
            },
            "engine_user": {
                "id": self.engine_user.id,
                "login": self.engine_user.login,
                "name": self.engine_user.name,
            },
            "projects": {
                identifier: {
                    "id": project.id,
                    "name": project.name,
                    "types": {
                        name: resolved_type.id
                        for name, resolved_type in project.types.items()
                    },
                }
                for identifier, project in self.projects.items()
            },
            "agent_field": {
                "name": self.agent_field.name,
                "field_id": self.agent_field.field_id,
                "options": dict(sorted(self.agent_field.options.items())),
            },
            "warnings": list(self.warnings),
        }

    def summary(self) -> list[str]:
        lines = ["statuses:"]
        for key, status in self.statuses.items():
            lines.append(f"  {key}: {status.name} (id {status.id})")

        lines.append("users:")
        for login, user in self.users.items():
            lines.append(f"  {login}: {user.name} (id {user.id})")
        lines.append(
            f"  engine user: {self.engine_user.login} -> id {self.engine_user.id}"
        )

        lines.append("projects:")
        for identifier, project in self.projects.items():
            lines.append(f"  {identifier}: {project.name} (id {project.id})")
            types = ", ".join(
                f"{resolved_type.name} (id {resolved_type.id})"
                for resolved_type in project.types.values()
            )
            lines.append(f"    types: {types}")

        field = self.agent_field
        lines.append(f"agent field: {field.name} -> {field.field_id}")
        for value, option_id in sorted(field.options.items()):
            lines.append(f"  {value}: option id {option_id}")

        if self.warnings:
            lines.append("warnings:")
            for warning in self.warnings:
                lines.append(f"  - {warning}")
        return lines


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------


async def resolve_workflow(
    client: OpenProjectClient,
    config: WorkflowConfig,
) -> OpenProjectResolution:
    """Resolve every OpenProject ID referenced by ``config``.

    Raises :class:`OpenProjectResolutionError` listing every missing item.
    """
    statuses = await _resolve_statuses(client, config.openproject.statuses)
    users = await _resolve_users(
        client,
        sorted(
            {agent.op_login for agent in config.agents.values()}
            | {config.openproject.engine_user}
        ),
    )
    engine_user = users[config.openproject.engine_user]

    projects: dict[str, ResolvedProject] = {}
    warnings: list[str] = []
    for project_config in config.projects:
        project = await _resolve_project(client, project_config.identifier)
        types, type_warnings = await _resolve_types(
            client,
            project.id,
            project.identifier,
            config.openproject.eligible_types,
        )
        warnings.extend(type_warnings)
        projects[project.identifier] = ResolvedProject(
            identifier=project.identifier,
            id=project.id,
            name=project.name,
            types=types,
        )

    agent_field = await _resolve_agent_field(client, config, projects)

    return OpenProjectResolution(
        statuses=statuses,
        users=users,
        engine_user=engine_user,
        projects=projects,
        agent_field=agent_field,
        warnings=tuple(warnings),
    )


def _status_key(name: str) -> str:
    """Case- and whitespace-insensitive key for a status name.

    OpenProject's default statuses use sentence case (``In progress``) while
    operators commonly write title case (``In Progress``) in
    ``workflow.yaml``. Treat the two as the same name, but never guess when
    two statuses would collapse to the same key.
    """
    return " ".join(name.split()).casefold()


async def _resolve_statuses(
    client: OpenProjectClient, names: StatusNames
) -> dict[str, ResolvedStatus]:
    available: dict[str, tuple[int, str]] = {}
    by_key: dict[str, list[tuple[int, str]]] = {}
    async for element in client.iter_collection("/api/v3/statuses"):
        name = _require_str(element.get("name"), "status.name", "statuses")
        status_id = _require_int(element.get("id"), "status.id", f"status {name!r}")
        entry = (status_id, name)
        available[name] = entry
        by_key.setdefault(_status_key(name), []).append(entry)

    resolved: dict[str, ResolvedStatus] = {}
    missing: list[str] = []
    for key, configured_name in names.by_key().items():
        found = available.get(configured_name)
        if found is None:
            candidates = by_key.get(_status_key(configured_name), [])
            if len(candidates) == 1:
                found = candidates[0]
            elif len(candidates) > 1:
                matches = ", ".join(repr(name) for _, name in candidates)
                missing.append(
                    f"{key}='{configured_name}' (ambiguous: {matches})"
                )
                continue
        if found is None:
            missing.append(f"{key}='{configured_name}'")
            continue
        resolved[key] = ResolvedStatus(key=key, name=found[1], id=found[0])

    if missing:
        raise OpenProjectResolutionError(
            "OpenProject statuses not found: "
            + ", ".join(missing)
            + " (available: "
            + ", ".join(sorted(available))
            + ")"
        )
    return resolved


async def _resolve_users(
    client: OpenProjectClient, logins: list[str]
) -> dict[str, ResolvedUser]:
    filters = json_dumps([{"login": {"operator": "=", "values": logins}}])
    resolved: dict[str, ResolvedUser] = {}

    try:
        async for element in client.iter_collection(
            "/api/v3/users", params={"filters": filters}
        ):
            login = element.get("login")
            if not isinstance(login, str) or not login:
                continue
            resolved[login] = ResolvedUser(
                login=login,
                id=_require_int(element.get("id"), "user.id", f"user {login!r}"),
                name=_require_str(
                    element.get("name"), "user.name", f"user {login!r}"
                ),
            )
    except OpenProjectAuthError as error:
        if error.status_code == 403:
            hint = (
                "the engine token needs the global 'manage_user' permission "
                "(or administrator) to list users"
            )
        else:
            hint = "check that OP_ENGINE_TOKEN is valid"
        raise OpenProjectResolutionError(
            f"cannot resolve OpenProject users: {hint}"
        ) from error

    missing = sorted(set(logins) - set(resolved))
    if missing:
        raise OpenProjectResolutionError(
            "OpenProject users not found: "
            + ", ".join(missing)
            + " (resolved: "
            + (", ".join(sorted(resolved)) or "none")
            + ")"
        )
    return resolved


async def _resolve_project(
    client: OpenProjectClient, identifier: str
) -> ResolvedProject:
    path = f"/api/v3/projects/{quote(identifier, safe='')}"
    try:
        payload = await client.get_json(path)
    except OpenProjectNotFoundError as error:
        raise OpenProjectResolutionError(
            f"OpenProject project not found: '{identifier}'"
        ) from error

    if not isinstance(payload, dict):
        raise OpenProjectResolutionError(
            f"OpenProject project '{identifier}' returned a non-object response"
        )
    project_id = _require_int(payload.get("id"), "project.id", f"project {identifier!r}")
    name = _require_str(
        payload.get("name"), "project.name", f"project {identifier!r}"
    )
    return ResolvedProject(identifier=identifier, id=project_id, name=name, types={})


async def _resolve_types(
    client: OpenProjectClient,
    project_id: int,
    identifier: str,
    eligible_types: list[str],
) -> tuple[dict[str, ResolvedType], list[str]]:
    available: dict[str, ResolvedType] = {}
    async for element in client.iter_collection(
        f"/api/v3/projects/{project_id}/types"
    ):
        name = _require_str(
            element.get("name"), "type.name", f"project {identifier!r} types"
        )
        available[name] = ResolvedType(
            name=name,
            id=_require_int(
                element.get("id"), "type.id", f"project {identifier!r} type {name!r}"
            ),
        )

    selected = {
        name: available[name] for name in eligible_types if name in available
    }
    if not selected:
        raise OpenProjectResolutionError(
            f"OpenProject project '{identifier}' enables none of the eligible types "
            f"({', '.join(eligible_types)}); available: "
            + (", ".join(sorted(available)) or "none")
        )

    warnings = [
        f"project '{identifier}' does not enable eligible type '{name}'"
        for name in eligible_types
        if name not in available
    ]
    return selected, warnings


async def _resolve_agent_field(
    client: OpenProjectClient,
    config: WorkflowConfig,
    projects: dict[str, ResolvedProject],
) -> ResolvedAgentField:
    field_name = config.openproject.agent_field
    field_id: str | None = None
    option_ids: dict[str, int] = {}
    schema_options: dict[str, set[str]] = {}
    problems: list[str] = []

    for project in projects.values():
        for resolved_type in project.types.values():
            schema_label = f"project '{project.identifier}', type '{resolved_type.name}'"
            schema_identifier = f"{project.id}-{resolved_type.id}"
            payload = await client.get_json(
                f"/api/v3/work_packages/schemas/{schema_identifier}"
            )
            if not isinstance(payload, dict):
                raise OpenProjectResolutionError(
                    f"OpenProject schema {schema_identifier} ({schema_label}) "
                    "returned a non-object response"
                )

            matches = [
                (key, value)
                for key, value in payload.items()
                if _CUSTOM_FIELD_KEY.fullmatch(key)
                and isinstance(value, dict)
                and value.get("name") == field_name
            ]
            if not matches:
                problems.append(
                    f"custom field '{field_name}' not found in schema "
                    f"{schema_identifier} ({schema_label})"
                )
                continue
            if len(matches) > 1:
                problems.append(
                    f"custom field '{field_name}' appears more than once in schema "
                    f"{schema_identifier} ({schema_label})"
                )
                continue

            key, property_payload = matches[0]
            this_field_id = key
            if field_id is None:
                field_id = this_field_id
            elif this_field_id != field_id:
                problems.append(
                    f"custom field '{field_name}' resolves to {this_field_id} in schema "
                    f"{schema_identifier} but {field_id} elsewhere"
                )
                continue

            allowed_values = _allowed_values(property_payload)
            if allowed_values is None:
                problems.append(
                    f"custom field '{field_name}' in schema {schema_identifier} "
                    f"({schema_label}) is not a list field with allowed values"
                )
                continue

            values_here: set[str] = set()
            for option in allowed_values:
                if not isinstance(option, dict):
                    problems.append(
                        f"custom field '{field_name}' in schema {schema_identifier} "
                        "has a malformed allowed value"
                    )
                    continue
                value = option.get("title")
                if not isinstance(value, str) or not value:
                    value = option.get("value")
                if not isinstance(value, str) or not value:
                    problems.append(
                        f"custom field '{field_name}' in schema {schema_identifier} "
                        "has an allowed value without a title"
                    )
                    continue
                option_id = _option_id(option)
                if option_id is None:
                    problems.append(
                        f"custom field '{field_name}' option '{value}' in schema "
                        f"{schema_identifier} has no resolvable id"
                    )
                    continue
                prior = option_ids.get(value)
                if prior is not None and prior != option_id:
                    problems.append(
                        f"custom field '{field_name}' option '{value}' has id {option_id} "
                        f"in schema {schema_identifier} but {prior} elsewhere"
                    )
                    continue
                option_ids[value] = option_id
                values_here.add(value)

            schema_options[schema_identifier] = values_here

    if problems:
        raise OpenProjectResolutionError(
            "OpenProject custom field resolution failed:\n  - "
            + "\n  - ".join(problems)
        )
    if field_id is None:
        raise OpenProjectResolutionError(
            f"OpenProject custom field '{field_name}' could not be resolved"
        )

    for value in sorted(config.agents):
        if value not in option_ids:
            raise OpenProjectResolutionError(
                f"OpenProject custom field '{field_name}' has no option for agent "
                f"'{value}' (available: {', '.join(sorted(option_ids)) or 'none'})"
            )

    for schema_identifier, values_here in schema_options.items():
        missing = sorted(set(config.agents) - values_here)
        if missing:
            raise OpenProjectResolutionError(
                f"OpenProject schema {schema_identifier} does not offer custom field "
                f"option(s) for agent(s): {', '.join(missing)}"
            )

    return ResolvedAgentField(name=field_name, field_id=field_id, options=option_ids)


def _allowed_values(property_payload: dict[str, Any]) -> list[Any] | None:
    links = property_payload.get("_links")
    if not isinstance(links, dict):
        return None
    values = links.get("allowedValues")
    if not isinstance(values, list):
        return None
    return values


def _option_id(option: dict[str, Any]) -> int | None:
    direct = option.get("id")
    if isinstance(direct, int) and not isinstance(direct, bool):
        return direct

    href: Any = option.get("href")
    if not isinstance(href, str):
        links = option.get("_links")
        if isinstance(links, dict):
            self_link = links.get("self")
            if isinstance(self_link, dict):
                href = self_link.get("href")
    if not isinstance(href, str):
        return None
    match = _OPTION_ID_FROM_HREF.search(href)
    if match is None:
        return None
    return int(match.group(1))


def _require_int(value: Any, label: str, location: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise OpenProjectResolutionError(
            f"OpenProject response for {location} has no integer {label}"
        )
    return value


def _require_str(value: Any, label: str, location: str) -> str:
    if not isinstance(value, str) or not value:
        raise OpenProjectResolutionError(
            f"OpenProject response for {location} has no {label}"
        )
    return value
