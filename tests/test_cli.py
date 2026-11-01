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

EXAMPLE_ENV = {
    "OP_BASE_URL": "https://openproject.example.com",
    "OP_PROJECT": "example-project",
    "AGENT_MODEL": "openrouter/deepseek/deepseek-v4.1-flash-1017",
    "OPENROUTER_API_KEY": "sk-or-test",
    "OP_ENGINE_TOKEN": "engine-token",
    "OP_TOKEN_FRONTEND": "frontend-token",
    "OP_TOKEN_BACKEND": "backend-token",
    "OP_TOKEN_DESIGNER": "designer-token",
    "OP_TOKEN_REVIEWER": "reviewer-token",
    "WORKSPACE_ROOT": "/srv/agents",
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


def test_check_config_reads_the_environment(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["check-config", "--offline"]) == OK_EXIT

    output = capsys.readouterr().out
    assert "Configuration OK" in output
    assert "agents: 4" in output
    assert "runners: 1" in output
    # Secrets must never appear in check-config output.
    assert "frontend-token" not in output


def test_check_config_json(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["check-config", "--json", "--offline"]) == OK_EXIT

    payload = json.loads(capsys.readouterr().out)
    assert payload["source"] == "environment"
    assert payload["statuses"]["in_testing"] == "In Testing"
    assert payload["agents"]["REVIEWER"]["kind"] == "reviewer"
    # The repository is resolved from GIT_REPO at startup, not built offline.
    assert payload["projects"][0]["github"] is None
    assert payload["repo_field"] == "GIT_REPO"
    assert payload["resolved"] is None
    assert payload["provider_credentials"]["level"] == "ok"


def test_check_config_reports_missing_environment_variable(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("OP_PROJECT")

    assert main(["check-config", "--offline"]) == CONFIG_ERROR_EXIT

    error = capsys.readouterr().err
    assert "error:" in error
    assert "OP_PROJECT" in error


def test_check_config_reports_validation_errors(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OP_PROJECT", "*, demo")

    assert main(["check-config", "--offline"]) == CONFIG_ERROR_EXIT

    error = capsys.readouterr().err
    assert "cannot be combined with explicit project identifiers" in error


def test_check_config_requires_the_provider_key(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("OPENROUTER_API_KEY")

    assert main(["check-config", "--offline"]) == CONFIG_ERROR_EXIT

    error = capsys.readouterr().err
    assert "OPENROUTER_API_KEY" in error
    assert "sk-or-test" not in error


def test_check_config_rejects_mixed_model_providers(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AGENT_MODEL_REVIEWER", "anthropic/claude-sonnet-4-5")

    assert main(["check-config", "--offline"]) == CONFIG_ERROR_EXIT

    assert "one model provider per deployment" in capsys.readouterr().err


def test_check_config_warns_when_no_model_is_configured(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("AGENT_MODEL")

    assert main(["check-config", "--offline"]) == OK_EXIT

    assert "provider credentials are not checked" in capsys.readouterr().err


def test_check_config_requires_api_environment(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("OP_ENGINE_TOKEN", raising=False)

    assert main(["check-config"]) == CONFIG_ERROR_EXIT

    error = capsys.readouterr().err
    assert "missing required environment variables: OP_ENGINE_TOKEN" in error


def test_check_config_reports_resolved_ids(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli, "_resolve_against_api", _fake_resolve)

    assert main(["check-config"]) == OK_EXIT

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

    assert main(["check-config", "--json"]) == OK_EXIT

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

    assert main(["check-config"]) == CONFIG_ERROR_EXIT

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

    assert main(["provision"]) == OK_EXIT
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

    assert main(["provision", "--apply", "--json"]) == OK_EXIT
    payload = json.loads(capsys.readouterr().out)
    assert payload["apply"] is True
    assert payload["actions"] == []


def test_provision_passwords_prints_login_and_password_lines(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_provision(
        config: object, settings: object, *, apply: bool
    ) -> ProvisionReport:
        report = ProvisionReport(apply=apply)
        report.generated_passwords.update(
            {"agent-engine": "Pw1!", "frontendEngineer": "Pw2?"}
        )
        return report

    monkeypatch.setattr(cli, "_provision_async", fake_provision)

    assert main(["provision", "--apply", "--passwords"]) == OK_EXIT

    output = capsys.readouterr().out
    assert "agent-engine\tPw1!" in output
    assert "frontendEngineer\tPw2?" in output
    # Machine-readable: no JSON and no report text around the lines.
    assert "{" not in output


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

    assert main(["provision", "--apply", "--json"]) == OK_EXIT

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

    assert main(["check-config"]) == CONFIG_ERROR_EXIT
    error = capsys.readouterr().err
    assert "agent token/login mismatch" in error
    assert "frontend-token" not in error


def test_run_requires_deployment_values(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("OP_PROJECT")

    assert main(["run"]) == CONFIG_ERROR_EXIT
    assert "OP_PROJECT" in capsys.readouterr().err


def test_status_without_data_dir_reports_no_history(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))

    assert main(["status"]) == OK_EXIT

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
        assert main(["run"]) == 1
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)

    output = capsys.readouterr().out
    assert "another engine instance holds the lock" in output


def test_dry_run_flag_is_accepted_before_subcommand(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import op_agent_engine.engine as engine
    from op_agent_engine.openproject.client import OpenProjectClient
    from tests.test_poller import PRIORITIES
    from tests.test_resolve import make_config, make_fake

    fake = make_fake(work_packages=[], priorities=PRIORITIES)

    def factory(base_url: str, token: str, **kwargs: Any) -> OpenProjectClient:
        return OpenProjectClient(base_url, token, transport=fake.transport, **kwargs)

    monkeypatch.setattr(engine, "OpenProjectClient", factory)
    monkeypatch.setattr(cli, "_load_config", lambda settings=None: make_config())
    monkeypatch.setenv("OP_ENGINE_TOKEN", "test-token")
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))

    # A single dry-run poll cycle completes and claims nothing.
    assert main(["--dry-run", "once"]) == OK_EXIT
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
