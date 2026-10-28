from __future__ import annotations

import json
from pathlib import Path

import pytest

from op_agent_engine.cli import CONFIG_ERROR_EXIT, NOT_IMPLEMENTED_EXIT, OK_EXIT, main

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = REPO_ROOT / "config" / "workflow.example.yaml"

EXAMPLE_ENV = {
    "OP_BASE_URL": "https://openproject.example.com",
    "OP_TOKEN_FRONTEND": "frontend-token",
    "OP_TOKEN_BACKEND": "backend-token",
    "OP_TOKEN_DESIGNER": "designer-token",
    "OP_TOKEN_REVIEWER": "reviewer-token",
}


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
    assert main(["check-config"]) == OK_EXIT

    output = capsys.readouterr().out
    assert "Configuration OK" in output
    assert "agents: 4" in output
    assert "runners: 3" in output
    # Secrets must never appear in check-config output.
    assert "frontend-token" not in output


def test_check_config_accepts_config_after_subcommand(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["check-config", "--config", str(EXAMPLE)]) == OK_EXIT
    assert "Configuration OK" in capsys.readouterr().out


def test_check_config_json(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--config", str(EXAMPLE), "check-config", "--json"]) == OK_EXIT

    payload = json.loads(capsys.readouterr().out)
    assert payload["path"] == str(EXAMPLE)
    assert payload["statuses"]["in_testing"] == "In Testing"
    assert payload["agents"]["REVIEWER"]["kind"] == "reviewer"
    assert payload["projects"][0]["github"] == "310networks/example-project"


def test_check_config_reports_missing_environment_variable(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("OP_TOKEN_REVIEWER")

    assert main(["check-config", "--config", str(EXAMPLE)]) == CONFIG_ERROR_EXIT

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

    assert main(["check-config", "--config", str(path)]) == CONFIG_ERROR_EXIT

    error = capsys.readouterr().err
    assert "at least one project must be configured" in error


def test_run_requires_valid_config(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["run", "--config", str(tmp_path / "missing.yaml")]) == CONFIG_ERROR_EXIT
    assert "configuration file not found" in capsys.readouterr().err


def test_stub_commands_load_config_then_report_not_implemented(
    capsys: pytest.CaptureFixture[str],
) -> None:
    for command in ("run", "once", "dry-run", "status"):
        assert main([command, "--config", str(EXAMPLE)]) == NOT_IMPLEMENTED_EXIT

    error = capsys.readouterr().err
    assert "'run' is not implemented yet" in error
    assert "'status' is not implemented yet" in error


def test_dry_run_flag_is_accepted_before_subcommand(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["--dry-run", "run", "--config", str(EXAMPLE)]) == NOT_IMPLEMENTED_EXIT
    assert "not implemented yet" in capsys.readouterr().err
