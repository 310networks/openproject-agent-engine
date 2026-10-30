"""Tests for the read-only dashboard data layer and HTTP server."""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from op_agent_engine.config import EngineSettings, WorkflowConfig
from op_agent_engine.dashboard import (
    DashboardData,
    DashboardHTTPServer,
    _asset,
)
from op_agent_engine.engine import write_heartbeat
from op_agent_engine.store import FAILED, SUCCEEDED, Store

RUNS_TEMPLATE = "{root}/{id}"


def _config() -> WorkflowConfig:
    return WorkflowConfig.from_mapping(
        {
            "projects": [
                {
                    "identifier": "demo",
                    "repo": {"github": "owner/repo", "dir": "repo"},
                }
            ],
            "agents": {
                "IMPL": {
                    "name": "implementer",
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
            "runners": {"pi": {"argv": ["pi"]}},
        },
        environ={},
    )


def _settings(data_dir: Path) -> EngineSettings:
    return EngineSettings(
        data_dir=data_dir,
        op_base_url="https://openproject.example",
        poll_interval_seconds=60,
        max_concurrent_runs=2,
    )


@pytest.fixture
def snapshot(tmp_path: Path) -> tuple[dict[str, Any], Path]:
    settings = _settings(tmp_path)
    config = _config()
    write_heartbeat(settings)

    store = Store(tmp_path / "engine.db")
    try:
        active = store.begin_run(
            work_package_id=77,
            project_id=4,
            agent="IMPL",
            kind="implement",
            attempt=1,
            branch="op-4-77",
            run_dir_template=str(store.runs_dir / "{id}"),
            display_id="#77",
            subject="Build the dashboard",
        )
        approved = store.begin_run(
            work_package_id=78,
            project_id=4,
            agent="REVIEWER",
            kind="review",
            attempt=1,
            branch="op-4-78",
            run_dir_template=str(store.runs_dir / "{id}"),
            display_id="#78",
            subject="Approve the dashboard",
        )
        store.finish_run(approved, state=SUCCEEDED, outcome="approved")
        rejected = store.begin_run(
            work_package_id=79,
            project_id=4,
            agent="REVIEWER",
            kind="review",
            attempt=1,
            branch="op-4-79",
            run_dir_template=str(store.runs_dir / "{id}"),
        )
        store.finish_run(rejected, state=SUCCEEDED, outcome="rejected")
        failed = store.begin_run(
            work_package_id=80,
            project_id=4,
            agent="DESIGNER",
            kind="implement",
            attempt=1,
            branch="op-4-80",
            run_dir_template=str(store.runs_dir / "{id}"),
        )
        store.finish_run(failed, state=FAILED, outcome="nonzero_exit")

        meta_dir = store.runs_dir / str(approved)
        meta_dir.mkdir(parents=True, exist_ok=True)
        (meta_dir / "meta.json").write_text(
            json.dumps(
                {
                    "project": "demo",
                    "repo": "owner/repo",
                    "pr_url": "https://github.com/owner/repo/pull/1",
                    "duration_seconds": 42.5,
                    "env": {"OP_TOKEN_IMPL": "must-not-leak"},
                }
            ),
            encoding="utf-8",
        )
        assert active == 1
    finally:
        store.close()

    return DashboardData(config, settings).snapshot(), tmp_path


def test_snapshot_engine_status(snapshot: tuple[dict[str, Any], Path]) -> None:
    payload, _ = snapshot
    engine = payload["engine"]
    assert engine["status"] == "active"
    assert engine["polling"] is True
    assert engine["paused"] is False
    assert engine["heartbeat_age_seconds"] is not None
    assert engine["active_runs"] == 1
    assert engine["max_concurrent_runs"] == 2


def test_snapshot_counts(snapshot: tuple[dict[str, Any], Path]) -> None:
    payload, _ = snapshot
    counts = payload["counts"]
    assert counts["total"] == 4
    assert counts["running"] == 1
    assert counts["completed"] == 1
    assert counts["declined"] == 1
    assert counts["blocked"] == 0
    assert counts["failed"] == 1
    assert counts["outcomes"] == {"approved": 1, "nonzero_exit": 1, "rejected": 1}


def test_snapshot_agents(snapshot: tuple[dict[str, Any], Path]) -> None:
    payload, _ = snapshot
    agents = {agent["key"]: agent for agent in payload["agents"]}
    assert set(agents) == {"IMPL", "REVIEWER"}

    impl = agents["IMPL"]
    assert impl["status"] == "running"
    assert impl["current_session"]["id"] == 1
    assert impl["current_session"]["subject"] == "Build the dashboard"
    assert impl["counts"]["total"] == 1
    assert impl["counts"]["running"] == 1

    reviewer = agents["REVIEWER"]
    assert reviewer["status"] == "idle"
    assert reviewer["current_session"] is None
    assert reviewer["counts"]["completed"] == 1
    assert reviewer["counts"]["declined"] == 1


def test_snapshot_sessions_and_meta(snapshot: tuple[dict[str, Any], Path]) -> None:
    payload, _ = snapshot
    assert [run["id"] for run in payload["active_sessions"]] == [1]
    assert payload["active_sessions"][0]["duration_seconds"] is not None

    sessions = {run["id"]: run for run in payload["recent_sessions"]}
    assert set(sessions) == {2, 3, 4}
    assert sessions[2]["pr_url"] == "https://github.com/owner/repo/pull/1"
    assert sessions[2]["project"] == "demo"
    assert sessions[2]["duration_seconds"] == 42.5
    assert sessions[2]["work_package_url"] == (
        "https://openproject.example/work_packages/78"
    )
    assert sessions[3]["display_id"] == "#79"  # fallback when none was stored
    assert sessions[4]["display_id"] == "#80"
    assert "must-not-leak" not in json.dumps(payload)


def test_snapshot_limit(snapshot: tuple[dict[str, Any], Path]) -> None:
    _, data_dir = snapshot
    payload = DashboardData(_config(), _settings(data_dir)).snapshot(limit=2)
    assert len(payload["recent_sessions"]) == 2


def test_engine_status_stale(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    old = (datetime.now(UTC) - timedelta(minutes=10)).isoformat()
    (tmp_path / "heartbeat").write_text(old + "\n", encoding="utf-8")

    payload = DashboardData(_config(), settings).snapshot()
    assert payload["engine"]["status"] == "stale"


def test_engine_status_paused(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    write_heartbeat(settings)
    (tmp_path / "PAUSE").write_text("", encoding="utf-8")

    payload = DashboardData(_config(), settings).snapshot()
    assert payload["engine"]["status"] == "paused"
    assert payload["engine"]["paused"] is True


def test_engine_status_offline_without_store(tmp_path: Path) -> None:
    payload = DashboardData(_config(), _settings(tmp_path)).snapshot()
    assert payload["engine"]["status"] == "offline"
    assert payload["counts"]["total"] == 0
    assert payload["agents"]
    assert payload["active_sessions"] == []
    assert payload["recent_sessions"] == []


def test_assets_are_packaged() -> None:
    html = _asset("index.html").decode("utf-8")
    assert "Agent Engine" in html
    assert "/api/status" in _asset("app.js").decode("utf-8")
    assert ":root" in _asset("style.css").decode("utf-8")


@pytest.fixture
def server(tmp_path: Path) -> Any:
    settings = _settings(tmp_path)
    write_heartbeat(settings)
    httpd = DashboardHTTPServer(
        ("127.0.0.1", 0), DashboardData(_config(), settings), refresh_seconds=7
    )
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield httpd
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


def _get(httpd: DashboardHTTPServer, path: str) -> tuple[int, str, bytes]:
    host, port = httpd.server_address[:2]
    url = f"http://{host}:{port}{path}"
    try:
        with urllib.request.urlopen(url, timeout=5) as response:
            return response.status, response.headers.get("Content-Type", ""), response.read()
    except urllib.error.HTTPError as error:
        return error.code, error.headers.get("Content-Type", ""), error.read()


def test_http_api(server: DashboardHTTPServer) -> None:
    status, content_type, body = _get(server, "/api/status?limit=5")
    assert status == 200
    assert content_type.startswith("application/json")
    payload = json.loads(body)
    assert payload["refresh_seconds"] == 7
    assert payload["engine"]["status"] == "active"


def test_http_ui_and_assets(server: DashboardHTTPServer) -> None:
    for path, expected in (
        ("/", b"Agent Engine"),
        ("/style.css", b":root"),
        ("/app.js", b"/api/status"),
    ):
        status, _, body = _get(server, path)
        assert status == 200
        assert expected in body


def test_http_health_and_errors(server: DashboardHTTPServer) -> None:
    status, _, body = _get(server, "/healthz")
    assert status == 200
    assert json.loads(body) == {"ok": True}

    status, _, _ = _get(server, "/api/status?limit=nope")
    assert status == 400
    status, _, _ = _get(server, "/api/status?limit=9999")
    assert status == 400
    status, _, _ = _get(server, "/does-not-exist")
    assert status == 404


# -- CLI wiring ------------------------------------------------------------


def _write_workflow(path: Path) -> Path:
    import yaml

    path.write_text(
        yaml.safe_dump(
            {
                "projects": [
                    {
                        "identifier": "demo",
                        "repo": {"github": "owner/repo", "dir": "repo"},
                    }
                ],
                "agents": {
                    "IMPL": {
                        "name": "implementer",
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
                "runners": {"pi": {"argv": ["pi"]}},
            }
        ),
        encoding="utf-8",
    )
    return path


def test_cli_dashboard_port_flag(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from op_agent_engine import cli

    config_path = _write_workflow(tmp_path / "workflow.yaml")
    captured: dict[str, Any] = {}

    def fake_serve(
        config: WorkflowConfig,
        settings: EngineSettings,
        *,
        host: str | None,
        port: int | None,
        limit: int,
        refresh_seconds: int,
    ) -> int:
        captured.update(
            host=host,
            port=port,
            limit=limit,
            refresh_seconds=refresh_seconds,
        )
        return 0

    monkeypatch.setattr(cli, "serve_dashboard", fake_serve)
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))

    assert (
        cli.main(
            [
                "--config",
                str(config_path),
                "dashboard",
                "--host",
                "0.0.0.0",
                "--port",
                "9123",
                "--limit",
                "10",
                "--refresh",
                "5",
            ]
        )
        == 0
    )
    assert captured == {
        "host": "0.0.0.0",
        "port": 9123,
        "limit": 10,
        "refresh_seconds": 5,
    }


def test_cli_dashboard_uses_env_port(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from op_agent_engine import cli

    config_path = _write_workflow(tmp_path / "workflow.yaml")
    captured: dict[str, Any] = {}

    def fake_serve(
        config: WorkflowConfig,
        settings: EngineSettings,
        *,
        host: str | None,
        port: int | None,
        limit: int,
        refresh_seconds: int,
    ) -> int:
        captured.update(host=host, port=port)
        return 0

    monkeypatch.setattr(cli, "serve_dashboard", fake_serve)
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("DASHBOARD_PORT", "9333")
    monkeypatch.setenv("DASHBOARD_HOST", "127.0.0.1")

    assert cli.main(["--config", str(config_path), "dashboard"]) == 0
    # ``None`` lets EngineSettings supply the value from engine.env.
    assert captured == {"host": None, "port": 9333}


def test_cli_dashboard_rejects_invalid_port(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from op_agent_engine import cli

    config_path = _write_workflow(tmp_path / "workflow.yaml")
    called = False

    def fake_serve(*args: object, **kwargs: object) -> int:
        nonlocal called
        called = True
        return 0

    monkeypatch.setattr(cli, "serve_dashboard", fake_serve)
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))

    assert (
        cli.main(["--config", str(config_path), "dashboard", "--port", "70000"])
        == cli.CONFIG_ERROR_EXIT
    )
    assert called is False
