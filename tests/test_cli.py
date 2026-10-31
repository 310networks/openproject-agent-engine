from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

import op_agent_engine.cli as cli
from op_agent_engine.cli import CONFIG_ERROR_EXIT, OK_EXIT, main
from op_agent_engine.openproject.resolve import (
    OpenProjectResolution,
    OpenProjectResolutionError,
    ResolvedAgentField,
    ResolvedProject,
    ResolvedStatus,
    ResolvedType,
    ResolvedUser,
)
from op_agent_engine.provision import ProvisionReport, TokenReport, TokenStatus
from tests.fakes import non_write_requests

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = REPO_ROOT / "workflow.example.yaml"

EXAMPLE_ENV = {
    "OP_BASE_URL": "https://openproject.example.com",
    "AGENT": "pi",
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


async def _fake_audit(config: object, settings: object) -> ProvisionReport:
    return ProvisionReport(apply=False)


@pytest.fixture(autouse=True)
def example_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name, value in EXAMPLE_ENV.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("ENGINE_CONFIG", str(EXAMPLE))
    # The provisioning audit is exercised in tests/test_provision.py; keep the
    # CLI tests offline.
    monkeypatch.setattr(cli, "_audit_against_api", _fake_audit)


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
    assert "Provisioning audit:" in output
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
    assert payload["provisioning"]["apply"] is False
    assert payload["agents"]["FRONTEND"]["projects"] == ["*"]


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


def test_provision_is_dry_run_by_default(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, bool] = {}

    async def fake_provision(
        config: object, settings: object, *, apply: bool
    ) -> ProvisionReport:
        captured["apply"] = apply
        report = ProvisionReport(apply=apply)
        report.add("create_user", "impl", "would create user")
        return report

    monkeypatch.setattr(cli, "_provision_async", fake_provision)

    assert main(["provision", "--config", str(EXAMPLE)]) == OK_EXIT
    assert captured["apply"] is False
    output = capsys.readouterr().out
    assert "provisioning (dry-run)" in output
    assert "create_user impl" in output


def test_provision_apply_and_json(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_provision(
        config: object, settings: object, *, apply: bool
    ) -> ProvisionReport:
        return ProvisionReport(apply=apply)

    monkeypatch.setattr(cli, "_provision_async", fake_provision)

    assert main(["provision", "--apply", "--json", "--config", str(EXAMPLE)]) == OK_EXIT
    payload = json.loads(capsys.readouterr().out)
    assert payload["apply"] is True
    assert payload["actions"] == []


def test_provision_json_keeps_logs_on_stderr(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_provision(
        config: object, settings: object, *, apply: bool
    ) -> ProvisionReport:
        logging.getLogger("httpx").info("HTTP Request: GET /api/v3/users/me")
        return ProvisionReport(apply=apply)

    monkeypatch.setattr(cli, "_provision_async", fake_provision)
    monkeypatch.delenv("LOG_LEVEL", raising=False)

    assert main(["provision", "--apply", "--json", "--config", str(EXAMPLE)]) == OK_EXIT

    captured = capsys.readouterr()
    # stdout must stay valid JSON even when INFO logging is enabled.
    assert json.loads(captured.out)["apply"] is True
    assert "HTTP Request" in captured.err


def test_log_level_flag_overrides_environment(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_provision(
        config: object, settings: object, *, apply: bool
    ) -> ProvisionReport:
        logging.getLogger("httpx").info("HTTP Request: GET /api/v3/users/me")
        return ProvisionReport(apply=apply)

    monkeypatch.setattr(cli, "_provision_async", fake_provision)
    monkeypatch.setenv("LOG_LEVEL", "INFO")
    try:
        assert main(
            [
                "provision",
                "--apply",
                "--json",
                "--log-level",
                "CRITICAL",
                "--config",
                str(EXAMPLE),
            ]
        ) == OK_EXIT
        captured = capsys.readouterr()
        assert json.loads(captured.out)["apply"] is True
        assert "HTTP Request" not in captured.err
    finally:
        logging.getLogger().setLevel(logging.INFO)


def test_check_config_fails_on_agent_token_mismatch(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    async def mismatch_audit(config: object, settings: object) -> ProvisionReport:
        report = ProvisionReport(apply=False)
        report.tokens = TokenReport(
            statuses=(
                TokenStatus(
                    agent_key="FRONTEND",
                    env_var="OP_TOKEN_FRONTEND",
                    login="backendEngineer",
                    ok=False,
                    message="belongs to the wrong login",
                ),
            )
        )
        return report

    monkeypatch.setattr(cli, "_audit_against_api", mismatch_audit)
    monkeypatch.setattr(cli, "_resolve_against_api", _fake_resolve)

    assert main(["check-config", "--config", str(EXAMPLE)]) == CONFIG_ERROR_EXIT
    error = capsys.readouterr().err
    assert "agent token/login mismatch" in error
    assert "frontend-token" not in error


def test_run_requires_valid_config(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["run", "--config", str(tmp_path / "missing.yaml")]) == CONFIG_ERROR_EXIT
    assert "configuration file not found" in capsys.readouterr().err


def test_status_without_data_dir_reports_no_history(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))

    assert main(["status", "--config", str(EXAMPLE)]) == OK_EXIT

    output = capsys.readouterr().out
    assert "heartbeat: missing" in output
    assert "paused: no" in output
    assert "active runs: 0" in output
    assert "last 0 finished run(s)" in output
    assert "(none)" in output


def test_status_json_reports_heartbeat_and_recent_runs(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from op_agent_engine.store import SUCCEEDED, Store

    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True)
    (data_dir / "heartbeat").write_text(
        datetime.now(UTC).isoformat() + "\n", encoding="utf-8"
    )
    store = Store(data_dir / "engine.db")
    try:
        run_id = store.begin_run(
            work_package_id=100,
            project_id=4,
            agent="IMPL",
            kind="implement",
            attempt=1,
            branch="op-4-100",
            run_dir_template=str(store.runs_dir / "{id}"),
        )
        store.finish_run(run_id, state=SUCCEEDED, outcome="ready_for_review")
    finally:
        store.close()
    monkeypatch.setenv("DATA_DIR", str(data_dir))

    assert main(["status", "--json"]) == OK_EXIT

    payload = json.loads(capsys.readouterr().out)
    assert payload["heartbeat_age_seconds"] is not None
    assert payload["heartbeat_age_seconds"] >= 0
    assert payload["active_runs"] == []
    assert len(payload["finished_runs"]) == 1
    assert payload["finished_runs"][0]["work_package_id"] == 100
    assert payload["finished_runs"][0]["state"] == SUCCEEDED


def test_run_refuses_to_start_when_the_lock_is_held(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import fcntl
    import os

    data_dir = tmp_path / "data"
    lock_path = data_dir / "engine.lock"
    lock_path.parent.mkdir(parents=True)
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o644)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    monkeypatch.setenv("DATA_DIR", str(data_dir))
    try:
        assert main(["run", "--config", str(EXAMPLE)]) == 1
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)

    output = capsys.readouterr().out
    assert "another engine instance holds the lock" in output


def test_dry_run_flag_is_accepted_before_subcommand(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import yaml

    import op_agent_engine.engine as engine
    from op_agent_engine.openproject.client import OpenProjectClient
    from tests.test_poller import PRIORITIES
    from tests.test_resolve import make_fake, workflow_mapping

    fake = make_fake(work_packages=[], priorities=PRIORITIES)

    def factory(base_url: str, token: str, **kwargs: Any) -> OpenProjectClient:
        return OpenProjectClient(base_url, token, transport=fake.transport, **kwargs)

    monkeypatch.setattr(engine, "OpenProjectClient", factory)
    monkeypatch.setenv("OP_ENGINE_TOKEN", "test-token")
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))

    config_path = tmp_path / "workflow.yaml"
    config_path.write_text(yaml.safe_dump(workflow_mapping()), encoding="utf-8")

    # A single dry-run poll cycle completes and claims nothing.
    assert main(["--dry-run", "once", "--config", str(config_path)]) == OK_EXIT
    assert len(non_write_requests(fake.requests)) == len(fake.requests)


def test_cleanup_removes_old_run_directories(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    import os
    from datetime import timedelta

    data_dir = tmp_path / "data"
    runs_dir = data_dir / "runs"
    old = runs_dir / "1"
    old.mkdir(parents=True)
    old_file = old / "prompt.txt"
    old_file.write_text("prompt", encoding="utf-8")
    fresh = runs_dir / "2"
    fresh.mkdir()
    timestamp = (datetime.now(UTC) - timedelta(days=40)).timestamp()
    os.utime(old_file, (timestamp, timestamp))
    os.utime(old, (timestamp, timestamp))
    monkeypatch.setenv("DATA_DIR", str(data_dir))
    monkeypatch.setenv("RUN_RETENTION_DAYS", "30")

    assert main(["cleanup", "--json"]) == OK_EXIT

    payload = json.loads(capsys.readouterr().out)
    assert payload["removed_count"] == 1
    assert payload["removed"][0]["name"] == "1"
    assert not old.exists()
    assert fresh.is_dir()


def test_cleanup_dry_run_keeps_directories(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    import os
    from datetime import timedelta

    data_dir = tmp_path / "data"
    old = data_dir / "runs" / "9"
    old.mkdir(parents=True)
    old_file = old / "prompt.txt"
    old_file.write_text("prompt", encoding="utf-8")
    timestamp = (datetime.now(UTC) - timedelta(days=90)).timestamp()
    os.utime(old_file, (timestamp, timestamp))
    os.utime(old, (timestamp, timestamp))
    monkeypatch.setenv("DATA_DIR", str(data_dir))

    assert main(["cleanup", "--days", "30", "--dry-run"]) == OK_EXIT

    output = capsys.readouterr().out
    assert "would remove: 1 run director(ies)" in output
    assert old.is_dir()


def test_cleanup_rejects_negative_days(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))

    assert main(["cleanup", "--days", "-1"]) == CONFIG_ERROR_EXIT
    assert "--days must not be negative" in capsys.readouterr().err
