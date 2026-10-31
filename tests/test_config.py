from __future__ import annotations

import pytest
from pydantic import ValidationError

from op_agent_engine.config import (
    AGENT_TOKEN_VARS,
    ConfigError,
    EngineSettings,
    WorkflowConfig,
)

DEPLOYMENT_ENV = {
    "OP_BASE_URL": "https://openproject.example.com",
    "OP_PROJECT": "demo",
    "GITHUB_REPO": "owner/demo",
    "AGENT": "pi",
    "OP_TOKEN_FRONTEND": "frontend-token",
    "OP_TOKEN_BACKEND": "backend-token",
    "OP_TOKEN_DESIGNER": "designer-token",
    "OP_TOKEN_REVIEWER": "reviewer-token",
}


def base_mapping() -> dict[str, object]:
    return {
        "projects": [
            {
                "identifier": "demo",
                "repo": {"github": "owner/demo", "dir": "demo", "default_branch": "main"},
                "enabled": True,
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


def deployment_settings(**overrides: object) -> EngineSettings:
    values: dict[str, object] = {
        "op_base_url": "https://openproject.example.com",
        "op_project": "demo",
        "github_repo": "owner/demo",
        "agent": "pi",
    }
    values.update(overrides)
    return EngineSettings(**values)


def test_env_builds_the_whole_workflow() -> None:
    config = WorkflowConfig.from_settings(
        deployment_settings(), environ=dict(DEPLOYMENT_ENV)
    )

    assert [project.identifier for project in config.projects] == ["demo"]
    assert config.projects[0].repo.github == "owner/demo"
    # The clone directory defaults to the repository name.
    assert config.projects[0].repo.dir == "demo"
    assert set(config.agents) == {"FRONTEND", "BACKEND", "LEAD_DESIGNER", "REVIEWER"}
    assert config.reviewer.name == "codeReviewer"
    assert {agent.kind for agent in config.agents.values()} == {"implementer", "reviewer"}
    assert config.agents["FRONTEND"].env["OP_CLI_TOKEN"] == "frontend-token"
    assert config.agents["FRONTEND"].env["OP_CLI_HOST"] == DEPLOYMENT_ENV["OP_BASE_URL"]
    assert set(config.runners) == {"pi", "opencode"}


def test_env_summary_mentions_entities() -> None:
    config = WorkflowConfig.from_settings(
        deployment_settings(), environ=dict(DEPLOYMENT_ENV)
    )
    summary = "\n".join(config.summary())

    assert "statuses: new=New" in summary
    assert "demo: owner/demo -> demo [enabled]" in summary
    assert "REVIEWER: codeReviewer (reviewer)" in summary


def test_agent_variable_selects_the_runner() -> None:
    for agent in ("pi", "opencode"):
        config = WorkflowConfig.from_settings(
            deployment_settings(agent=agent),
            environ={**DEPLOYMENT_ENV, "AGENT": agent},
        )
        assert {item.runner for item in config.agents.values()} == {agent}


def test_repo_dir_and_branch_can_be_overridden() -> None:
    config = WorkflowConfig.from_settings(
        deployment_settings(github_repo_dir="clone", github_default_branch="trunk"),
        environ=dict(DEPLOYMENT_ENV),
    )

    assert config.projects[0].repo.dir == "clone"
    assert config.projects[0].repo.default_branch == "trunk"


def test_status_names_and_object_names_can_be_overridden() -> None:
    config = WorkflowConfig.from_settings(
        deployment_settings(
            op_status_in_progress="Working",
            op_agent_field="TEAM",
            op_engine_user="bot",
            op_agent_role="Worker",
            op_email_domain="bots.example.com",
        ),
        environ=dict(DEPLOYMENT_ENV),
    )

    assert config.openproject.statuses.in_progress == "Working"
    assert config.openproject.agent_field == "TEAM"
    assert config.openproject.engine_user == "bot"
    assert config.openproject.provisioning.agent_role == "Worker"
    assert config.openproject.provisioning.email_domain == "bots.example.com"


def test_model_is_applied_to_every_runner() -> None:
    config = WorkflowConfig.from_settings(
        deployment_settings(agent_model="anthropic/claude-sonnet-4-5"),
        environ=dict(DEPLOYMENT_ENV),
    )

    assert {runner.model for runner in config.runners.values()} == {
        "anthropic/claude-sonnet-4-5"
    }
    assert config.model_for("FRONTEND") == "anthropic/claude-sonnet-4-5"


def test_missing_required_values_are_reported() -> None:
    with pytest.raises(ConfigError) as excinfo:
        WorkflowConfig.from_settings(
            deployment_settings(op_project=None, github_repo=None),
            environ=dict(DEPLOYMENT_ENV),
        )

    message = str(excinfo.value)
    assert "missing required environment variables" in message
    assert "OP_PROJECT" in message
    assert "GITHUB_REPO" in message


def test_missing_token_is_empty_not_a_startup_error() -> None:
    # ``provision`` has to build the config before any token exists.
    environ = {key: value for key, value in DEPLOYMENT_ENV.items() if key != "OP_TOKEN_BACKEND"}
    config = WorkflowConfig.from_settings(deployment_settings(), environ=environ)

    assert config.agents["BACKEND"].env["OP_CLI_TOKEN"] == ""


def test_blank_engine_role_is_treated_as_none() -> None:
    raw = base_mapping()
    raw["openproject"] = {"provisioning": {"engine_role": "  "}}

    config = WorkflowConfig.from_mapping(raw, environ={})

    assert config.openproject.provisioning.engine_role is None


def test_engine_role_name_is_kept_for_a_least_privilege_engine() -> None:
    raw = base_mapping()
    raw["openproject"] = {"provisioning": {"engine_role": "Agent Engine"}}

    config = WorkflowConfig.from_mapping(raw, environ={})

    assert config.openproject.provisioning.engine_role == "Agent Engine"


def test_mapping_interpolates_environment_variables() -> None:
    raw = base_mapping()
    raw["projects"][0]["repo"]["github"] = "${REPO}"  # type: ignore[index]

    config = WorkflowConfig.from_mapping(raw, environ={"REPO": "owner/demo"})

    assert config.projects[0].repo.github == "owner/demo"


def test_missing_environment_variable_is_reported_with_location() -> None:
    raw = base_mapping()
    raw["projects"][0]["repo"]["github"] = "${REPO}"  # type: ignore[index]

    with pytest.raises(ConfigError) as excinfo:
        WorkflowConfig.from_mapping(raw, environ={})

    message = str(excinfo.value)
    assert "unresolved environment variables" in message
    assert "projects[0].repo.github" in message
    assert "REPO is not set" in message


def test_non_mapping_is_rejected() -> None:
    with pytest.raises(ConfigError, match="top level must be a mapping"):
        WorkflowConfig.from_mapping([], environ={})


def test_unknown_runner_is_rejected() -> None:
    raw = base_mapping()
    raw["agents"]["IMPL"]["runner"] = "nope"  # type: ignore[index]

    with pytest.raises(ConfigError) as excinfo:
        WorkflowConfig.from_mapping(raw, environ={})

    assert "undefined runners: nope" in str(excinfo.value)


def test_unknown_dispatch_priority_key_is_rejected() -> None:
    raw = base_mapping()
    raw["openproject"] = {"dispatch_priority": ["newish"]}

    with pytest.raises(ConfigError) as excinfo:
        WorkflowConfig.from_mapping(raw, environ={})

    message = str(excinfo.value)
    assert "dispatch_priority" in message
    assert "newish" in message


def test_unknown_runner_placeholder_is_rejected() -> None:
    raw = base_mapping()
    raw["runners"]["pi"]["argv"] = ["pi", "@{prompt_fiel}"]  # type: ignore[index]

    with pytest.raises(ConfigError) as excinfo:
        WorkflowConfig.from_mapping(raw, environ={})

    message = str(excinfo.value)
    assert "unknown placeholder '{prompt_fiel}'" in message
    assert "prompt_file" in message


def test_exactly_one_reviewer_is_required() -> None:
    raw = base_mapping()
    raw["agents"]["IMPL"]["kind"] = "reviewer"  # type: ignore[index]

    with pytest.raises(ConfigError) as excinfo:
        WorkflowConfig.from_mapping(raw, environ={})

    assert "exactly one agent must have kind 'reviewer'" in str(excinfo.value)


def test_duplicate_agent_workspace_is_rejected() -> None:
    raw = base_mapping()
    raw["agents"]["IMPL"]["workspace"] = "reviewer"  # type: ignore[index]

    with pytest.raises(ConfigError) as excinfo:
        WorkflowConfig.from_mapping(raw, environ={})

    assert "agent workspace values must be unique" in str(excinfo.value)


def test_display_status_names_must_be_unique() -> None:
    raw = base_mapping()
    raw["openproject"] = {"statuses": {"new": "New", "in_progress": "New"}}

    with pytest.raises(ConfigError) as excinfo:
        WorkflowConfig.from_mapping(raw, environ={})

    assert "status names must be unique" in str(excinfo.value)


def test_engine_settings_reads_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OP_BASE_URL", "https://openproject.example.com")
    monkeypatch.setenv("OP_ENGINE_TOKEN", "engine-token")
    monkeypatch.setenv("OP_PROJECT", "demo")
    monkeypatch.setenv("GITHUB_REPO", "owner/demo")
    monkeypatch.setenv("AGENT", "opencode")
    monkeypatch.setenv("WORKSPACE_ROOT", "/srv/agents")
    monkeypatch.setenv("MAX_CONCURRENT_RUNS", "2")
    monkeypatch.setenv("MAX_RUNS_PER_AGENT", "3")
    monkeypatch.setenv("ENGINE_PAUSED", "true")

    settings = EngineSettings()

    assert settings.op_base_url == "https://openproject.example.com"
    assert settings.max_concurrent_runs == 2
    assert settings.max_runs_per_agent == 3
    assert settings.engine_paused is True
    assert settings.agent == "opencode"
    assert settings.op_project == "demo"
    assert settings.github_repo == "owner/demo"
    assert settings.op_engine_token is not None
    assert settings.op_engine_token.get_secret_value() == "engine-token"


def test_engine_settings_rejects_an_unknown_agent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENT", "claude")

    with pytest.raises(ValidationError, match="'pi' or 'opencode'"):
        EngineSettings()


def test_engine_settings_reports_missing_runtime_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in (
        "OP_BASE_URL",
        "OP_ENGINE_TOKEN",
        "OP_PROJECT",
        "GITHUB_REPO",
        "AGENT",
        "WORKSPACE_ROOT",
        *AGENT_TOKEN_VARS,
    ):
        monkeypatch.delenv(name, raising=False)

    settings = EngineSettings()

    assert settings.missing_runtime_values() == [
        "OP_BASE_URL",
        "OP_ENGINE_TOKEN",
        "OP_PROJECT",
        "GITHUB_REPO",
        "AGENT",
        "WORKSPACE_ROOT",
        *AGENT_TOKEN_VARS,
    ]
    with pytest.raises(ConfigError, match="OP_BASE_URL"):
        settings.require_runtime_values()


def test_missing_agent_tokens_are_listed(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = EngineSettings()

    assert settings.missing_agent_tokens({}) == list(AGENT_TOKEN_VARS)
    assert settings.missing_agent_tokens(
        {name: "x" for name in AGENT_TOKEN_VARS}
    ) == []
