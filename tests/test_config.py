from __future__ import annotations

from pathlib import Path

import pytest

from op_agent_engine.config import ConfigError, EngineSettings, WorkflowConfig

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = REPO_ROOT / "workflow.example.yaml"

EXAMPLE_ENV = {
    "OP_BASE_URL": "https://openproject.example.com",
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


def test_example_config_loads_with_environment() -> None:
    config = WorkflowConfig.load(EXAMPLE, environ=EXAMPLE_ENV)

    assert [project.identifier for project in config.projects] == ["example-project"]
    assert config.reviewer.name == "codeReviewer"
    assert {agent.kind for agent in config.agents.values()} == {"implementer", "reviewer"}
    # The token placeholder was interpolated, not left literal.
    assert config.agents["FRONTEND"].env["OP_CLI_TOKEN"] == "frontend-token"
    assert config.agents["FRONTEND"].env["OP_CLI_HOST"] == EXAMPLE_ENV["OP_BASE_URL"]


def test_example_config_summary_mentions_entities() -> None:
    config = WorkflowConfig.load(EXAMPLE, environ=EXAMPLE_ENV)
    summary = "\n".join(config.summary())

    assert "statuses: new=New" in summary
    assert "example-project" in summary
    assert "REVIEWER: codeReviewer (reviewer)" in summary


def test_missing_environment_variable_is_reported_with_location() -> None:
    environ = {key: value for key, value in EXAMPLE_ENV.items() if key != "OP_TOKEN_BACKEND"}

    with pytest.raises(ConfigError) as excinfo:
        WorkflowConfig.load(EXAMPLE, environ=environ)

    message = str(excinfo.value)
    assert "unresolved environment variables" in message
    assert "agents.BACKEND.env.OP_CLI_TOKEN" in message
    assert "OP_TOKEN_BACKEND is not set" in message


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


def test_missing_file_has_clear_error(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="configuration file not found"):
        WorkflowConfig.load(tmp_path / "nope.yaml")


def test_invalid_yaml_has_clear_error(tmp_path: Path) -> None:
    path = tmp_path / "broken.yaml"
    path.write_text("projects: [\n", encoding="utf-8")

    with pytest.raises(ConfigError, match="invalid YAML"):
        WorkflowConfig.load(path)


def test_empty_file_has_clear_error(tmp_path: Path) -> None:
    path = tmp_path / "empty.yaml"
    path.write_text("", encoding="utf-8")

    with pytest.raises(ConfigError, match="configuration file is empty"):
        WorkflowConfig.load(path)


def test_engine_settings_reads_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OP_BASE_URL", "https://openproject.example.com")
    monkeypatch.setenv("OP_ENGINE_TOKEN", "engine-token")
    monkeypatch.setenv("WORKSPACE_ROOT", "/srv/agents")
    monkeypatch.setenv("MAX_CONCURRENT_RUNS", "2")
    monkeypatch.setenv("ENGINE_PAUSED", "true")

    settings = EngineSettings()

    assert settings.op_base_url == "https://openproject.example.com"
    assert settings.max_concurrent_runs == 2
    assert settings.engine_paused is True
    assert settings.missing_runtime_values() == []
    assert settings.op_engine_token is not None
    assert settings.op_engine_token.get_secret_value() == "engine-token"


def test_engine_settings_reports_missing_runtime_values(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("OP_BASE_URL", "OP_ENGINE_TOKEN", "WORKSPACE_ROOT"):
        monkeypatch.delenv(name, raising=False)

    settings = EngineSettings()

    assert settings.missing_runtime_values() == [
        "OP_BASE_URL",
        "OP_ENGINE_TOKEN",
        "WORKSPACE_ROOT",
    ]
    with pytest.raises(ConfigError, match="OP_BASE_URL"):
        settings.require_runtime_values()
