"""Tests for M8 agent provisioning, project scoping and token checks."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from op_agent_engine.config import ConfigError, WorkflowConfig
from op_agent_engine.openproject.client import OpenProjectClient
from op_agent_engine.provision import (
    ProvisioningError,
    TokenVerificationError,
    _create_user_payload,
    audit_workflow,
    check_agent_tokens,
    enforce_agent_tokens,
    provision_workflow,
    require_admin,
)
from tests.fakes.openproject import FakeOpenProject

BASE_URL = "https://op.example.com"
ADMIN_TOKEN = "admin-token"

PROJECTS: list[dict[str, Any]] = [
    {"id": 4, "identifier": "demo", "name": "Demo"},
    {"id": 5, "identifier": "old", "name": "Old", "active": False},
]

ROLES: list[dict[str, Any]] = [
    {"id": 5, "name": "Agent Worker"},
    {"id": 6, "name": "Agent Engine"},
    {"id": 3, "name": "Member"},
]

USERS: list[dict[str, Any]] = [
    {
        "id": 10,
        "login": "agent-engine",
        "name": "Agent Engine",
        "firstName": "Agent",
        "lastName": "Engine",
        "email": "agent-engine@agent-engine.local",
        "admin": True,
    },
    {
        "id": 11,
        "login": "impl",
        "name": "Impl",
        "firstName": "Impl",
        "lastName": "Agent",
        "email": "impl@agent-engine.local",
        "admin": False,
    },
    {
        "id": 12,
        "login": "reviewer",
        "name": "Reviewer",
        "firstName": "Reviewer",
        "lastName": "Agent",
        "email": "reviewer@agent-engine.local",
        "admin": False,
    },
]


def agent_config(
    key: str,
    *,
    kind: str = "implementer",
    projects: list[str] | None = None,
    env: dict[str, str] | None = None,
) -> dict[str, Any]:
    login = key.lower()
    return {
        "name": key,
        "kind": kind,
        "op_login": login,
        "workspace": key.lower(),
        "runner": "pi",
        "projects": projects if projects is not None else ["*"],
        "env": env or {},
    }


def workflow_mapping(
    *,
    agents: dict[str, Any] | None = None,
    projects: list[dict[str, Any]] | None = None,
    provisioning: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "openproject": {
            "engine_user": "agent-engine",
            "provisioning": provisioning
            or {
                "enabled": True,
                "agent_role": "Agent Worker",
                "engine_role": "Agent Engine",
                "email_domain": "agent-engine.local",
            },
        },
        "projects": projects
        or [
            {
                "identifier": "demo",
                "repo": {"github": "owner/demo", "dir": "demo"},
                "enabled": True,
            },
            {
                "identifier": "old",
                "repo": {"github": "owner/old", "dir": "old"},
                "enabled": False,
            },
        ],
        "agents": agents
        or {
            "FRONTEND": agent_config("FRONTEND"),
            "BACKEND": agent_config("BACKEND"),
            "DESIGNER": agent_config("DESIGNER"),
            "REVIEWER": agent_config("REVIEWER", kind="reviewer"),
        },
        "runners": {"pi": {"argv": ["pi", "-p", "@{prompt_file}"]}},
    }


def make_config(**overrides: Any) -> WorkflowConfig:
    return WorkflowConfig.from_mapping(workflow_mapping(**overrides), environ={})


def make_fake(**overrides: Any) -> FakeOpenProject:
    data: dict[str, Any] = {
        "statuses": [],
        "users": [],
        "projects": PROJECTS,
        "types": {},
        "schemas": {},
        "roles": ROLES,
        "memberships": [],
        "token_users": {ADMIN_TOKEN: 10},
    }
    data.update(overrides)
    return FakeOpenProject(**data)


def client_for(fake: FakeOpenProject, token: str = ADMIN_TOKEN) -> OpenProjectClient:
    return OpenProjectClient(BASE_URL, token, transport=fake.transport)


def writes(fake: FakeOpenProject) -> list[httpx.Request]:
    return [request for request in fake.requests if request.method != "GET"]


async def test_apply_creates_five_users_and_scoped_memberships_idempotently() -> None:
    fake = make_fake()
    config = make_config()
    client = client_for(fake)
    try:
        report = await provision_workflow(
            client, config, apply=True, environ={}, base_url=BASE_URL
        )
    finally:
        await client.aclose()

    assert report.ok
    assert report.writes == 10  # five users + four agents and the engine user
    created_logins = {user["login"] for user in fake.users}
    assert created_logins == {
        "frontend",
        "backend",
        "designer",
        "reviewer",
        "agent-engine",
    }
    assert len(fake.memberships) == 5
    engine_id = next(
        user["id"] for user in fake.users if user["login"] == "agent-engine"
    )
    membership_targets = {
        (
            membership["_links"]["principal"]["href"],
            membership["_links"]["project"]["href"],
        )
        for membership in fake.memberships
    }
    assert (f"/api/v3/users/{engine_id}", "/api/v3/projects/4") in {
        (principal, project)
        for principal, project in membership_targets
    }
    # The disabled project is never provisioned.
    assert all(
        membership["_links"]["project"]["href"] != "/api/v3/projects/5"
        for membership in fake.memberships
    )
    # Role sets are exactly the configured role.
    engine_href = f"/api/v3/users/{engine_id}"
    assert all(
        membership["_links"]["roles"] == [{"href": "/api/v3/roles/5"}]
        for membership in fake.memberships
        if membership["_links"]["principal"]["href"] != engine_href
    )
    assert all(
        membership["_links"]["roles"] == [{"href": "/api/v3/roles/6"}]
        for membership in fake.memberships
        if membership["_links"]["principal"]["href"] == engine_href
    )

    # A second run performs no writes at all.
    before = len(fake.requests)
    client = client_for(fake)
    try:
        second = await provision_workflow(
            client, config, apply=True, environ={}, base_url=BASE_URL
        )
    finally:
        await client.aclose()
    assert second.actions == []
    assert second.writes == 0
    assert all(request.method == "GET" for request in fake.requests[before:])
    assert len(fake.users) == 5
    assert len(fake.memberships) == 5


async def test_wildcard_expands_to_enabled_projects_only() -> None:
    fake = make_fake(users=USERS)
    config = make_config(
        agents={
            "FRONTEND": agent_config("FRONTEND", projects=["*"]),
            "REVIEWER": agent_config("REVIEWER", kind="reviewer", projects=["demo"]),
        }
    )
    client = client_for(fake)
    try:
        report = await provision_workflow(
            client, config, apply=True, environ={}, base_url=BASE_URL
        )
    finally:
        await client.aclose()

    assert report.ok
    project_hrefs = {
        membership["_links"]["project"]["href"] for membership in fake.memberships
    }
    assert project_hrefs == {"/api/v3/projects/4"}
    # Both agents and the engine user have the one enabled project.
    assert len(fake.memberships) == 3


async def test_dry_run_plans_without_writing() -> None:
    fake = make_fake()
    config = make_config()
    client = client_for(fake)
    try:
        report = await provision_workflow(
            client, config, apply=False, environ={}, base_url=BASE_URL
        )
    finally:
        await client.aclose()

    assert report.ok
    assert report.apply is False
    assert report.writes == 0
    assert report.actions
    assert all(not action.applied for action in report.actions)
    assert writes(fake) == []
    assert fake.users == []
    assert fake.memberships == []


async def test_missing_role_fails_apply_with_specific_message() -> None:
    fake = make_fake(roles=[{"id": 5, "name": "Agent Worker"}])
    config = make_config()
    client = client_for(fake)
    try:
        report = await audit_workflow(
            client, config, environ={}, base_url=BASE_URL
        )
        assert report.ok is False
        assert report.missing_roles == ["Agent Engine"]
        assert "roles API is read-only" in report.errors[0]
        with pytest.raises(ProvisioningError) as excinfo:
            await provision_workflow(
                client, config, apply=True, environ={}, base_url=BASE_URL
            )
    finally:
        await client.aclose()

    message = str(excinfo.value)
    assert "Agent Engine" in message
    assert "roles API is read-only" in message
    assert writes(fake) == []


async def test_existing_membership_with_wrong_role_is_updated() -> None:
    human_membership = {
        "id": 2,
        "_type": "Membership",
        "_links": {
            "self": {"href": "/api/v3/memberships/2"},
            "principal": {"href": "/api/v3/users/99"},
            "project": {"href": "/api/v3/projects/4"},
            "roles": [{"href": "/api/v3/roles/3"}],
        },
    }
    fake = make_fake(users=USERS, memberships=[
        {
            "id": 1,
            "_type": "Membership",
            "_links": {
                "self": {"href": "/api/v3/memberships/1"},
                "principal": {"href": "/api/v3/users/11"},
                "project": {"href": "/api/v3/projects/4"},
                "roles": [{"href": "/api/v3/roles/3"}],
            },
        },
        human_membership,
    ])
    config = make_config(
        agents={
            "IMPL": agent_config("IMPL", projects=["demo"]),
            "REVIEWER": agent_config("REVIEWER", kind="reviewer", projects=["demo"]),
        }
    )
    client = client_for(fake)
    try:
        report = await provision_workflow(
            client, config, apply=True, environ={}, base_url=BASE_URL
        )
    finally:
        await client.aclose()

    assert report.ok
    updates = [action for action in report.actions if action.action == "update_membership"]
    assert len(updates) == 1
    assert updates[0].target == "impl@demo"
    assert fake.memberships[0]["_links"]["roles"] == [{"href": "/api/v3/roles/5"}]
    # A human membership (not a configured login) is never touched.
    assert human_membership["_links"]["roles"] == [{"href": "/api/v3/roles/3"}]
    assert not any(
        request.method != "GET" and "/api/v3/memberships/2" in str(request.url)
        for request in fake.requests
    )


async def test_existing_user_name_and_email_are_updated_login_kept() -> None:
    existing = {
        "id": 11,
        "login": "impl",
        "name": "Old Name",
        "firstName": "Old",
        "lastName": "Name",
        "email": "old@example.com",
        "admin": False,
    }
    fake = make_fake(users=[existing, USERS[0]])
    config = make_config(
        agents={
            "IMPL": agent_config("IMPL", projects=["demo"]),
            "REVIEWER": agent_config("REVIEWER", kind="reviewer", projects=["demo"]),
        }
    )
    client = client_for(fake)
    try:
        report = await provision_workflow(
            client, config, apply=True, environ={}, base_url=BASE_URL
        )
    finally:
        await client.aclose()

    assert report.ok
    user = next(user for user in fake.users if user["id"] == 11)
    assert user["login"] == "impl"
    assert user["name"] == "Impl Agent"
    assert user["email"] == "impl@agent-engine.local"
    assert any(action.action == "update_user" for action in report.actions)


async def test_agent_token_login_mismatch_fails_and_missing_is_reported() -> None:
    fake = make_fake(
        users=USERS,
        token_users={
            ADMIN_TOKEN: 10,
            "wrong-token": 12,  # belongs to the reviewer login
        },
    )
    config = make_config(
        agents={
            "IMPL": agent_config(
                "IMPL", env={"OP_CLI_TOKEN": "wrong-token"}
            ),
            "REVIEWER": agent_config("REVIEWER", kind="reviewer", env={}),
        }
    )
    environ = {"OP_TOKEN_IMPL": "wrong-token"}
    report = await check_agent_tokens(
        BASE_URL, config, environ, transport=fake.transport
    )
    assert len(report.mismatches) == 1
    assert report.mismatches[0].login == "reviewer"
    assert "expected 'reviewer'" not in report.mismatches[0].message
    assert "expected 'impl'" in report.mismatches[0].message
    assert len(report.missing) == 1
    assert report.missing[0].agent_key == "REVIEWER"

    with pytest.raises(TokenVerificationError) as excinfo:
        await enforce_agent_tokens(
            BASE_URL, config, environ, transport=fake.transport
        )
    # The env var name is reported; the token value never is.
    assert "OP_TOKEN_IMPL" in str(excinfo.value)
    assert "wrong-token" not in str(excinfo.value)
    assert "impl" in str(excinfo.value)


async def test_matching_agent_token_is_ok() -> None:
    fake = make_fake(
        users=USERS,
        token_users={ADMIN_TOKEN: 10, "impl-token": 11},
    )
    config = make_config(
        agents={
            "IMPL": agent_config("IMPL", env={"OP_CLI_TOKEN": "impl-token"}),
            "REVIEWER": agent_config(
                "REVIEWER", kind="reviewer", env={"OP_CLI_TOKEN": "impl-token"}
            ),
        }
    )
    report = await check_agent_tokens(
        BASE_URL, config, {"OP_TOKEN_IMPL": "impl-token"}, transport=fake.transport
    )
    # REVIEWER uses the same token, which belongs to IMPL, so it is a mismatch.
    assert len(report.mismatches) == 1
    assert report.mismatches[0].agent_key == "REVIEWER"

    config = make_config(
        agents={
            "IMPL": agent_config("IMPL", env={"OP_CLI_TOKEN": "impl-token"}),
            "REVIEWER": agent_config("REVIEWER", kind="reviewer", env={}),
        }
    )
    report = await check_agent_tokens(
        BASE_URL, config, {"OP_TOKEN_IMPL": "impl-token"}, transport=fake.transport
    )
    assert report.mismatches == []


async def test_invalid_agent_token_is_not_silently_ignored() -> None:
    fake = make_fake(users=USERS, token_users={ADMIN_TOKEN: 10})
    config = make_config(
        agents={
            "IMPL": agent_config("IMPL", env={"OP_CLI_TOKEN": "bad-token"}),
            "REVIEWER": agent_config("REVIEWER", kind="reviewer", env={}),
        }
    )
    report = await check_agent_tokens(
        BASE_URL, config, {"OP_TOKEN_IMPL": "bad-token"}, transport=fake.transport
    )
    assert len(report.mismatches) == 1
    assert report.mismatches[0].login is None
    assert "rejected" in report.mismatches[0].message
    with pytest.raises(TokenVerificationError):
        await enforce_agent_tokens(
            BASE_URL, config, {"OP_TOKEN_IMPL": "bad-token"}, transport=fake.transport
        )


async def test_require_admin_rejects_non_admin_token() -> None:
    fake = make_fake(
        users=USERS,
        token_users={ADMIN_TOKEN: 10, "impl-token": 11},
    )
    client = client_for(fake, "impl-token")
    try:
        with pytest.raises(ProvisioningError) as excinfo:
            await require_admin(client)
    finally:
        await client.aclose()
    assert "not an administrator" in str(excinfo.value)


async def test_report_never_contains_tokens() -> None:
    fake = make_fake(
        users=USERS,
        token_users={ADMIN_TOKEN: 10, "impl-token": 11},
    )
    config = make_config(
        agents={
            "IMPL": agent_config("IMPL", env={"OP_CLI_TOKEN": "impl-token"}),
            "REVIEWER": agent_config("REVIEWER", kind="reviewer", env={}),
        }
    )
    client = client_for(fake)
    try:
        report = await provision_workflow(
            client,
            config,
            apply=True,
            environ={"OP_TOKEN_IMPL": "impl-token"},
            base_url=BASE_URL,
        )
    finally:
        await client.aclose()

    payload = json.dumps(report.as_dict())
    assert "impl-token" not in payload
    assert "admin-token" not in payload


def test_project_scope_identifier_must_be_declared() -> None:
    agents = {
        "IMPL": agent_config("IMPL", projects=["missing"]),
        "REVIEWER": agent_config("REVIEWER", kind="reviewer"),
    }
    with pytest.raises(ConfigError) as excinfo:
        make_config(agents=agents)
    assert "not declared in projects" in str(excinfo.value)


def test_wildcard_cannot_be_combined_with_explicit_projects() -> None:
    agents = {
        "IMPL": agent_config("IMPL", projects=["*", "demo"]),
        "REVIEWER": agent_config("REVIEWER", kind="reviewer"),
    }
    with pytest.raises(ConfigError) as excinfo:
        make_config(agents=agents)
    assert "cannot be combined" in str(excinfo.value)


def test_generated_passwords_satisfy_the_instance_policy() -> None:
    # OpenProject rejects users whose password lacks any of: lowercase,
    # uppercase, numeric or special (verified live: HTTP 422).
    seen: set[str] = set()
    for _ in range(50):
        password = _create_user_payload("u", "U", "u@example.com")["password"]
        assert any(c.islower() for c in password)
        assert any(c.isupper() for c in password)
        assert any(c.isdigit() for c in password)
        assert any(not c.isalnum() for c in password)
        assert len(password) >= 36
        seen.add(password)
    assert len(seen) == 50  # passwords are random, not constant
