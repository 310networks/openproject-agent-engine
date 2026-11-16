"""Current OpenProject status counts are not historical run outcomes."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from op_agent_engine.openproject.client import OpenProjectClient, OpenProjectError
from op_agent_engine.openproject.resolve import resolve_workflow
from op_agent_engine.work_package_counts import FILE_NAME, read_counts, refresh_counts
from tests.test_dashboard import _config, _settings
from tests.test_poller import wp
from tests.test_resolve import BASE_URL, TOKEN, make_config, make_fake


async def test_counts_use_server_total_and_exact_scope(tmp_path: Path) -> None:
    config = make_config()
    # No agent/type/assignee eligibility restriction: these have no run history.
    fake = make_fake(work_packages=[
        wp(1, status=8), wp(2, status=8, ptype=999, assignee=999),
        wp(3, status=5), wp(4, status=1), wp(5, status=8, proj=999),
    ])
    settings = _settings(tmp_path).model_copy(update={"op_base_url": BASE_URL})
    async with OpenProjectClient(BASE_URL, TOKEN, transport=fake.transport) as client:
        resolution = await resolve_workflow(client, config)
        fake.requests.clear()
        await refresh_counts(client, config, resolution, settings)
        result = read_counts(config, settings, now=datetime.now(UTC))
        assert result["status"] == "fresh"
        assert (result["blocked"], result["on_hold"]) == (2, 1)
        assert result["projects"] == ["demo"]
        assert len(fake.requests) == 2
        for request in fake.requests:
            assert request.method == "GET"
            assert request.url.params["pageSize"] == "1"
            filters = json.loads(request.url.params["filters"])
            assert set().union(*(item.keys() for item in filters)) == {"project", "status"}
        fake.work_packages.clear()
        await refresh_counts(client, config, resolution, settings)
        result = read_counts(config, settings, now=datetime.now(UTC))
        assert (result["blocked"], result["on_hold"]) == (0, 0)


async def test_multiple_enabled_projects_excludes_disabled(tmp_path: Path) -> None:
    config = make_config()
    fake = make_fake(work_packages=[
        wp(1, status=8), wp(2, status=8, proj=9), wp(3, status=5, proj=10),
    ])
    settings = _settings(tmp_path).model_copy(update={"op_base_url": BASE_URL})
    async with OpenProjectClient(BASE_URL, TOKEN, transport=fake.transport) as client:
        resolution = await resolve_workflow(client, config)
        base = config.projects[0]
        config.projects.extend([
            base.model_copy(update={"identifier": "second"}),
            base.model_copy(update={"identifier": "disabled", "enabled": False}),
        ])
        resolution.projects["second"] = replace(resolution.projects["demo"], id=9)
        resolution.projects["disabled"] = replace(resolution.projects["demo"], id=10)
        await refresh_counts(client, config, resolution, settings)
    result = read_counts(config, settings, now=datetime.now(UTC))
    assert result["status"] == "fresh"
    assert result["projects"] == ["demo", "second"]
    assert (result["blocked"], result["on_hold"]) == (2, 0)


@pytest.mark.parametrize("bad", [None, -1, True, "3", 1.5])
async def test_malformed_total_does_not_publish_partial_counts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, bad: object
) -> None:
    config = make_config()
    fake = make_fake()
    settings = _settings(tmp_path).model_copy(update={"op_base_url": BASE_URL})
    async with OpenProjectClient(BASE_URL, TOKEN, transport=fake.transport) as client:
        resolution = await resolve_workflow(client, config)
        monkeypatch.setattr(
            client, "get_json", AsyncMock(side_effect=[{"total": 2}, {"total": bad}])
        )
        await refresh_counts(client, config, resolution, settings)
    result = read_counts(config, settings, now=datetime.now(UTC))
    assert result["status"] == "unavailable"
    assert result["blocked"] is result["on_hold"] is None
    assert result["observed_at"] is None
    assert result["checked_at"] is not None


async def test_failure_replaces_previous_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = make_config()
    fake = make_fake()
    settings = _settings(tmp_path).model_copy(update={"op_base_url": BASE_URL})
    async with OpenProjectClient(BASE_URL, TOKEN, transport=fake.transport) as client:
        resolution = await resolve_workflow(client, config)
        await refresh_counts(client, config, resolution, settings)
        monkeypatch.setattr(client, "get_json", AsyncMock(side_effect=OpenProjectError("secret")))
        await refresh_counts(client, config, resolution, settings)
    result = read_counts(config, settings, now=datetime.now(UTC))
    assert result["status"] == "unavailable"
    assert result["blocked"] is result["on_hold"] is None
    assert "secret" not in (tmp_path / FILE_NAME).read_text()


def observation(now: datetime) -> dict:
    return {
        "source": "https://openproject.example",
        "projects": ["demo"],
        "checked_at": now.isoformat(),
        "observed_at": now.isoformat(),
        "blocked": 2,
        "on_hold": 0,
    }


def test_read_fresh_stale_and_missing(tmp_path: Path) -> None:
    config, settings = _config(), _settings(tmp_path)
    now = datetime.now(UTC)
    assert read_counts(config, settings, now=now)["status"] == "unavailable"
    (tmp_path / FILE_NAME).write_text(json.dumps(observation(now)))
    assert read_counts(config, settings, now=now)["blocked"] == 2
    result = read_counts(config, settings, now=now + timedelta(seconds=136))
    assert result["status"] == "stale"
    assert result["blocked"] is result["on_hold"] is None
    assert result["observed_at"] == now.isoformat()
    assert result["age_seconds"] == 136


@pytest.mark.parametrize("patch", [
    {"source": "https://another.example"}, {"projects": ["another"]},
    {"blocked": -1}, {"blocked": True}, {"on_hold": None},
    {"checked_at": "garbage"}, {"observed_at": "2020-01-01"},
    {"projects": None},
])
def test_invalid_snapshot_is_unavailable(tmp_path: Path, patch: dict) -> None:
    now = datetime.now(UTC)
    (tmp_path / FILE_NAME).write_text(json.dumps(observation(now) | patch))
    result = read_counts(_config(), _settings(tmp_path), now=now)
    assert result["status"] == "unavailable"
    assert result["blocked"] is result["on_hold"] is None


@pytest.mark.parametrize("text", ["{", "[]", "null"])
def test_corrupt_snapshot_is_unavailable(tmp_path: Path, text: str) -> None:
    (tmp_path / FILE_NAME).write_text(text)
    result = read_counts(_config(), _settings(tmp_path), now=datetime.now(UTC))
    assert result["status"] == "unavailable"
