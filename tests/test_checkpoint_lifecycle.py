"""Delivery ends an episode; inactive tickets cannot retain provider-poll loops."""

from contextlib import closing
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest

from op_agent_engine.checkpoints import build_resume_gate
from op_agent_engine.config import EngineSettings
from op_agent_engine.github import ChecksSummary
from op_agent_engine.openproject.client import (
    OpenProjectClient,
    OpenProjectError,
    OpenProjectNotFoundError,
)
from op_agent_engine.store import Store
from tests.test_checkpoints import FakeChecks, _hold


def test_delivery_retires_episode_without_replaying_milestones_or_clearing_other_tasks() -> None:
    with closing(Store(":memory:")) as store:
        store.record_checkpoint_progress(100, head_sha="a", milestone="endpoint implemented")
        store.record_checkpoint_progress(100, head_sha="b")
        store.claim_ci_diagnosis(100, "b", "Unit tests")
        _hold(store, ready_in_seconds=1)
        store.record_checkpoint_progress(200, head_sha="other")
        store.complete_checkpoint_episode(100)
        assert store.checkpoint_progress(100) == {}
        assert store.latest_ci_diagnosis(100) is None
        assert store.resume_hold(100) is None
        assert store.checkpoint_progress(200)["revisions"] == 1
        # Fresh fix work gets a new budget, but old milestones are still known.
        assert store.record_checkpoint_progress(
            100, head_sha="c", milestone="endpoint implemented"
        ) == (0, 1)


@pytest.mark.parametrize("status_id", [20, 30, 40, 50, None])
async def test_gate_checks_configured_active_status_before_polling_ci(
    status_id: int | None,
) -> None:
    client = AsyncMock(spec=OpenProjectClient)
    client.get_json.return_value = (
        {"_links": {"status": {"href": f"/api/v3/statuses/{status_id}"}}}
        if status_id is not None else {}
    )
    checks = FakeChecks(ChecksSummary(state="pending", total=1, pending=1))
    now = datetime.now(UTC) + timedelta(seconds=5)
    with closing(Store(":memory:")) as store:
        _hold(store, ready_in_seconds=1)
        gate = build_resume_gate(
            store, EngineSettings(_env_file=None), checks,
            client=client, active_status_ids=frozenset({20, 30}),
        )
        report = await gate.refresh(now=now)
        client.get_json.assert_awaited_once_with("/api/v3/work_packages/100")
        if status_id in {40, 50}:
            assert report.cleared and not report.extended
            assert store.resume_hold(100) is None
            await gate.refresh(now=now + timedelta(seconds=600))
            assert client.get_json.await_count == 1
            assert checks.calls == []
        else:
            assert report.extended and not report.cleared
            assert store.resume_hold(100) is not None
            assert bool(checks.calls) == (status_id in {20, 30})


@pytest.mark.parametrize("missing", [False, True])
async def test_status_provider_failure_stays_passive_but_missing_ticket_retires_hold(
    missing: bool,
) -> None:
    client = AsyncMock(spec=OpenProjectClient)
    client.get_json.side_effect = (
        OpenProjectNotFoundError("missing", url="https://example.org/api/v3/work_packages/100")
        if missing else OpenProjectError("temporarily unavailable")
    )
    checks = FakeChecks(ChecksSummary(state="pending", total=1, pending=1))
    with closing(Store(":memory:")) as store:
        _hold(store, ready_in_seconds=1)
        gate = build_resume_gate(
            store, EngineSettings(_env_file=None), checks,
            client=client, active_status_ids=frozenset({20, 30}),
        )
        report = await gate.refresh(now=datetime.now(UTC) + timedelta(seconds=5))
        assert bool(report.cleared) == missing
        assert bool(report.extended) != missing
        assert (store.resume_hold(100) is None) == missing
        assert checks.calls == []


def test_client_requires_explicit_resolved_active_status_ids() -> None:
    with closing(Store(":memory:")) as store, pytest.raises(ValueError, match="active status IDs"):
        build_resume_gate(
            store, EngineSettings(_env_file=None), FakeChecks(),
            client=AsyncMock(spec=OpenProjectClient),
        )
