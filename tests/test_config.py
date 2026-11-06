from __future__ import annotations

import pytest
from pydantic import SecretStr

from op_agent_engine.config import (
    AGENT_MODEL_VARS,
    AGENT_TOKEN_VARS,
    TEAM,
    ConfigError,
    EngineSettings,
    WorkflowConfig,
)

DEPLOYMENT_ENV = {
    "OP_BASE_URL": "https://openproject.example.com",
    "OP_PROJECT": "demo",
    "OP_TOKEN_FRONTEND": "frontend-token",
    "OP_TOKEN_BACKEND": "backend-token",
    "OP_TOKEN_DESIGNER": "designer-token",
    "OP_TOKEN_VFX": "vfx-token",
    "OP_TOKEN_AUDIO": "audio-token",
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
    }
    values.update(overrides)
    return EngineSettings(**values)


def test_env_builds_the_whole_workflow() -> None:
    config = WorkflowConfig.from_settings(
        deployment_settings(), environ=dict(DEPLOYMENT_ENV)
    )

    assert [project.identifier for project in config.projects] == ["demo"]
    # The repository is resolved from the GIT_REPO project attribute at
    # startup.
    assert config.projects[0].repo is None
    assert set(config.agents) == {
        "FRONTEND",
        "BACKEND",
        "LEAD_DESIGNER",
        "VFX",
        "AUDIO",
        "REVIEWER",
    }
    assert config.reviewer.name == "codeReviewer"
    assert {agent.kind for agent in config.agents.values()} == {"implementer", "reviewer"}
    assert config.agents["FRONTEND"].env["OP_CLI_TOKEN"] == "frontend-token"
    assert config.agents["FRONTEND"].env["OP_CLI_HOST"] == DEPLOYMENT_ENV["OP_BASE_URL"]
    assert config.agents["VFX"].env["OP_CLI_TOKEN"] == "vfx-token"
    assert config.agents["AUDIO"].env["OP_CLI_TOKEN"] == "audio-token"
    assert config.agents["VFX"].name == "vfxArtist"
    assert config.agents["AUDIO"].name == "audioDesigner"
    assert set(config.runners) == {"pi"}


def test_reviewer_can_use_a_dedicated_github_identity() -> None:
    config = WorkflowConfig.from_settings(
        deployment_settings(
            gh_token_reviewer=SecretStr("reviewer-gh-token"),
            git_reviewer_name="reviewer-account",
            git_reviewer_email="reviewer@example.com",
        ),
        environ=dict(DEPLOYMENT_ENV),
    )

    reviewer = config.agents["REVIEWER"].env
    assert reviewer["GH_TOKEN"] == "reviewer-gh-token"
    assert reviewer["GITHUB_TOKEN"] == "reviewer-gh-token"
    assert reviewer["GIT_AUTHOR_NAME"] == "reviewer-account"
    assert reviewer["GIT_AUTHOR_EMAIL"] == "reviewer@example.com"
    assert reviewer["GIT_COMMITTER_NAME"] == "reviewer-account"
    assert reviewer["GIT_COMMITTER_EMAIL"] == "reviewer@example.com"
    # Implementers keep the shared GH_TOKEN inherited from the container.
    assert "GH_TOKEN" not in config.agents["FRONTEND"].env
    assert "github identity: dedicated reviewer token" in "\n".join(config.summary())


def test_dedicated_reviewer_identity_has_safe_defaults() -> None:
    config = WorkflowConfig.from_settings(
        deployment_settings(gh_token_reviewer=SecretStr("reviewer-gh-token")),
        environ=dict(DEPLOYMENT_ENV),
    )

    reviewer = config.agents["REVIEWER"].env
    assert reviewer["GIT_AUTHOR_NAME"] == "codeReviewer"
    assert reviewer["GIT_AUTHOR_EMAIL"] == "codeReviewer@agent-engine.local"


def test_shared_github_identity_remains_the_default() -> None:
    config = WorkflowConfig.from_settings(
        deployment_settings(), environ=dict(DEPLOYMENT_ENV)
    )

    reviewer = config.agents["REVIEWER"].env
    assert "GH_TOKEN" not in reviewer
    assert "GIT_AUTHOR_NAME" not in reviewer
    assert "github identity: shared GH_TOKEN" in "\n".join(config.summary())


def test_env_summary_mentions_entities() -> None:
    config = WorkflowConfig.from_settings(
        deployment_settings(), environ=dict(DEPLOYMENT_ENV)
    )
    summary = "\n".join(config.summary())

    assert "statuses: new=New" in summary
    assert "demo: <GIT_REPO> [enabled]" in summary
    assert "repo attribute: GIT_REPO" in summary
    assert "REVIEWER: codeReviewer (reviewer)" in summary


def test_repo_dir_and_branch_can_be_overridden() -> None:
    config = WorkflowConfig.from_settings(
        deployment_settings(github_repo_dir="clone", github_default_branch="trunk"),
        environ=dict(DEPLOYMENT_ENV),
    )

    assert config.repo_discovery.dir_override == "clone"
    assert config.repo_discovery.default_branch == "trunk"


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


def test_per_agent_model_overrides_the_shared_model() -> None:
    config = WorkflowConfig.from_settings(
        deployment_settings(
            agent_model="openrouter/deepseek/deepseek-v4.1-flash-1017",
            agent_model_reviewer="openrouter/anthropic/claude-sonnet-4-5",
        ),
        environ=dict(DEPLOYMENT_ENV),
    )

    assert config.model_for("REVIEWER") == "openrouter/anthropic/claude-sonnet-4-5"
    assert (
        config.model_for("FRONTEND")
        == "openrouter/deepseek/deepseek-v4.1-flash-1017"
    )


def test_vfx_and_audio_can_have_their_own_models() -> None:
    config = WorkflowConfig.from_settings(
        deployment_settings(
            agent_model="openrouter/deepseek/deepseek-v4.1-flash-1017",
            agent_model_vfx="openrouter/deepseek/deepseek-v4.1-vision",
            agent_model_audio="openrouter/deepseek/deepseek-v4.1-chat",
        ),
        environ=dict(DEPLOYMENT_ENV),
    )

    assert config.model_for("VFX") == "openrouter/deepseek/deepseek-v4.1-vision"
    assert config.model_for("AUDIO") == "openrouter/deepseek/deepseek-v4.1-chat"
    assert (
        config.model_for("BACKEND")
        == "openrouter/deepseek/deepseek-v4.1-flash-1017"
    )


def test_per_agent_models_do_not_need_a_shared_default() -> None:
    config = WorkflowConfig.from_settings(
        deployment_settings(
            agent_model_frontend="openrouter/deepseek/deepseek-v4.1-flash-1017",
            agent_model_backend="openrouter/deepseek/deepseek-v4.1-chat",
        ),
        environ=dict(DEPLOYMENT_ENV),
    )

    assert config.model_for("FRONTEND") == "openrouter/deepseek/deepseek-v4.1-flash-1017"
    assert config.model_for("BACKEND") == "openrouter/deepseek/deepseek-v4.1-chat"
    # An agent without an override keeps the CLI's own default model.
    assert config.model_for("REVIEWER") is None


def test_per_agent_model_may_switch_provider() -> None:
    config = WorkflowConfig.from_settings(
        deployment_settings(
            agent_model="openrouter/deepseek/deepseek-v4.1-flash-1017",
            agent_model_designer="anthropic/claude-sonnet-4-5",
        ),
        environ=dict(DEPLOYMENT_ENV),
    )

    # The default provider is the fallback for every agent without an override.
    assert config.model_for("FRONTEND") == "openrouter/deepseek/deepseek-v4.1-flash-1017"
    assert config.model_for("LEAD_DESIGNER") == "anthropic/claude-sonnet-4-5"
    assert config.providers_in_use() == {
        "openrouter": [
            "FRONTEND",
            "BACKEND",
            "VFX",
            "AUDIO",
            "REVIEWER",
        ],
        "anthropic": ["LEAD_DESIGNER"],
    }
    summary = "\n".join(config.summary())
    assert "model providers: openrouter (FRONTEND" in summary
    assert "anthropic (LEAD_DESIGNER)" in summary


def test_agent_model_variables_cover_the_team() -> None:
    assert set(AGENT_MODEL_VARS) == {key for key, *_ in TEAM}
    for variable in AGENT_MODEL_VARS.values():
        assert variable.lower() in EngineSettings.model_fields, variable


def test_missing_required_values_are_reported() -> None:
    with pytest.raises(ConfigError) as excinfo:
        WorkflowConfig.from_settings(
            deployment_settings(op_project=None),
            environ=dict(DEPLOYMENT_ENV),
        )

    message = str(excinfo.value)
    assert "missing required environment variables" in message
    assert "OP_PROJECT" in message


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
    monkeypatch.setenv("WORKSPACE_ROOT", "/srv/agents")
    monkeypatch.setenv("MAX_CONCURRENT_RUNS", "2")
    monkeypatch.setenv("MAX_RUNS_PER_AGENT", "3")
    monkeypatch.setenv("ENGINE_PAUSED", "true")

    settings = EngineSettings()

    assert settings.op_base_url == "https://openproject.example.com"
    assert settings.max_concurrent_runs == 2
    assert settings.max_runs_per_agent == 3
    assert settings.engine_paused is True
    assert settings.op_project == "demo"
    assert settings.op_engine_token is not None
    assert settings.op_engine_token.get_secret_value() == "engine-token"


def test_engine_settings_reports_missing_runtime_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in (
        "OP_BASE_URL",
        "OP_ENGINE_TOKEN",
        "OP_PROJECT",
        "WORKSPACE_ROOT",
        *AGENT_TOKEN_VARS,
    ):
        monkeypatch.delenv(name, raising=False)

    settings = EngineSettings()

    assert settings.missing_runtime_values() == [
        "OP_BASE_URL",
        "OP_ENGINE_TOKEN",
        "OP_PROJECT",
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


def test_env_project_list_and_wildcard() -> None:
    listed = WorkflowConfig.from_settings(
        deployment_settings(op_project="demo, other"),
        environ=dict(DEPLOYMENT_ENV),
    )
    assert [project.identifier for project in listed.projects] == ["demo", "other"]
    assert all(project.repo is None for project in listed.projects)

    wildcard = WorkflowConfig.from_settings(
        deployment_settings(op_project="*"),
        environ=dict(DEPLOYMENT_ENV),
    )
    assert [project.identifier for project in wildcard.projects] == ["*"]


def test_env_rejects_a_wildcard_mixed_with_identifiers() -> None:
    with pytest.raises(ConfigError, match="cannot be combined"):
        WorkflowConfig.from_settings(
            deployment_settings(op_project="*, demo"),
            environ=dict(DEPLOYMENT_ENV),
        )


def test_env_repo_dir_is_single_project_only() -> None:
    with pytest.raises(ConfigError, match="GITHUB_REPO_DIR is only supported"):
        WorkflowConfig.from_settings(
            deployment_settings(op_project="demo, other", github_repo_dir="clone"),
            environ=dict(DEPLOYMENT_ENV),
        )
