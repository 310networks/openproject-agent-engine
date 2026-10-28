from __future__ import annotations

import json
from pathlib import Path

import pytest

import op_agent_engine.cli as cli
from op_agent_engine.cli import CONFIG_ERROR_EXIT, NOT_IMPLEMENTED_EXIT, OK_EXIT, main
from op_agent_engine.openproject.resolve import (
    OpenProjectResolution,
    OpenProjectResolutionError,
    ResolvedAgentField,
    ResolvedProject,
    ResolvedStatus,
    ResolvedType,
    ResolvedUser,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = REPO_ROOT / "config" / "workflow.example.yaml"

EXAMPLE_ENV = {
    "OP_BASE_URL": "https://openproject.example.com",
    "OP_ENGINE_TOKEN": "engine-token",
    "OP_TOKEN_FRONTEND": "frontend-token",
    "OP_TOKEN_BACKEND": "backend-token",
    "OP_TOKEN_DESIGNER": "designer-token",
    "OP_TOKEN_REVIEWER": "reviewer-token",
}

SAMPLE_RESOLUTION = OpenProjectResolution(
    statuses={
        "new": ResolvedStatus("new", "New", 1),
        "in_progress": ResolvedStatus("in_progress", "In Progress", 2),
        "ready_for_review": ResolvedStatus("ready_for_review", "Ready for Review", 3),
        "in_testing": ResolvedStatus("in_testing", "In Testing", 4),
        "on_hold": ResolvedStatus("on_hold", "On Hold", 5),
        "rejected": ResolvedStatus("rejected", "Rejected", 6),
        "closed": ResolvedStatus("closed", "Closed", 7),
    },
    users={
        "agent-engine": ResolvedUser("agent-engine", 10, "Agent Engine"),
        "frontendEngineer": ResolvedUser("frontendEngineer", 11, "Frontend"),
    },
    engine_user=ResolvedUser("agent-engine", 10, "Agent Engine"),
    projects={
        "example-project": ResolvedProject(
            "example-project", 4, "Example Project", {"Task": ResolvedType("Task", 1)}
        )
    },
    agent_field=ResolvedAgentField(
        "AGENT",
        "customField3",
        {"FRONTEND": 11, "BACKEND": 12, "LEAD_DESIGNER": 13, "REVIEWER": 14},
    ),
)


async def _fake_resolve(config: object, settings: object) -> OpenProjectResolution:
    return SAMPLE_RESOLUTION


@pytest.fixture(autouse=True)
def example_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name, value in EXAMPLE_ENV.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("ENGINE_CONFIG", str(EXAMPLE))


def test_help_exits_zero(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as excinfo:
        main(["--help"])

    assert excinfo.value.code == 0
    output = capsys.readouterr().out
    assert "check-config" in output
    assert "run" in output


def test_version_exits_zero(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as excinfo:
        main(["--version"])

    assert excinfo.value.code == 0
    assert "op-agent-engine" in capsys.readouterr().out


def test_check_config_default_path_from_environment(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["check-config", "--offline"]) == OK_EXIT

    output = capsys.readouterr().out
    assert "Configuration OK" in output
    assert "agents: 4" in output
    assert "runners: 3" in output
    # Secrets must never appear in check-config output.
    assert "frontend-token" not in output


def test_check_config_accepts_config_after_subcommand(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["check-config", "--offline", "--config", str(EXAMPLE)]) == OK_EXIT
    assert "Configuration OK" in capsys.readouterr().out


def test_check_config_json(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--config", str(EXAMPLE), "check-config", "--json", "--offline"]) == OK_EXIT

    payload = json.loads(capsys.readouterr().out)
    assert payload["path"] == str(EXAMPLE)
    assert payload["statuses"]["in_testing"] == "In Testing"
    assert payload["agents"]["REVIEWER"]["kind"] == "reviewer"
    assert payload["projects"][0]["github"] == "310networks/example-project"
    assert payload["resolved"] is None


def test_check_config_reports_missing_environment_variable(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("OP_TOKEN_REVIEWER")

    assert main(["check-config", "--offline", "--config", str(EXAMPLE)]) == CONFIG_ERROR_EXIT

    error = capsys.readouterr().err
    assert "error:" in error
    assert "OP_TOKEN_REVIEWER is not set" in error


def test_check_config_reports_validation_errors(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "workflow.yaml"
    path.write_text(
        """
projects: []
agents:
  IMPL:
    name: impl
    kind: implementer
    op_login: impl
    workspace: impl
    runner: pi
  REVIEWER:
    name: reviewer
    kind: reviewer
    op_login: reviewer
    workspace: reviewer
    runner: pi
runners:
  pi:
    argv: ["pi", "-p", "@{prompt_file}"]
""",
        encoding="utf-8",
    )

    assert main(["check-config", "--offline", "--config", str(path)]) == CONFIG_ERROR_EXIT

    error = capsys.readouterr().err
    assert "at least one project must be configured" in error


def test_check_config_requires_api_environment(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "workflow.yaml"
    path.write_text(
        """
projects:
  - identifier: demo
    repo: { github: owner/demo, dir: demo }
agents:
  IMPL:
    name: impl
    kind: implementer
    op_login: impl
    workspace: impl
    runner: pi
  REVIEWER:
    name: reviewer
    kind: reviewer
    op_login: reviewer
    workspace: reviewer
    runner: pi
runners:
  pi:
    argv: ["pi", "-p", "@{prompt_file}"]
""",
        encoding="utf-8",
    )
    monkeypatch.delenv("OP_BASE_URL", raising=False)
    monkeypatch.delenv("OP_ENGINE_TOKEN", raising=False)

    assert main(["check-config", "--config", str(path)]) == CONFIG_ERROR_EXIT

    error = capsys.readouterr().err
    assert "OP_BASE_URL, OP_ENGINE_TOKEN" in error


def test_check_config_reports_resolved_ids(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli, "_resolve_against_api", _fake_resolve)

    assert main(["check-config", "--config", str(EXAMPLE)]) == OK_EXIT

    output = capsys.readouterr().out
    assert "Resolved OpenProject IDs:" in output
    assert "in_testing: In Testing (id 4)" in output
    assert "agent-engine: Agent Engine (id 10)" in output
    assert "example-project: Example Project (id 4)" in output
    assert "agent field: AGENT -> customField3" in output
    assert "FRONTEND: option id 11" in output
    assert "engine-token" not in output


def test_check_config_json_includes_resolved_ids(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli, "_resolve_against_api", _fake_resolve)

    assert main(["check-config", "--config", str(EXAMPLE), "--json"]) == OK_EXIT

    payload = json.loads(capsys.readouterr().out)
    assert payload["resolved"]["statuses"]["in_testing"]["id"] == 4
    assert payload["resolved"]["agent_field"]["field_id"] == "customField3"
    assert payload["resolved"]["agent_field"]["options"]["REVIEWER"] == 14
    assert payload["resolved"]["projects"]["example-project"]["types"]["Task"] == 1


def test_check_config_reports_resolution_failure(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    async def failing_resolve(config: object, settings: object) -> OpenProjectResolution:
        raise OpenProjectResolutionError("OpenProject statuses not found: in_testing='In Testing'")

    monkeypatch.setattr(cli, "_resolve_against_api", failing_resolve)

    assert main(["check-config", "--config", str(EXAMPLE)]) == CONFIG_ERROR_EXIT

    error = capsys.readouterr().err
    assert "OpenProject statuses not found" in error
    assert "in_testing='In Testing'" in error


def test_run_requires_valid_config(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["run", "--config", str(tmp_path / "missing.yaml")]) == CONFIG_ERROR_EXIT
    assert "configuration file not found" in capsys.readouterr().err


def test_stub_commands_load_config_then_report_not_implemented(
    capsys: pytest.CaptureFixture[str],
) -> None:
    # ``run`` and ``status`` are still stubs; ``once``/``dry-run`` are
    # implemented in M2 and drive a real (mocked) poll instead.
    for command in ("run", "status"):
        assert main([command, "--config", str(EXAMPLE)]) == NOT_IMPLEMENTED_EXIT

    error = capsys.readouterr().err
    assert "'run' is not implemented yet" in error
    assert "'status' is not implemented yet" in error


def test_dry_run_flag_is_accepted_before_subcommand(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["--dry-run", "run", "--config", str(EXAMPLE)]) == NOT_IMPLEMENTED_EXIT
    assert "not implemented yet" in capsys.readouterr().err
