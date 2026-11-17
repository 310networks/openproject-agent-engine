"""Progress is not the CI poll count; keep convergence and evidence honesty."""

from __future__ import annotations

import json
from contextlib import closing
from pathlib import Path

import pytest

from op_agent_engine.continuation import checkpoint_record, compact_report
from op_agent_engine.github import ChecksSummary
from op_agent_engine.store import Store
from tests.test_dispatcher import (
    STATUS_IN_PROGRESS,
    STATUS_ON_HOLD,
    FakeGitHub,
    FakeRunner,
    claim,
    make_dispatcher,
    make_item,
    make_result,
    posted_comments,
    status_href,
    wp,
)


def test_productive_revisions_do_not_accumulate_stagnation() -> None:
    with closing(Store(":memory:")) as store:
        for n in range(12):
            assert store.record_checkpoint_progress(1, head_sha=f"sha-{n}") == (0, n + 1)
        assert store.record_checkpoint_progress(1, head_sha="sha-11") == (1, 12)
        assert store.record_checkpoint_progress(1, head_sha="sha-11") == (2, 12)


def test_milestone_must_be_new_to_reset_convergence() -> None:
    with closing(Store(":memory:")) as store:
        assert store.record_checkpoint_progress(1, head_sha="one") == (0, 1)
        for sha, milestone, expected in (
            ("two", "endpoint implemented", (0, 0)),
            ("three", "endpoint implemented", (0, 1)),
            ("four", "migration implemented", (0, 0)),
            ("five", "endpoint implemented", (0, 1)),
        ):
            observed = store.record_checkpoint_progress(1, head_sha=sha, milestone=milestone)
            assert observed == expected


def test_progress_survives_restart_and_missing_source_never_proves_progress(tmp_path: Path) -> None:
    path = tmp_path / "engine.db"
    with closing(Store(path)) as store:
        assert store.record_checkpoint_progress(1, head_sha=None) == (1, 0)
        assert store.record_checkpoint_progress(1, head_sha="one") == (0, 1)
    with closing(Store(path)) as store:
        assert store.record_checkpoint_progress(1, head_sha="one") == (1, 1)
        store.reset_checkpoint_streak(1)
        assert store.record_checkpoint_progress(1, head_sha="two") == (0, 1)


def test_next_action_survives_legacy_report_clipping() -> None:
    state = {
        "source_sha": "abc",
        "next_action": "Read unit test failure; fix request validation",
        "completed_milestone": "endpoint implemented",
        "evidence": "https://example.org/run/1",
    }
    report = "old history\n" * 1000 + "```continuation\n" + json.dumps(state) + "\n```"
    brief = compact_report(report)
    assert len(brief) <= 4000
    assert state["next_action"] in brief[:1500]
    assert "source_sha: abc" in brief
    assert "truncated" in brief
    assert checkpoint_record(report) == state


@pytest.mark.parametrize(
    "payload",
    ["[]", "null", "invalid", '{"next_action": 2}', '{"source_sha":"abc","next_action":false}'],
)
def test_invalid_checkpoint_records_cannot_claim_milestones(payload: str) -> None:
    assert checkpoint_record("```continuation\n" + payload + "\n```") == {}


async def test_many_productive_checkpoints_still_get_fresh_passive_poll_budget(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = FakeRunner(
        make_result(tmp_path, result_line="RESULT: WAITING_CI", stdout="RESULT: WAITING_CI\n")
    )
    dispatcher, fake, store, client, _ = await make_dispatcher(
        tmp_path,
        monkeypatch,
        runner=runner,
        gh=FakeGitHub(checks=ChecksSummary(state="pending", total=2, pending=1)),
        work_packages=[wp(100, status=STATUS_IN_PROGRESS, agent=11)],
        settings_overrides={"max_checkpoint_waits": 1},
    )
    try:
        for n in range(12):
            store.record_checkpoint_progress(100, head_sha=f"old-{n}")
        claimed = claim(store, make_item(100), kind="resume")
        await dispatcher.execute(claimed)
        hold = store.resume_hold(100)
        assert hold is not None and hold.attempts == 0
        assert status_href(fake, 100) == f"/api/v3/statuses/{STATUS_IN_PROGRESS}"
        assert not any("cap reached" in c for c in posted_comments(fake, 100))
    finally:
        await client.aclose()
        store.close()


async def test_source_churn_without_milestone_requests_one_strategy_review(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = FakeRunner(
        make_result(tmp_path, result_line="RESULT: WAITING_CI", stdout="RESULT: WAITING_CI\n")
    )
    dispatcher, fake, store, client, _ = await make_dispatcher(
        tmp_path,
        monkeypatch,
        runner=runner,
        gh=FakeGitHub(checks=ChecksSummary(state="pending", total=1, pending=1)),
        work_packages=[wp(100, status=STATUS_IN_PROGRESS, agent=11)],
        settings_overrides={"max_checkpoint_revisions": 2},
    )
    try:
        store.record_checkpoint_progress(100, head_sha="one")
        store.record_checkpoint_progress(100, head_sha="two")
        claimed = claim(store, make_item(100), kind="resume")
        await dispatcher.execute(claimed)
        assert status_href(fake, 100) == f"/api/v3/statuses/{STATUS_ON_HOLD}"
        assert "source revisions" in posted_comments(fake, 100)[0]
        assert "strategy" in posted_comments(fake, 100)[0]
        assert store.resume_hold(100) is None
    finally:
        await client.aclose()
        store.close()
