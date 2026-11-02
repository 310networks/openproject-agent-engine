from __future__ import annotations

from typing import Any

import pytest

from op_agent_engine.config import WorkflowConfig
from op_agent_engine.openproject.client import OpenProjectClient
from op_agent_engine.openproject.resolve import (
    OpenProjectResolutionError,
    resolve_workflow,
)
from tests.fakes.openproject import FakeOpenProject

BASE_URL = "https://op.example.com"
TOKEN = "test-token"

STATUSES: list[dict[str, Any]] = [
    {"id": 1, "name": "New"},
    {"id": 2, "name": "In Progress"},
    {"id": 3, "name": "Ready for Review"},
    {"id": 4, "name": "In Testing"},
    {"id": 5, "name": "On Hold"},
    {"id": 6, "name": "Rejected"},
    {"id": 7, "name": "Closed"},
    {"id": 8, "name": "Blocked"},
]

USERS: list[dict[str, Any]] = [
    {"id": 10, "login": "agent-engine", "name": "Agent Engine"},
    {"id": 11, "login": "impl", "name": "Implementer"},
    {"id": 12, "login": "reviewer", "name": "Reviewer"},
]

PROJECTS: list[dict[str, Any]] = [
    {"id": 4, "identifier": "demo", "name": "Demo"},
]

TYPES: dict[str, list[dict[str, Any]]] = {
    "demo": [
        {"id": 1, "name": "Task"},
        {"id": 4, "name": "Feature"},
        {"id": 7, "name": "Bug"},
    ]
}


def schema(
    *,
    field_id: int = 3,
    options: tuple[tuple[str, int], ...] = (("IMPL", 11), ("REVIEWER", 22)),
) -> dict[str, Any]:
    key = f"customField{field_id}"
    return {
        "_type": "Schema",
        key: {
            "type": "CustomField",
            "name": "AGENT",
            "_links": {
                "allowedValues": [
                    {"href": f"/api/v3/custom_options/{option_id}", "title": value}
                    for value, option_id in options
                ]
            },
        },
    }


def schemas(**overrides: dict[str, Any]) -> dict[str, dict[str, Any]]:
    result = {
        "4-1": schema(),
        "4-4": schema(),
        "4-7": schema(),
    }
    result.update(overrides)
    return result


def workflow_mapping(**overrides: Any) -> dict[str, Any]:
    mapping: dict[str, Any] = {
        "openproject": {
            "eligible_types": ["Task", "Bug", "Feature"],
        },
        "projects": [
            {
                "identifier": "demo",
                "repo": {"github": "owner/demo", "dir": "demo"},
            }
        ],
        "agents": {
            "IMPL": {
                "name": "impl",
                "kind": "implementer",
                "op_login": "impl",
                "workspace": "impl",
                "runner": "pi",
            },
            "REVIEWER": {
                "name": "reviewer",
                "kind": "reviewer",
                "op_login": "reviewer",
                "workspace": "reviewer",
                "runner": "pi",
            },
        },
        "runners": {"pi": {"argv": ["pi", "-p", "@{prompt_file}"]}},
    }
    mapping.update(overrides)
    return mapping


def make_config(**overrides: Any) -> WorkflowConfig:
    return WorkflowConfig.from_mapping(workflow_mapping(**overrides), environ={})


def make_fake(**overrides: Any) -> FakeOpenProject:
    data: dict[str, Any] = {
        "statuses": STATUSES,
        "users": USERS,
        "projects": PROJECTS,
        "types": TYPES,
        "schemas": schemas(),
    }
    data.update(overrides)
    return FakeOpenProject(**data)


async def resolve(fake: FakeOpenProject, config: WorkflowConfig | None = None):
    client = OpenProjectClient(BASE_URL, TOKEN, transport=fake.transport)
    try:
        return await resolve_workflow(client, config or make_config())
    finally:
        await client.aclose()


async def test_resolves_all_ids() -> None:
    resolution = await resolve(make_fake())

    assert resolution.statuses["new"].id == 1
    assert resolution.statuses["in_testing"].id == 4
    assert resolution.statuses["closed"].id == 7
    assert resolution.statuses["blocked"].id == 8
    assert resolution.users["agent-engine"].id == 10
    assert resolution.engine_user.login == "agent-engine"
    assert resolution.projects["demo"].id == 4
    assert resolution.projects["demo"].types["Bug"].id == 7
    assert resolution.agent_field.field_id == "customField3"
    assert resolution.agent_field.options == {"IMPL": 11, "REVIEWER": 22}
    assert resolution.warnings == ()


async def test_missing_status_is_named() -> None:
    fake = make_fake(
        statuses=[status for status in STATUSES if status["name"] != "In Testing"]
    )

    with pytest.raises(OpenProjectResolutionError) as excinfo:
        await resolve(fake)

    message = str(excinfo.value)
    assert "OpenProject statuses not found" in message
    assert "in_testing='In Testing'" in message
    assert "available: Blocked, Closed, In Progress" in message


SENTENCE_STATUSES: list[dict[str, Any]] = [
    {"id": 1, "name": "New"},
    {"id": 2, "name": "In progress"},
    {"id": 3, "name": "Ready for Review"},
    {"id": 4, "name": "In testing"},
    {"id": 5, "name": "On hold"},
    {"id": 6, "name": "Rejected"},
    {"id": 7, "name": "Closed"},
    {"id": 8, "name": "blocked"},
]


async def test_status_names_match_case_insensitively() -> None:
    # OpenProject's default statuses are sentence case; the configuration commonly
    # uses title case. Both must resolve to the same status.
    resolution = await resolve(make_fake(statuses=SENTENCE_STATUSES))

    assert resolution.statuses["in_progress"].id == 2
    assert resolution.statuses["in_progress"].name == "In progress"
    assert resolution.statuses["in_testing"].name == "In testing"
    assert resolution.statuses["on_hold"].id == 5
    assert resolution.statuses["blocked"].id == 8


async def test_ambiguous_status_names_are_rejected() -> None:
    statuses = [
        status for status in STATUSES if status["name"] != "In Progress"
    ] + [
        {"id": 20, "name": "In progress"},
        {"id": 21, "name": "IN PROGRESS"},
    ]

    with pytest.raises(OpenProjectResolutionError) as excinfo:
        await resolve(make_fake(statuses=statuses))

    message = str(excinfo.value)
    assert "in_progress='In Progress'" in message
    assert "ambiguous" in message


async def test_missing_user_is_named() -> None:
    fake = make_fake(users=[user for user in USERS if user["login"] != "reviewer"])

    with pytest.raises(OpenProjectResolutionError) as excinfo:
        await resolve(fake)

    assert "OpenProject users not found: reviewer" in str(excinfo.value)


async def test_user_listing_permission_failure_has_actionable_hint() -> None:
    fake = make_fake(allow_user_listing=False)

    with pytest.raises(OpenProjectResolutionError) as excinfo:
        await resolve(fake)

    message = str(excinfo.value)
    assert "cannot resolve OpenProject users" in message
    assert "manage_user" in message


async def test_missing_project_is_named() -> None:
    fake = make_fake(projects=[])

    with pytest.raises(OpenProjectResolutionError) as excinfo:
        await resolve(fake)

    assert "OpenProject project not found: 'demo'" in str(excinfo.value)


async def test_missing_custom_field_names_project_and_type() -> None:
    fake = make_fake(schemas=schemas(**{"4-1": {"_type": "Schema"}}))

    with pytest.raises(OpenProjectResolutionError) as excinfo:
        await resolve(fake)

    message = str(excinfo.value)
    assert "custom field 'AGENT' not found in schema 4-1" in message
    assert "project 'demo', type 'Task'" in message


async def test_missing_agent_option_is_named() -> None:
    fake = make_fake(
        schemas=schemas(
            **{
                "4-1": schema(options=(("IMPL", 11),)),
                "4-4": schema(options=(("IMPL", 11),)),
                "4-7": schema(options=(("IMPL", 11),)),
            }
        )
    )

    with pytest.raises(OpenProjectResolutionError) as excinfo:
        await resolve(fake)

    message = str(excinfo.value)
    assert "has no option for agent 'REVIEWER'" in message
    assert "available: IMPL" in message


async def test_inconsistent_option_id_is_rejected() -> None:
    fake = make_fake(
        schemas=schemas(
            **{
                "4-1": schema(options=(("IMPL", 11), ("REVIEWER", 22))),
                "4-4": schema(options=(("IMPL", 99), ("REVIEWER", 22))),
                "4-7": schema(options=(("IMPL", 11), ("REVIEWER", 22))),
            }
        )
    )

    with pytest.raises(OpenProjectResolutionError) as excinfo:
        await resolve(fake)

    assert "option 'IMPL' has id 99" in str(excinfo.value)
    assert "but 11 elsewhere" in str(excinfo.value)


async def test_inconsistent_field_id_is_rejected() -> None:
    fake = make_fake(
        schemas=schemas(
            **{
                "4-1": schema(field_id=3),
                "4-4": schema(field_id=8),
                "4-7": schema(field_id=3),
            }
        )
    )

    with pytest.raises(OpenProjectResolutionError) as excinfo:
        await resolve(fake)

    assert "customField8" in str(excinfo.value)
    assert "customField3 elsewhere" in str(excinfo.value)


async def test_missing_eligible_type_is_a_warning() -> None:
    fake = make_fake(
        types={"demo": [{"id": 1, "name": "Task"}]},
        schemas={"4-1": schema()},
    )

    resolution = await resolve(fake)

    assert list(resolution.projects["demo"].types) == ["Task"]
    assert resolution.warnings == (
        "project 'demo' does not enable eligible type 'Bug'",
        "project 'demo' does not enable eligible type 'Feature'",
    )


async def test_project_with_no_eligible_types_fails() -> None:
    fake = make_fake(
        types={"demo": [{"id": 9, "name": "Milestone"}]},
        schemas={},
    )
    config = make_config(
        openproject={"eligible_types": ["Task", "Bug", "Feature"]}
    )

    with pytest.raises(OpenProjectResolutionError) as excinfo:
        await resolve(fake, config)

    assert "enables none of the eligible types" in str(excinfo.value)
    assert "available: Milestone" in str(excinfo.value)


async def test_disabled_project_is_still_validated() -> None:
    mapping = workflow_mapping()
    mapping["projects"].append(
        {
            "identifier": "disabled-demo",
            "repo": {"github": "owner/disabled-demo", "dir": "disabled-demo"},
            "enabled": False,
        }
    )
    config = WorkflowConfig.from_mapping(mapping, environ={})

    with pytest.raises(OpenProjectResolutionError) as excinfo:
        await resolve(make_fake(), config)

    assert "OpenProject project not found: 'disabled-demo'" in str(excinfo.value)


async def test_resolution_summary_lists_ids() -> None:
    summary = "\n".join((await resolve(make_fake())).summary())

    assert "in_testing: In Testing (id 4)" in summary
    assert "agent-engine: Agent Engine (id 10)" in summary
    assert "demo: Demo (id 4)" in summary
    assert "Task (id 1)" in summary
    assert "agent field: AGENT -> customField3" in summary
    assert "IMPL: option id 11" in summary

async def test_users_are_paginated_without_losing_the_filter() -> None:
    fake = make_fake(max_page_size=1)

    resolution = await resolve(fake)

    assert set(resolution.users) == {"agent-engine", "impl", "reviewer"}
    user_requests = [request for request in fake.requests if request.url.path == "/api/v3/users"]
    assert len(user_requests) == 3
    assert all("filters" in request.url.params for request in user_requests)


async def test_supports_custom_option_self_link_shape() -> None:
    def option(value: str, option_id: int) -> dict[str, Any]:
        return {
            "_type": "CustomOption",
            "value": value,
            "_links": {"self": {"href": f"/api/v3/custom_options/{option_id}"}},
        }

    links = [option("IMPL", 31), option("REVIEWER", 32)]
    schema_with_self_links = {
        "_type": "Schema",
        "customField9": {
            "type": "CustomOption",
            "name": "AGENT",
            "_links": {"allowedValues": links},
        },
    }
    fake = make_fake(
        schemas={
            "4-1": schema_with_self_links,
            "4-4": schema_with_self_links,
            "4-7": schema_with_self_links,
        }
    )

    resolution = await resolve(fake)

    assert resolution.agent_field.field_id == "customField9"
    assert resolution.agent_field.options == {"IMPL": 31, "REVIEWER": 32}



async def test_schema_is_read_from_the_form_endpoint() -> None:
    # OpenProject 17.x only embeds ``allowedValues`` in the form schema; the
    # plain schemas endpoint returns link properties with empty ``_links``.
    fake = make_fake()

    resolution = await resolve(fake)

    assert resolution.agent_field.options == {"IMPL": 11, "REVIEWER": 22}
    form_requests = [
        request
        for request in fake.requests
        if request.url.path == "/api/v3/work_packages/form"
    ]
    assert len(form_requests) == 3
    assert all(request.method == "POST" for request in form_requests)
    schema_requests = [
        request
        for request in fake.requests
        if request.url.path.startswith("/api/v3/work_packages/schemas/")
    ]
    assert schema_requests == []


async def test_falls_back_to_schema_endpoint_without_form_permission() -> None:
    fake = make_fake(form_error=(403, "You are not authorized to access this resource."))

    resolution = await resolve(fake)

    assert resolution.agent_field.options == {"IMPL": 11, "REVIEWER": 22}
    schema_requests = [
        request
        for request in fake.requests
        if request.url.path.startswith("/api/v3/work_packages/schemas/")
    ]
    assert len(schema_requests) == 3


async def test_link_field_without_allowed_values_is_reported() -> None:
    # Shape returned by GET /api/v3/work_packages/schemas/<id>-<type> on
    # OpenProject 17.x: the custom field is present but ``_links`` is empty.
    empty_links_schema = {
        "_type": "Schema",
        "customField3": {
            "type": "CustomOption",
            "name": "AGENT",
            "writable": True,
            "location": "_links",
            "_links": {},
        },
    }
    fake = make_fake(
        schemas={"4-1": empty_links_schema, "4-4": empty_links_schema, "4-7": empty_links_schema},
        form_error=(403, "You are not authorized to access this resource."),
    )

    with pytest.raises(OpenProjectResolutionError) as excinfo:
        await resolve(fake)

    message = str(excinfo.value)
    assert "is not a list field with allowed values" in message
    assert "returned no allowedValues" in message


# -- repository discovery (GIT_REPO project attribute) ----------------------


def project_with_repo(
    identifier: str,
    *,
    project_id: int = 4,
    repo: str | None = "acme/demo",
    active: bool = True,
) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "id": project_id,
        "identifier": identifier,
        "name": identifier.title(),
        "active": active,
    }
    if repo is not None:
        entry["customField5"] = repo
    return entry


REPO_FIELD = {"customField5": {"type": "String", "name": "GIT_REPO"}}


async def test_git_repo_field_sets_the_repository() -> None:
    fake = make_fake(
        projects=[project_with_repo("demo")],
        project_schema=REPO_FIELD,
    )
    config = make_config(projects=[{"identifier": "demo"}])

    await resolve(fake, config)

    assert config.projects[0].repo is not None
    assert config.projects[0].repo.github == "acme/demo"
    assert config.projects[0].repo.dir == "demo"
    assert config.projects[0].repo.default_branch is None


async def test_git_repo_name_matches_spaces_case_and_hyphens() -> None:
    # OpenProject's Project attributes UI commonly stores the display name
    # "GIT REPO", while the engine refers to GIT_REPO.
    fake = make_fake(
        projects=[project_with_repo("demo")],
        project_schema={"customField5": {"type": "String", "name": "GIT REPO"}},
    )
    config = make_config(projects=[{"identifier": "demo"}])

    await resolve(fake, config)

    assert config.projects[0].repo is not None
    assert config.projects[0].repo.github == "acme/demo"


async def test_ambiguous_git_repo_attribute_names_are_rejected() -> None:
    fake = make_fake(
        projects=[project_with_repo("demo")],
        project_schema={
            "customField5": {"type": "String", "name": "GIT-REPO"},
            "customField6": {"type": "String", "name": "GIT REPO"},
        },
    )
    config = make_config(projects=[{"identifier": "demo"}])

    with pytest.raises(OpenProjectResolutionError, match="ambiguous"):
        await resolve(fake, config)


async def test_git_repo_accepts_a_github_url() -> None:
    fake = make_fake(
        projects=[
            project_with_repo(
                "demo", repo="https://github.com/acme/demo.git"
            )
        ],
        project_schema=REPO_FIELD,
    )
    config = make_config(projects=[{"identifier": "demo"}])

    await resolve(fake, config)

    assert config.projects[0].repo is not None
    assert config.projects[0].repo.github == "acme/demo"


async def test_wildcard_discovers_active_projects_with_a_repo() -> None:
    fake = make_fake(
        projects=[
            project_with_repo("demo", project_id=4),
            project_with_repo("other", project_id=5, repo=None),
            project_with_repo("archived", project_id=6, repo="acme/old", active=False),
        ],
        project_schema=REPO_FIELD,
        types={"demo": TYPES["demo"]},
    )
    config = make_config(projects=[{"identifier": "*"}])

    await resolve(fake, config)

    assert [project.identifier for project in config.projects] == ["demo"]
    assert config.projects[0].repo is not None
    assert config.projects[0].repo.github == "acme/demo"


async def test_missing_git_repo_field_names_the_manual_setup() -> None:
    fake = make_fake(
        projects=[project_with_repo("demo")],
        project_schema={
            "customField7": {"type": "String", "name": "OTHER ATTRIBUTE"}
        },
    )
    config = make_config(projects=[{"identifier": "demo"}])

    with pytest.raises(OpenProjectResolutionError) as excinfo:
        await resolve(fake, config)

    message = str(excinfo.value)
    assert "project attribute 'GIT_REPO' not found" in message
    assert "'OTHER ATTRIBUTE' (customField7)" in message
    assert "Administration -> Projects -> Project attributes" in message


async def test_git_repo_must_be_a_github_repository() -> None:
    fake = make_fake(
        projects=[
            project_with_repo("demo", repo="https://gitlab.com/acme/demo")
        ],
        project_schema=REPO_FIELD,
    )
    config = make_config(projects=[{"identifier": "demo"}])

    with pytest.raises(OpenProjectResolutionError, match="not a GitHub repository"):
        await resolve(fake, config)


async def test_repo_directories_are_unique_across_projects() -> None:
    fake = make_fake(
        projects=[
            project_with_repo("demo", project_id=4, repo="acme/demo"),
            project_with_repo("other", project_id=5, repo="other/demo"),
        ],
        project_schema=REPO_FIELD,
        types={"demo": TYPES["demo"], "other": TYPES["demo"]},
        schemas={**schemas(), "5-1": schema(), "5-4": schema(), "5-7": schema()},
    )
    config = make_config(projects=[{"identifier": "*"}])

    await resolve(fake, config)

    assert [project.repo.dir for project in config.projects if project.repo] == [
        "demo",
        "demo-other",
    ]


