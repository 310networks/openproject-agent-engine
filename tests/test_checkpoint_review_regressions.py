"""Independent-review regressions: unhold, unknown identity and truthful progress."""

from contextlib import closing
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from op_agent_engine.config import EngineSettings
from op_agent_engine.github import ChecksSummary
from op_agent_engine.outcomes import OutcomeDecision
from op_agent_engine.store import SUCCEEDED, Store
from tests.test_dispatcher import (
    PR,
    PRIORITIES,
    STATUS_IN_PROGRESS,
    STATUS_IN_TESTING,
    STATUS_ON_HOLD,
    FakeGitHub,
    FakeRunner,
    claim,
    make_dispatcher,
    make_fake,
    make_item,
    make_result,
    make_review_item,
    status_change,
    status_href,
    wp,
)


async def test_cap_then_human_unhold_resets_new_budgets_once_not_every_resume(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = make_fake(
        work_packages=[wp(100, status=STATUS_IN_PROGRESS, agent=11)],
        priorities=PRIORITIES,
        activities=[
            status_change(100, "On Hold", previous="In Progress", activity_id=900),
            status_change(100, "In Progress", previous="On Hold", activity_id=901),
        ],
    )
    runner = FakeRunner(
        make_result(tmp_path, result_line="RESULT: WAITING_CI", stdout="RESULT: WAITING_CI\n")
    )
    dispatcher, fake, store, client, _ = await make_dispatcher(
        tmp_path,
        monkeypatch,
        runner=runner,
        fake=fake,
        gh=FakeGitHub(checks=ChecksSummary(state="success", total=1)),
        settings_overrides={"max_checkpoint_waits": 1},
    )
    try:
        previous = claim(store, make_item(100), kind="resume")
        store.finish_run(previous.run_id, state=SUCCEEDED, outcome="checkpoint_wait_cap_reached")
        store.record_checkpoint_progress(100, head_sha="unchanged")
        for _ in range(5):
            store.record_checkpoint_progress(100, head_sha="unchanged")
        store.claim_ci_diagnosis(100, "unchanged", "Unit tests")
        for iteration in range(3):
            current = claim(store, make_item(100), kind="resume")
            await dispatcher.execute(current)
            if iteration < 2:
                assert status_href(fake, 100) == f"/api/v3/statuses/{STATUS_IN_PROGRESS}"
            else:
                assert status_href(fake, 100) == f"/api/v3/statuses/{STATUS_ON_HOLD}"
        assert store.latest_ci_diagnosis(100) is None
    finally:
        await client.aclose()
        store.close()


@pytest.mark.parametrize("kind", ["review", "resume"])
async def test_unhold_reset_precedes_review_and_open_pr_short_circuit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
) -> None:
    reviewing = kind == "review"
    item = make_review_item(100) if reviewing else make_item(100)
    status = STATUS_IN_TESTING if reviewing else STATUS_IN_PROGRESS
    fake = make_fake(
        work_packages=[wp(100, status=status, agent=22 if reviewing else 11)],
        priorities=PRIORITIES,
        activities=[
            status_change(100, "On Hold", previous="In Progress", activity_id=900),
            status_change(
                100, "Ready for Review" if reviewing else "New", previous="On Hold", activity_id=901
            ),
            status_change(
                100,
                "In Testing" if reviewing else "In Progress",
                previous="Ready for Review" if reviewing else "New",
                activity_id=902,
            ),
        ],
    )
    runner = FakeRunner(
        make_result(tmp_path, result_line="RESULT: WAITING_CI", stdout="RESULT: WAITING_CI\n")
    )
    dispatcher, _, store, client, _ = await make_dispatcher(
        tmp_path,
        monkeypatch,
        runner=runner,
        fake=fake,
        gh=FakeGitHub(
            pr=replace(PR, head_oid="unchanged"), checks=ChecksSummary(state="success", total=1)
        ),
        settings_overrides={"max_checkpoint_waits": 1},
    )
    try:
        previous = claim(store, item, kind=kind)
        store.finish_run(previous.run_id, state=SUCCEEDED, outcome="checkpoint_wait_cap_reached")
        for _ in range(6):
            store.record_checkpoint_progress(100, head_sha="unchanged")
        store.claim_ci_diagnosis(100, "unchanged", "Unit tests")
        current = claim(store, item, kind=kind)
        await dispatcher.execute(current)
        observed = store.get_run(current.run_id)
        assert observed is not None
        assert observed.outcome == ("waiting_ci" if reviewing else "ready_for_review_existing_pr")
        assert store.latest_ci_diagnosis(100) is None
        assert store.checkpoint_progress(100).get("stagnant", 0) == 0
    finally:
        await client.aclose()
        store.close()


async def test_headless_pending_checkpoint_is_bounded_not_a_pollable_hold(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dispatcher, _, store, client, _ = await make_dispatcher(
        tmp_path,
        monkeypatch,
        runner=FakeRunner(make_result(tmp_path)),
        settings_overrides={"max_checkpoint_waits": 1},
    )
    try:
        monkeypatch.setattr(
            dispatcher,
            "_commit_checks",
            AsyncMock(return_value=ChecksSummary(state="pending", total=1, pending=1)),
        )
        claimed = claim(store, make_item(100), kind="resume")
        decision = OutcomeDecision(run_state=SUCCEEDED, outcome="waiting_ci")
        for _ in range(2):
            observed, _ = await dispatcher._apply_checkpoint_policy(
                claimed, decision, pr=None, heads={}, result=make_result(tmp_path)
            )
        assert observed.transition == "on_hold"
        assert observed.outcome == "checkpoint_wait_cap_reached"
        assert store.resume_hold(100) is None
    finally:
        await client.aclose()
        store.close()


def test_pending_observations_and_sentinel_milestones_cannot_erase_real_stagnation() -> None:
    with closing(Store(":memory:")) as store:
        assert store.record_checkpoint_progress(1, head_sha="a") == (0, 1)
        assert store.record_checkpoint_progress(1, head_sha="a", waiting=True) == (0, 1)
        assert store.record_checkpoint_progress(1, head_sha="a", milestone="new name") == (1, 1)
        assert store.record_checkpoint_progress(
            1, head_sha="b", milestone="omit unless newly completed"
        ) == (0, 2)


def test_hold_clock_preserves_same_source_but_resets_new_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = iter(
        ["2026-11-17T00:00:00+00:00", "2026-11-17T00:01:00+00:00", "2026-11-17T00:02:00+00:00"]
    )
    monkeypatch.setattr("op_agent_engine.store._utcnow", lambda: next(clock))
    with closing(Store(":memory:")) as store:
        a = store.set_resume_hold(
            1, repo="owner/repo", head_sha="a", ready_at="later", reason="pending"
        )
        b = store.set_resume_hold(
            1, repo="owner/repo", head_sha="b", ready_at="later", reason="pending"
        )
        same = store.set_resume_hold(
            1, repo="owner/repo", head_sha="b", ready_at="later", reason="pending"
        )
        assert a.created_at == "2026-11-17T00:00:00+00:00"
        assert b.created_at == "2026-11-17T00:01:00+00:00"
        assert same.created_at == b.created_at
        assert same.updated_at == "2026-11-17T00:02:00+00:00"


def test_operator_actionable_profile_is_json_env_not_pr_data(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CI_ACTIONABLE_CHECKS", '{"owner/repo":["Unit tests","Integration tests"]}')
    settings = EngineSettings(_env_file=None)
    assert settings.ci_actionable_checks == {"owner/repo": ("Unit tests", "Integration tests")}
    monkeypatch.setenv("CI_ACTIONABLE_CHECKS", "[]")
    with pytest.raises(ValueError):
        EngineSettings(_env_file=None)
