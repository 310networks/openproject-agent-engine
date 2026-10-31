"""Tests for the account provisioning and token interview.

``op-agent-engine provision-users`` runs before the container starts: it
creates the OpenProject accounts, shows the generated passwords once at each
account's token prompt, and writes the tokens it collects into ``engine.env``.
"""

from __future__ import annotations

import io
import json
from pathlib import Path
from typing import Any

import pytest

import op_agent_engine.account_setup as account_setup
from op_agent_engine.account_setup import (
    UI,
    AccountSetupError,
    TokenRequest,
    run_provision_users,
    token_requests,
)
from op_agent_engine.envfile import load_env_file

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
WORKSPACE_ROOT=/opt/op-agent-engine/agents
AGENT=pi
"""

STORED_ENV = """\
OP_BASE_URL=https://openproject.example.com
OP_PROVISION_TOKEN=admin-secret-token
OP_ENGINE_TOKEN=stored-engine-token
OP_TOKEN_FRONTEND=stored-frontend-token
WORKSPACE_ROOT=/opt/op-agent-engine/agents
AGENT=pi
"""


def _config(tmp_path: Path, *, env_text: str = ENV) -> Path:
    """A ``config/`` directory as the container deployment ships it."""
    config = tmp_path / "config"
    config.mkdir()
    (config / "workflow.yaml").write_text(WORKFLOW, encoding="utf-8")
    env_file = config / "engine.env"
    env_file.write_text(env_text, encoding="utf-8")
    env_file.chmod(0o600)
    return config


def _stub_setup(
    monkeypatch: pytest.MonkeyPatch,
    *,
    verified: dict[str, tuple[bool | None, str]] | None = None,
    passwords: dict[str, str] | None = None,
) -> list[str]:
    """Stub provisioning, verification and ``check-config``; record prompts."""
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

    monkeypatch.setattr(
        account_setup, "UI", lambda *args, **kwargs: UI(io.StringIO())
    )
    monkeypatch.setattr(account_setup, "_secret_prompt", fake_secret)
    monkeypatch.setattr(account_setup, "_verify_token", fake_verify)
    monkeypatch.setattr(account_setup, "_run_captured", lambda *args, **kwargs: (0, "", ""))
    monkeypatch.setattr(account_setup, "_provision_users", lambda **kwargs: dict(passwords or {}))
    return prompts


# ---------------------------------------------------------------------------
# Token requests
# ---------------------------------------------------------------------------


def test_token_requests_engine_and_agents() -> None:
    raw: dict[str, Any] = {
        "openproject": {"engine_user": "agent-engine"},
        "agents": {
            "FRONTEND": {
                "name": "frontendEngineer",
                "op_login": "frontendEngineer",
                "env": {"OP_CLI_TOKEN": "${OP_TOKEN_FRONTEND}"},
            }
        },
    }

    requests = token_requests(raw)

    assert [request.env_var for request in requests] == [
        "OP_ENGINE_TOKEN",
        "OP_TOKEN_FRONTEND",
    ]
    assert requests[0].login == "agent-engine"
    assert requests[0].kind == "engine"
    assert requests[1].kind == "agent"
    assert isinstance(requests[1], TokenRequest)


def test_token_requests_derives_variable_when_env_has_no_reference() -> None:
    raw: dict[str, Any] = {
        "openproject": {"engine_user": "agent-engine"},
        "agents": {
            "BACKEND": {
                "name": "backendEngineer",
                "op_login": "backendEngineer",
                "env": {"OP_CLI_TOKEN": "not-an-interpolation"},
            }
        },
    }

    requests = token_requests(raw)

    assert [request.env_var for request in requests] == [
        "OP_ENGINE_TOKEN",
        "OP_TOKEN_BACKEND",
    ]


def test_token_requests_rejects_shared_variable() -> None:
    raw: dict[str, Any] = {
        "openproject": {"engine_user": "agent-engine"},
        "agents": {
            "FRONTEND": {
                "name": "frontendEngineer",
                "op_login": "frontendEngineer",
                "env": {"OP_CLI_TOKEN": "${OP_TOKEN_SHARED}"},
            },
            "BACKEND": {
                "name": "backendEngineer",
                "op_login": "backendEngineer",
                "env": {"OP_CLI_TOKEN": "${OP_TOKEN_SHARED}"},
            },
        },
    }

    with pytest.raises(AccountSetupError, match="OP_TOKEN_SHARED"):
        token_requests(raw)


# ---------------------------------------------------------------------------
# Provisioning failures
# ---------------------------------------------------------------------------


def test_provision_failure_detail_does_not_expose_passwords() -> None:
    payload = json.dumps(
        {
            "errors": ["missing role"],
            "missing_roles": ["Agent Worker"],
            "passwords": {"frontendEngineer": "super-secret-password"},
        }
    )

    detail = account_setup._provision_failure_detail(payload, "")

    assert "missing role" in detail
    assert "Agent Worker" in detail
    assert "super-secret-password" not in detail


def test_provision_failure_detail_uses_stderr_without_json() -> None:
    detail = account_setup._provision_failure_detail("", "connection refused")

    assert "connection refused" in detail


# ---------------------------------------------------------------------------
# The token interview
# ---------------------------------------------------------------------------


def test_provision_users_collects_tokens_into_engine_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path)

    def fake_prompt(text: str) -> str:
        variable = text.rsplit("(", 1)[1].split(")", 1)[0]
        return f"tok-{variable}"

    monkeypatch.setattr(
        account_setup, "UI", lambda *args, **kwargs: UI(io.StringIO())
    )
    monkeypatch.setattr(account_setup, "_verify_token", lambda *args: (True, "ok"))
    monkeypatch.setattr(account_setup, "_secret_prompt", fake_prompt)
    monkeypatch.setattr(account_setup, "_run_captured", lambda *args, **kwargs: (0, "", ""))
    monkeypatch.setattr(
        account_setup, "_provision_users", lambda **kwargs: {"frontendEngineer": "pw"}
    )

    assert run_provision_users(config_dir=config) == 0

    values = load_env_file(config / "engine.env")
    assert values["OP_ENGINE_TOKEN"] == "tok-OP_ENGINE_TOKEN"
    assert values["OP_TOKEN_FRONTEND"] == "tok-OP_TOKEN_FRONTEND"
    # The file holds tokens, so it must stay private.
    assert (config / "engine.env").stat().st_mode & 0o077 == 0


def test_provision_users_rerun_reuses_stored_tokens(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A re-run must resume instead of interviewing for tokens already stored.
    config = _config(tmp_path, env_text=STORED_ENV)
    prompts = _stub_setup(monkeypatch)

    assert run_provision_users(config_dir=config) == 0

    assert prompts == []
    text = (config / "engine.env").read_text(encoding="utf-8")
    assert "OP_ENGINE_TOKEN=stored-engine-token" in text
    assert "OP_TOKEN_FRONTEND=stored-frontend-token" in text


def test_provision_users_rerun_reprompts_only_the_rejected_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path, env_text=STORED_ENV)
    prompts = _stub_setup(
        monkeypatch, verified={"stored-frontend-token": (False, "token rejected (HTTP 401)")}
    )

    assert run_provision_users(config_dir=config) == 0

    assert len(prompts) == 1
    assert "OP_TOKEN_FRONTEND" in prompts[0]
    text = (config / "engine.env").read_text(encoding="utf-8")
    assert "OP_ENGINE_TOKEN=stored-engine-token" in text
    assert "OP_TOKEN_FRONTEND=typed-token" in text


def test_provision_users_reprompt_flag_asks_for_every_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path, env_text=STORED_ENV)
    prompts = _stub_setup(monkeypatch)

    assert run_provision_users(config_dir=config, reprompt_tokens=True) == 0

    assert len(prompts) == 2
    assert "reusing the existing token" not in " ".join(prompts)
    text = (config / "engine.env").read_text(encoding="utf-8")
    assert "OP_ENGINE_TOKEN=typed-token" in text
    assert "OP_TOKEN_FRONTEND=typed-token" in text


def test_provision_users_keeps_token_when_verification_is_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Offline OpenProject must not throw away a token that is already stored.
    config = _config(tmp_path, env_text=STORED_ENV)
    prompts = _stub_setup(
        monkeypatch,
        verified={
            "stored-engine-token": (None, "could not verify token: timeout"),
            "stored-frontend-token": (None, "could not verify token: timeout"),
        },
    )

    assert run_provision_users(config_dir=config) == 0

    assert prompts == []
    text = (config / "engine.env").read_text(encoding="utf-8")
    assert "OP_ENGINE_TOKEN=stored-engine-token" in text


def test_provision_users_requires_both_config_files(tmp_path: Path) -> None:
    config = tmp_path / "config"
    config.mkdir()
    (config / "engine.env").write_text(ENV, encoding="utf-8")

    with pytest.raises(AccountSetupError, match=r"workflow\.yaml"):
        run_provision_users(config_dir=config)


def test_provision_users_rejects_a_placeholder_base_url(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(
        tmp_path,
        env_text=ENV.replace(
            "https://openproject.example.com", "https://openproject.example.com"
        ),
    )
    monkeypatch.setattr(
        account_setup, "UI", lambda *args, **kwargs: UI(io.StringIO())
    )

    with pytest.raises(AccountSetupError, match="OP_BASE_URL"):
        run_provision_users(config_dir=config)
