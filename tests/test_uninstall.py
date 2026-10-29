"""Tests for local and OpenProject uninstall cleanup."""

from __future__ import annotations

import io
from pathlib import Path
from typing import Any

import pytest

from op_agent_engine import uninstall
from op_agent_engine.config import WorkflowConfig
from op_agent_engine.openproject.client import OpenProjectClient
from op_agent_engine.uninstall import (
    UninstallOptions,
    remove_local_paths,
    remove_openproject_users,
    run_uninstall,
)
from op_agent_engine.wizard import UI
from tests.fakes.openproject import FakeOpenProject

BASE_URL = "https://op.example.com"
ADMIN_TOKEN = "admin-token"


def _user(user_id: int, login: str, *, admin: bool = False) -> dict[str, Any]:
    return {
        "id": user_id,
        "login": login,
        "name": login,
        "admin": admin,
        "status": "active",
        "_links": {"self": {"href": f"/api/v3/users/{user_id}"}},
    }


def _membership(membership_id: int, user_id: int) -> dict[str, Any]:
    return {
        "id": membership_id,
        "_links": {
            "principal": {"href": f"/api/v3/users/{user_id}"},
            "project": {"href": "/api/v3/projects/4"},
            "roles": [{"href": "/api/v3/roles/5"}],
        },
    }


def make_fake(
    *, delete_user_error: tuple[int, str] | None = None
) -> FakeOpenProject:
    return FakeOpenProject(
        expected_token=ADMIN_TOKEN,
        posting_user_id=10,
        users=[
            _user(10, "admin", admin=True),
            _user(11, "agent-engine"),
            _user(12, "impl"),
            _user(13, "reviewer"),
        ],
        projects=[{"id": 4, "identifier": "demo", "name": "Demo"}],
        memberships=[
            _membership(101, 11),
            _membership(102, 12),
            _membership(103, 13),
        ],
        delete_user_error=delete_user_error,
    )


def make_config(*, engine_user: str = "agent-engine") -> WorkflowConfig:
    return WorkflowConfig.from_mapping(
        {
            "openproject": {
                "engine_user": engine_user,
                "provisioning": {
                    "enabled": True,
                    "agent_role": "Agent Worker",
                    "engine_role": "Agent Engine",
                },
            },
            "projects": [
                {
                    "identifier": "demo",
                    "repo": {"github": "owner/demo", "dir": "demo"},
                    "enabled": True,
                }
            ],
            "agents": {
                "IMPL": {
                    "name": "impl",
                    "kind": "implementer",
                    "op_login": "impl",
                    "workspace": "impl",
                    "projects": ["*"],
                    "runner": "pi",
                },
                "REVIEWER": {
                    "name": "reviewer",
                    "kind": "reviewer",
                    "op_login": "reviewer",
                    "workspace": "reviewer",
                    "projects": ["*"],
                    "runner": "pi",
                },
            },
            "runners": {"pi": {"argv": ["pi"]}},
        },
        environ={},
    )


def client_for(fake: FakeOpenProject) -> OpenProjectClient:
    return OpenProjectClient(
        BASE_URL, ADMIN_TOKEN, transport=fake.transport, max_attempts=1
    )


async def test_remove_users_deletes_and_removes_memberships() -> None:
    fake = make_fake()
    client = client_for(fake)
    try:
        report = await remove_openproject_users(client, make_config(), apply=True)
    finally:
        await client.aclose()

    assert report.ok
    assert {action.action for action in report.actions} == {"deleted"}
    assert {user["login"] for user in fake.users} == {"admin"}
    assert fake.memberships == []


async def test_remove_users_falls_back_to_locking() -> None:
    fake = make_fake(delete_user_error=(403, "Forbidden"))
    client = client_for(fake)
    try:
        report = await remove_openproject_users(client, make_config(), apply=True)
    finally:
        await client.aclose()

    assert report.ok
    assert {action.action for action in report.actions} == {"locked"}
    by_login = {user["login"]: user for user in fake.users}
    assert by_login["agent-engine"]["status"] == "locked"
    assert by_login["impl"]["status"] == "locked"
    assert fake.memberships == []


async def test_remove_users_skips_token_owner_and_admin() -> None:
    fake = make_fake()
    fake.users.append(_user(14, "other-admin", admin=True))
    client = client_for(fake)
    try:
        report = await remove_openproject_users(
            client, make_config(engine_user="admin"), apply=True
        )
    finally:
        await client.aclose()

    assert report.ok
    actions = {action.login: action.action for action in report.actions}
    assert actions["admin"] == "skipped"
    # "other-admin" is not in the workflow, so it is never touched.
    assert "other-admin" not in actions
    assert any(user["login"] == "other-admin" for user in fake.users)


async def test_remove_users_dry_run_writes_nothing() -> None:
    fake = make_fake()
    client = client_for(fake)
    try:
        report = await remove_openproject_users(client, make_config(), apply=False)
    finally:
        await client.aclose()

    assert report.ok
    assert {action.action for action in report.actions} == {"would-delete"}
    assert len(fake.users) == 4
    assert len(fake.memberships) == 3
    assert all(request.method == "GET" for request in fake.requests)


def test_remove_local_paths_removes_files_and_directories(tmp_path: Path) -> None:
    config = tmp_path / "config"
    config.mkdir()
    (config / "engine.env").write_text("secret", encoding="utf-8")
    data = tmp_path / "data"
    data.mkdir()
    (data / "runs").mkdir()

    actions = remove_local_paths([config, data], apply=True)

    assert len(actions) == 2
    assert not config.exists()
    assert not data.exists()


def test_local_targets_respects_keep_flags(tmp_path: Path) -> None:
    source = tmp_path / "checkout"
    source.mkdir()
    (source / "workflow.yaml").write_text("workflow", encoding="utf-8")
    (source / "engine.env").write_text("env", encoding="utf-8")
    (source / "engine.env.bak.1").write_text("backup", encoding="utf-8")
    config = tmp_path / "config"
    data = tmp_path / "data"
    workspace = tmp_path / "agents"
    repo = workspace / "impl" / "repositories" / "demo"
    repo.mkdir(parents=True)

    options = UninstallOptions(
        source_dir=source,
        config_dir=config,
        data_dir=data,
        purge_agent_repos=True,
    )
    targets = uninstall.local_targets(options, workspace_root=workspace)

    assert config in targets
    assert data in targets
    assert source / "engine.env" in targets
    assert source / "engine.env.bak.1" in targets
    assert workspace / "impl" / "repositories" in targets

    kept = UninstallOptions(
        source_dir=source,
        config_dir=config,
        data_dir=data,
        keep_config=True,
        keep_data=True,
    )
    assert uninstall.local_targets(kept, workspace_root=workspace) == []


def test_run_uninstall_dry_run_changes_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    source = tmp_path / "checkout"
    source.mkdir()
    (source / "workflow.yaml").write_text("workflow", encoding="utf-8")
    (source / "engine.env").write_text("env", encoding="utf-8")
    config = tmp_path / "config"
    config.mkdir()
    (config / "workflow.yaml").write_text("workflow", encoding="utf-8")
    data = tmp_path / "data"
    data.mkdir()
    options = UninstallOptions(
        source_dir=source, config_dir=config, data_dir=data, apply=False
    )

    code = run_uninstall(options, ui=UI(stream=io.StringIO()))

    assert code == 0
    assert (source / "engine.env").exists()
    assert config.exists()
    assert data.exists()


def test_run_uninstall_keep_users_removes_local_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "checkout"
    source.mkdir()
    (source / "workflow.yaml").write_text("workflow", encoding="utf-8")
    (source / "engine.env").write_text("env", encoding="utf-8")
    config = tmp_path / "config"
    config.mkdir()
    data = tmp_path / "data"
    data.mkdir()

    monkeypatch.setattr(uninstall, "stop_service", lambda ui: None)
    monkeypatch.setattr(uninstall, "remove_service_files", lambda ui: None)

    options = UninstallOptions(
        source_dir=source,
        config_dir=config,
        data_dir=data,
        keep_users=True,
        apply=True,
        assume_yes=True,
    )

    code = run_uninstall(options, ui=UI(stream=io.StringIO()))

    assert code == 0
    assert not (source / "workflow.yaml").exists()
    assert not (source / "engine.env").exists()
    assert not config.exists()
    assert not data.exists()


def test_cli_uninstall_delegates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import op_agent_engine.cli as cli

    captured: dict[str, Any] = {}

    def fake_uninstall(options: UninstallOptions) -> int:
        captured["options"] = options
        return 0

    monkeypatch.setattr(cli, "run_uninstall", fake_uninstall)
    source = tmp_path / "checkout"
    config = tmp_path / "config"
    data = tmp_path / "data"

    code = cli.main(
        [
            "uninstall",
            "--source-dir",
            str(source),
            "--config-dir",
            str(config),
            "--data-dir",
            str(data),
            "--apply",
            "--yes",
            "--keep-config",
            "--keep-data",
            "--keep-users",
            "--purge-agent-repos",
        ]
    )

    assert code == 0
    options = captured["options"]
    assert options == UninstallOptions(
        source_dir=source,
        config_dir=config,
        data_dir=data,
        keep_config=True,
        keep_data=True,
        keep_users=True,
        purge_agent_repos=True,
        apply=True,
        assume_yes=True,
    )
