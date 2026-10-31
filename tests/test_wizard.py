"""Tests for the interactive setup orchestration."""

from __future__ import annotations

import io
import json
from pathlib import Path
from typing import Any

import pytest

import op_agent_engine.cli as cli
import op_agent_engine.wizard as wizard
from op_agent_engine import install
from op_agent_engine.install import SetupError
from op_agent_engine.wizard import (
    UI,
    TokenRequest,
    run_setup,
    token_requests,
)

WORKFLOW = """\
openproject:
  engine_user: agent-engine
projects:
  - identifier: demo
    repo:
      github: owner/demo
      dir: demo
    enabled: true
agents:
  FRONTEND:
    name: frontendEngineer
    kind: implementer
    op_login: frontendEngineer
    workspace: frontendEngineer
    projects: ["*"]
    runner: pi
    env:
      OP_CLI_HOST: "${OP_BASE_URL}"
      OP_CLI_TOKEN: "${OP_TOKEN_FRONTEND}"
runners:
  pi:
    argv: ["pi", "-p"]
"""

ENV = """\
# operator environment
OP_BASE_URL=https://openproject.example.com
OP_PROVISION_TOKEN=admin-secret-token
OP_ENGINE_TOKEN=replace-with-agent-engine-api-token
OP_TOKEN_FRONTEND=replace-with-frontend-api-token
WORKSPACE_ROOT=/tmp/op-agent-engine/agents
"""


def _source_tree(tmp_path: Path, *, env_text: str = ENV) -> Path:
    source = tmp_path / "checkout"
    source.mkdir()
    (source / "workflow.yaml").write_text(WORKFLOW, encoding="utf-8")
    env_file = source / "engine.env"
    env_file.write_text(env_text, encoding="utf-8")
    env_file.chmod(0o644)
    return source


def test_token_requests_engine_and_agents() -> None:
    raw: dict[str, Any] = {
        "openproject": {"engine_user": "agent-engine"},
        "agents": {
            "FRONTEND": {
                "op_login": "frontendEngineer",
                "env": {"OP_CLI_TOKEN": "${OP_TOKEN_FRONTEND}"},
            },
            "REVIEWER": {
                "op_login": "codeReviewer",
                "env": {"OP_CLI_TOKEN": "${OP_TOKEN_REVIEWER}"},
            },
        },
    }

    requests = token_requests(raw)

    assert requests == [
        TokenRequest(
            "OP_ENGINE_TOKEN", "agent-engine", "agent-engine (runtime)", "engine"
        ),
        TokenRequest(
            "OP_TOKEN_FRONTEND", "frontendEngineer", "FRONTEND (frontendEngineer)", "agent"
        ),
        TokenRequest(
            "OP_TOKEN_REVIEWER", "codeReviewer", "REVIEWER (codeReviewer)", "agent"
        ),
    ]


def test_token_requests_derives_variable_when_env_has_no_reference() -> None:
    raw = {
        "agents": {
            "BACKEND": {
                "op_login": "backendEngineer",
                "env": {"OP_CLI_HOST": "${OP_BASE_URL}"},
            }
        }
    }

    requests = token_requests(raw)

    assert requests[-1].env_var == "OP_TOKEN_BACKEND"
    assert requests[-1].login == "backendEngineer"


def test_token_requests_rejects_shared_variable() -> None:
    raw = {
        "agents": {
            "FRONTEND": {"env": {"OP_CLI_TOKEN": "${OP_TOKEN_SHARED}"}},
            "BACKEND": {"env": {"OP_CLI_TOKEN": "${OP_TOKEN_SHARED}"}},
        }
    }

    with pytest.raises(SetupError, match="used by both"):
        token_requests(raw)


def test_run_setup_happy_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _source_tree(tmp_path)
    config_dir = tmp_path / "config"
    commands: list[list[str]] = []
    environments: list[dict[str, str]] = []

    def fake_capture(
        command: list[str], *, env: dict[str, str], cwd: Path
    ) -> tuple[int, str, str]:
        commands.append(command)
        environments.append(env)
        if "provision" in command:
            return 0, json.dumps(
                {
                    "writes": 5,
                    "passwords": {
                        "agent-engine": "engine-password",
                        "frontendEngineer": "frontend-password",
                    },
                }
            ), ""
        return 0, "", ""

    answers = iter(["engine-secret", "frontend-secret"])
    queried: list[tuple[str, str, str]] = []

    def fake_secret(prompt: str) -> str:
        return next(answers)

    def fake_verify(
        base_url: str, token: str, expected_login: str
    ) -> tuple[bool | None, str]:
        queried.append((base_url, token, expected_login))
        return True, f"verified as {expected_login}"

    monkeypatch.setattr(wizard, "_run_capture", fake_capture)
    monkeypatch.setattr(wizard, "_secret_prompt", fake_secret)
    monkeypatch.setattr(wizard, "_verify_token", fake_verify)

    output = io.StringIO()
    code = run_setup(
        source_dir=source,
        config_dir=config_dir,
        skip_service=True,
        assume_yes=True,
        ui=UI(stream=output),
    )

    assert code == 0
    assert [command[3] for command in commands] == ["init", "provision", "check-config", "dry-run"]
    text_output = output.getvalue()
    assert "engine-secret" not in text_output
    assert "frontend-secret" not in text_output
    assert "My account -> Access tokens" in text_output
    assert "Administration -> Users -> agent-engine -> Administrator" in text_output
    assert text_output.index("agent-engine -> Administrator") < text_output.index(
        "frontend-password"
    )
    # Each password is shown once, at its account's token prompt.
    assert text_output.count("engine-password") == 1
    assert text_output.count("frontend-password") == 1
    assert text_output.index("engine-password") < text_output.index("frontend-password")
    assert [entry[1] for entry in queried] == ["engine-secret", "frontend-secret"]

    provision_env = environments[1]
    assert provision_env["OP_ENGINE_TOKEN"] == ""
    assert provision_env["OP_TOKEN_FRONTEND"] == ""

    for path in (config_dir / "engine.env", source / "engine.env"):
        text = path.read_text(encoding="utf-8")
        assert "OP_ENGINE_TOKEN=engine-secret" in text
        assert "OP_TOKEN_FRONTEND=frontend-secret" in text
        assert "OP_PROVISION_TOKEN=admin-secret-token" in text
        assert "engine-password" not in text
        assert "frontend-password" not in text
        assert path.stat().st_mode & 0o777 == 0o600

    assert (config_dir / "workflow.yaml").is_file()
    assert config_dir.stat().st_mode & 0o777 == 0o700


def test_run_setup_warns_when_existing_user_password_is_unknown(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _source_tree(tmp_path)

    def fake_capture(
        command: list[str], *, env: dict[str, str], cwd: Path
    ) -> tuple[int, str, str]:
        if "provision" in command:
            # No users were created by this run, so no passwords are available.
            return 0, json.dumps({"writes": 0, "passwords": {}}), ""
        return 0, "", ""

    answers = iter(["engine-secret", "frontend-secret"])

    monkeypatch.setattr(wizard, "_run_capture", fake_capture)
    monkeypatch.setattr(
        wizard, "_secret_prompt", lambda prompt: next(answers)
    )
    monkeypatch.setattr(
        wizard, "_verify_token", lambda base_url, token, login: (True, "ok")
    )

    output = io.StringIO()
    code = run_setup(
        source_dir=source,
        config_dir=tmp_path / "config",
        skip_service=True,
        assume_yes=True,
        ui=UI(stream=output),
    )

    assert code == 0
    text_output = output.getvalue()
    assert "already exists" in text_output
    assert "Administration -> Users -> frontendEngineer" in text_output
    # The engine login is warned about once, not once per prompt.
    assert text_output.count("agent-engine already exists") == 1


def test_run_setup_rejects_placeholder_admin_token(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _source_tree(
        tmp_path,
        env_text=ENV.replace(
            "OP_PROVISION_TOKEN=admin-secret-token",
            "OP_PROVISION_TOKEN=replace-with-an-admin-token",
        ).replace(
            "OP_ENGINE_TOKEN=replace-with-agent-engine-api-token",
            "OP_ENGINE_TOKEN=replace-with-an-engine-token",
        ),
    )
    commands: list[list[str]] = []

    def fake_capture(
        command: list[str], *, env: dict[str, str], cwd: Path
    ) -> tuple[int, str, str]:
        commands.append(command)
        return 0, "", ""

    monkeypatch.setattr(wizard, "_run_capture", fake_capture)

    code = run_setup(
        source_dir=source,
        config_dir=tmp_path / "config",
        skip_service=True,
        assume_yes=True,
        ui=UI(stream=io.StringIO()),
    )

    assert code == 1
    assert commands == []


def test_run_setup_requires_source_files(tmp_path: Path) -> None:
    code = run_setup(
        source_dir=tmp_path,
        config_dir=tmp_path / "config",
        skip_service=True,
        assume_yes=True,
        ui=UI(stream=io.StringIO()),
    )

    assert code == 1


def test_cli_setup_delegates(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    def fake_setup(**kwargs: Any) -> int:
        captured.update(kwargs)
        return 0

    monkeypatch.setattr(cli, "run_setup", fake_setup)
    source = tmp_path / "checkout"
    config_dir = tmp_path / "config"

    code = cli.main(
        [
            "setup",
            "--source-dir",
            str(source),
            "--config-dir",
            str(config_dir),
            "--skip-service",
            "--yes",
        ]
    )

    assert code == 0
    assert captured == {
        "source_dir": source,
        "config_dir": config_dir,
        "skip_service": True,
        "assume_yes": True,
        "reprompt_tokens": False,
    }


def test_cli_setup_passes_reprompt_tokens(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    def fake_setup(**kwargs: Any) -> int:
        captured.update(kwargs)
        return 0

    monkeypatch.setattr(cli, "run_setup", fake_setup)

    code = cli.main(
        ["setup", "--source-dir", str(tmp_path), "--reprompt-tokens"]
    )

    assert code == 0
    assert captured["reprompt_tokens"] is True


def test_provision_failure_detail_does_not_expose_passwords() -> None:
    payload = json.dumps(
        {
            "errors": ["missing role"],
            "missing_roles": ["Agent Worker"],
            "passwords": {"frontendEngineer": "super-secret-password"},
        }
    )

    detail = wizard._provision_failure_detail(payload, "")

    assert "missing role" in detail
    assert "Agent Worker" in detail
    assert "super-secret-password" not in detail


def test_provision_failure_detail_uses_stderr_without_json() -> None:
    detail = wizard._provision_failure_detail("", "connection refused")

    assert "connection refused" in detail


def _capture_ok(
    monkeypatch: pytest.MonkeyPatch,
    *,
    verified: dict[str, tuple[bool | None, str]] | None = None,
) -> list[str]:
    """Stub provisioning/checks and record every token prompt."""

    def fake_capture(
        command: list[str], *, env: dict[str, str], cwd: Path
    ) -> tuple[int, str, str]:
        if "provision" in command:
            return 0, json.dumps({"writes": 0, "passwords": {}}), ""
        return 0, "", ""

    prompts: list[str] = []

    def fake_secret(prompt: str) -> str:
        prompts.append(prompt)
        return "typed-token"

    def fake_verify(
        base_url: str, token: str, expected_login: str
    ) -> tuple[bool | None, str]:
        if verified and token in verified:
            return verified[token]
        return True, f"verified as {expected_login}"

    monkeypatch.setattr(wizard, "_run_capture", fake_capture)
    monkeypatch.setattr(wizard, "_secret_prompt", fake_secret)
    monkeypatch.setattr(wizard, "_verify_token", fake_verify)
    return prompts


RESUME_ENV = """\
OP_BASE_URL=https://openproject.example.com
OP_PROVISION_TOKEN=admin-secret-token
OP_ENGINE_TOKEN=stored-engine-token
OP_TOKEN_FRONTEND=stored-frontend-token
WORKSPACE_ROOT=/tmp/op-agent-engine/agents
"""


def test_rerun_reuses_stored_tokens_without_prompting(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Re-running install.sh after a failed install must resume rather than
    # interviewing the operator for tokens that are already stored.
    source = _source_tree(tmp_path, env_text=RESUME_ENV)
    prompts = _capture_ok(monkeypatch)

    output = io.StringIO()
    code = run_setup(
        source_dir=source,
        config_dir=tmp_path / "config",
        skip_service=True,
        assume_yes=True,
        ui=UI(stream=output),
    )

    assert code == 0
    assert prompts == []
    text_output = output.getvalue()
    assert "OP_ENGINE_TOKEN: reusing the existing token" in text_output
    assert "OP_TOKEN_FRONTEND: reusing the existing token" in text_output
    assert "reused 2 existing token(s)" in text_output
    for path in (tmp_path / "config" / "engine.env", source / "engine.env"):
        text = path.read_text(encoding="utf-8")
        assert "OP_ENGINE_TOKEN=stored-engine-token" in text
        assert "OP_TOKEN_FRONTEND=stored-frontend-token" in text


def test_rerun_reprompts_only_the_rejected_token(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _source_tree(tmp_path, env_text=ENV.replace(
        "OP_ENGINE_TOKEN=replace-with-agent-engine-api-token",
        "OP_ENGINE_TOKEN=stored-engine-token",
    ).replace(
        "OP_TOKEN_FRONTEND=replace-with-frontend-api-token",
        "OP_TOKEN_FRONTEND=stored-frontend-token",
    ))
    prompts = _capture_ok(
        monkeypatch,
        verified={"stored-frontend-token": (False, "token rejected (HTTP 401)")},
    )

    output = io.StringIO()
    code = run_setup(
        source_dir=source,
        config_dir=tmp_path / "config",
        skip_service=True,
        assume_yes=True,
        ui=UI(stream=output),
    )

    assert code == 0
    assert len(prompts) == 1
    assert "OP_TOKEN_FRONTEND" in prompts[0]
    text_output = output.getvalue()
    assert "the existing token token rejected (HTTP 401)" in text_output
    text = (tmp_path / "config" / "engine.env").read_text(encoding="utf-8")
    assert "OP_ENGINE_TOKEN=stored-engine-token" in text
    assert "OP_TOKEN_FRONTEND=typed-token" in text


def test_reprompt_tokens_flag_asks_for_every_token(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _source_tree(tmp_path, env_text=RESUME_ENV)
    prompts = _capture_ok(monkeypatch)

    code = run_setup(
        source_dir=source,
        config_dir=tmp_path / "config",
        skip_service=True,
        assume_yes=True,
        reprompt_tokens=True,
        ui=UI(stream=io.StringIO()),
    )

    assert code == 0
    assert len(prompts) == 2
    assert "reusing the existing token" not in " ".join(prompts)
    text = (tmp_path / "config" / "engine.env").read_text(encoding="utf-8")
    assert "OP_ENGINE_TOKEN=typed-token" in text
    assert "OP_TOKEN_FRONTEND=typed-token" in text


def test_rerun_keeps_token_when_verification_is_unavailable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _source_tree(tmp_path, env_text=RESUME_ENV)
    prompts = _capture_ok(
        monkeypatch,
        verified={
            "stored-engine-token": (None, "could not verify token: timeout"),
            "stored-frontend-token": (None, "could not verify token: timeout"),
        },
    )

    output = io.StringIO()
    code = run_setup(
        source_dir=source,
        config_dir=tmp_path / "config",
        skip_service=True,
        assume_yes=True,
        ui=UI(stream=output),
    )

    assert code == 0
    assert prompts == []
    assert "reusing the existing token (could not verify token: timeout)" in (
        output.getvalue()
    )
    text = (tmp_path / "config" / "engine.env").read_text(encoding="utf-8")
    assert "OP_ENGINE_TOKEN=stored-engine-token" in text


def test_rerun_restores_tokens_over_checkout_placeholders(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A re-run copies the checkout's engine.env (still the example, with
    # placeholders) over the config copy. Tokens entered by the previous run
    # must survive that copy, otherwise the operator is interviewed again.
    source = _source_tree(tmp_path)
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "engine.env").write_text(RESUME_ENV, encoding="utf-8")
    prompts = _capture_ok(monkeypatch)

    output = io.StringIO()
    code = run_setup(
        source_dir=source,
        config_dir=config_dir,
        skip_service=True,
        assume_yes=True,
        ui=UI(stream=output),
    )

    assert code == 0
    assert prompts == []
    text_output = output.getvalue()
    assert "OP_ENGINE_TOKEN: restored from the previous install" in text_output
    assert "OP_TOKEN_FRONTEND: restored from the previous install" in text_output
    assert "reused 2 existing token(s)" in text_output
    for path in (config_dir / "engine.env", source / "engine.env"):
        text = path.read_text(encoding="utf-8")
        assert "OP_ENGINE_TOKEN=stored-engine-token" in text
        assert "OP_TOKEN_FRONTEND=stored-frontend-token" in text


def test_provision_users_collects_tokens_into_engine_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The container path: provision, prompt per account, write engine.env."""
    config = tmp_path / "config"
    config.mkdir()
    (config / "workflow.yaml").write_text(WORKFLOW, encoding="utf-8")
    env_file = config / "engine.env"
    env_file.write_text(ENV, encoding="utf-8")
    env_file.chmod(0o600)

    prompted: list[str] = []

    def fake_prompt(text: str) -> str:
        variable = text.rsplit("(", 1)[1].split(")", 1)[0]
        prompted.append(variable)
        return f"tok-{variable}"

    monkeypatch.setattr(wizard, "UI", lambda *args, **kwargs: UI(io.StringIO()))
    monkeypatch.setattr(wizard, "_verify_token", lambda *args: (True, "ok"))
    monkeypatch.setattr(wizard, "_secret_prompt", fake_prompt)
    monkeypatch.setattr(wizard, "_run_captured", lambda *args, **kwargs: (0, "", ""))
    monkeypatch.setattr(
        wizard, "_provision_users", lambda **kwargs: {"frontendEngineer": "pw"}
    )

    assert wizard.run_provision_users(config_dir=config) == 0

    values = install.load_env_file(env_file)
    assert prompted == ["OP_ENGINE_TOKEN", "OP_TOKEN_FRONTEND"]
    assert values["OP_ENGINE_TOKEN"] == "tok-OP_ENGINE_TOKEN"
    assert values["OP_TOKEN_FRONTEND"] == "tok-OP_TOKEN_FRONTEND"
    # The file holds tokens, so it must stay private.
    assert env_file.stat().st_mode & 0o077 == 0


def test_provision_users_requires_both_config_files(tmp_path: Path) -> None:
    config = tmp_path / "config"
    config.mkdir()
    (config / "engine.env").write_text(ENV, encoding="utf-8")

    with pytest.raises(SetupError, match=r"workflow\.yaml"):
        wizard.run_provision_users(config_dir=config)


def test_provision_users_rejects_a_placeholder_base_url(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = tmp_path / "config"
    config.mkdir()
    (config / "workflow.yaml").write_text(WORKFLOW, encoding="utf-8")
    (config / "engine.env").write_text(
        ENV.replace("https://openproject.example.com", "https://openproject.example.com"),
        encoding="utf-8",
    )
    monkeypatch.setattr(wizard, "UI", lambda *args, **kwargs: UI(io.StringIO()))

    with pytest.raises(SetupError, match="OP_BASE_URL"):
        wizard.run_provision_users(config_dir=config)
