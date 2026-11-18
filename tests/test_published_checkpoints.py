"""Published CI identity is independent of retained, unpublished local progress."""

import json
import sqlite3
from contextlib import closing
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from op_agent_engine.checkpoints import ResumeGate
from op_agent_engine.github import CheckResult, ChecksSummary, GitHubError, PullRequest
from op_agent_engine.outcomes import OutcomeDecision
from op_agent_engine.store import FAILED, SUCCEEDED, Store
from tests.test_dispatcher import (
    FakeGitHub,
    FakeRunner,
    claim,
    make_dispatcher,
    make_item,
    make_result,
    posted_comments,
)

PUBLISHED_PR = PullRequest(
    number=17, url="https://example.org/pulls/17", head_branch="op-4-100",
    state="open", is_draft=True, head_oid="published-source",
)
PENDING = ChecksSummary(state="pending", total=2, pending=1)


def gate(store: Store, provider: FakeGitHub) -> ResumeGate:
    return ResumeGate(
        store=store, checks=provider, wait_seconds=60, max_attempts=1,
        actionable_checks={"owner/demo": ("Unit tests",)},
    )


def due(store: Store) -> datetime:
    hold = store.resume_hold(100)
    assert hold is not None
    return datetime.fromisoformat(hold.ready_at) + timedelta(seconds=1)


async def test_dispatcher_retains_local_progress_but_gate_polls_published_head_after_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    provider = FakeGitHub(pr=PUBLISHED_PR, checks=PENDING)
    dispatcher, fake, store, client, _ = await make_dispatcher(
        tmp_path, monkeypatch, gh=provider,
        runner=FakeRunner(make_result(
            tmp_path, result_line="RESULT: WAITING_CI https://example.org/runs/unrelated",
            stdout="RESULT: WAITING_CI https://example.org/runs/unrelated\n",
        )),
    )
    try:
        monkeypatch.setattr(
            "op_agent_engine.dispatcher.branch_snapshot",
            AsyncMock(return_value=("local-only-source", True)),
        )
        with caplog.at_level("INFO"):
            claimed = claim(store, make_item(100), kind="resume")
            await dispatcher.execute(claimed)
        run = store.get_run(claimed.run_id)
        assert run is not None and run.outcome == "waiting_ci"
        meta = json.loads((Path(run.run_dir) / "meta.json").read_text())
        assert meta["worktree_checkpoint"] and meta["worktree_kept"]
        assert meta["heads"]["branch_after"] == "local-only-source"
        assert meta["heads"]["ci_head"] == PUBLISHED_PR.head_oid
        assert meta["heads"]["ci_local_head_mismatch"] is True
        assert store.checkpoint_progress(100)["head_sha"] == "local-only-source"
        comment = posted_comments(fake, 100)[0]
        assert "Checks for published-so" in comment
        assert "Retained local head: local-only-s" in comment
        assert any(getattr(r, "polled_head_sha", None) == PUBLISHED_PR.head_oid
                   and getattr(r, "local_head_sha", None) == "local-only-source"
                   for r in caplog.records)
        assert provider.checks_calls == [("owner/demo", PUBLISHED_PR.head_oid)]
    finally:
        await client.aclose()
        store.close()

    # Repeated engine polls after a restart never spend a new model session,
    # even beyond the warning threshold, nor query the retained local commit.
    with closing(Store(tmp_path / "engine.db")) as reopened:
        for _ in range(4):
            report = await gate(reopened, provider).refresh(now=due(reopened))
            assert report.extended and not report.cleared
        assert len(provider.checks_calls) == 5
        assert {sha for _, sha in provider.checks_calls} == {PUBLISHED_PR.head_oid}
        hold = reopened.resume_hold(100)
        assert hold is not None and hold.pr_branch == PUBLISHED_PR.head_branch


@pytest.mark.parametrize("with_pr", [False, True])
async def test_ambiguous_identity_never_polls_local_or_reported_sha_and_is_bounded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, with_pr: bool,
) -> None:
    provider = FakeGitHub(checks=PENDING)
    dispatcher, _, store, client, _ = await make_dispatcher(
        tmp_path, monkeypatch, gh=provider, runner=FakeRunner(make_result(tmp_path)),
        settings_overrides={"max_checkpoint_waits": 1},
    )
    try:
        claimed = claim(store, make_item(100), kind="resume")
        for n in range(2):
            decision, _ = await dispatcher._apply_checkpoint_policy(
                claimed, OutcomeDecision(run_state=SUCCEEDED, outcome="waiting_ci"),
                pr=replace(PUBLISHED_PR, head_oid=None) if with_pr else None,
                heads={"branch_after": f"unpublished-{n}", "pr_after": "untrusted-cache"},
                result=make_result(tmp_path, result_line="RESULT: WAITING_CI https://example.org/x"),
            )
        assert decision.outcome == "checkpoint_wait_cap_reached"
        assert decision.transition == "on_hold"
        assert provider.checks_calls == []
        assert store.resume_hold(100) is None
        assert store.checkpoint_progress(100)["stagnant"] == 2
    finally:
        await client.aclose()
        store.close()


@pytest.mark.parametrize(
    "outcome", ["waiting_ci", "progress_not_finished", "checkpoint_no_progress"],
)
@pytest.mark.parametrize("state", ["success", "failure", "none", "unavailable"])
async def test_explicit_unactionable_states_are_not_provider_unavailability(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, outcome: str, state: str,
) -> None:
    provider = FakeGitHub(
        checks=ChecksSummary(
            state=state, total=int(state != "none"), failed=int(state == "failure"),
        ),
        checks_error=GitHubError("provider unavailable") if state == "unavailable" else None,
    )
    dispatcher, _, store, client, _ = await make_dispatcher(
        tmp_path, monkeypatch, gh=provider, runner=FakeRunner(make_result(tmp_path)),
    )
    try:
        decision, kept = await dispatcher._apply_checkpoint_policy(
            claim(store, make_item(100)), OutcomeDecision(run_state=SUCCEEDED, outcome=outcome),
            pr=PUBLISHED_PR, heads={"branch_after": "local-only-source"},
            result=make_result(tmp_path),
        )
        expected_hold = ((state == "unavailable" and outcome != "progress_not_finished")
                         or (state == "none" and outcome == "waiting_ci"))
        assert (store.resume_hold(100) is not None) == expected_hold
        if outcome == "checkpoint_no_progress" and state != "unavailable":
            assert decision.run_state == FAILED and decision.outcome == "no_pr"
            assert decision.crash_retry and not kept
        else:
            assert decision.outcome == outcome and kept
        assert provider.checks_calls == [("owner/demo", PUBLISHED_PR.head_oid)]
    finally:
        await client.aclose()
        store.close()


@pytest.mark.parametrize("late_state", ["none", "pending", "success", "failure", "unavailable"])
async def test_registration_grace_is_bounded_and_restart_safe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, late_state: str,
) -> None:
    provider = FakeGitHub(pr=PUBLISHED_PR, checks=ChecksSummary(state="none"))
    dispatcher, _, store, client, _ = await make_dispatcher(
        tmp_path, monkeypatch, gh=provider, runner=FakeRunner(make_result(tmp_path)),
        settings_overrides={"max_checkpoint_waits": 1},
    )
    claimed = claim(store, make_item(100), kind="resume")
    try:
        decision, _ = await dispatcher._apply_checkpoint_policy(
            claimed, OutcomeDecision(run_state=SUCCEEDED, outcome="waiting_ci"),
            pr=PUBLISHED_PR, heads={"branch_after": "local-only-source"},
            result=make_result(tmp_path),
        )
        assert "one passive grace" in (decision.comment or "")
        assert store.resume_hold(100) is not None
    finally:
        store.close()

    with closing(Store(tmp_path / "engine.db")) as reopened:
        try:
            provider.checks = ChecksSummary(state=late_state, total=1)
            provider.checks_error = (GitHubError("unavailable")
                                     if late_state == "unavailable" else None)
            report = await gate(reopened, provider).refresh(now=due(reopened))
            assert bool(report.extended) == (late_state in {"pending", "unavailable"})
            assert bool(report.cleared) == (late_state not in {"pending", "unavailable"})
            # If checks eventually disappear, a hold cannot wait forever on none.
            if reopened.resume_hold(100) is not None:
                provider.checks = ChecksSummary(state="none")
                provider.checks_error = None
                assert (await gate(reopened, provider).refresh(now=due(reopened))).cleared
            # A new model report at the same SHA cannot buy another grace.
            dispatcher._store = reopened
            provider.checks = ChecksSummary(state="none")
            for _ in range(2):
                decision, _ = await dispatcher._apply_checkpoint_policy(
                    claimed, OutcomeDecision(run_state=SUCCEEDED, outcome="waiting_ci"),
                    pr=PUBLISHED_PR, heads={"branch_after": "local-only-source"},
                    result=make_result(tmp_path),
                )
                assert reopened.resume_hold(100) is None
            assert decision.outcome == "checkpoint_wait_cap_reached"
        finally:
            await client.aclose()


@pytest.mark.parametrize("source", ["local-only-source", "published-source"])
async def test_only_provider_source_can_identify_milestone_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, source: str,
) -> None:
    provider = FakeGitHub(checks=PENDING)
    dispatcher, _, store, client, _ = await make_dispatcher(
        tmp_path, monkeypatch, gh=provider, runner=FakeRunner(make_result(tmp_path)),
    )
    try:
        report = "```continuation\n" + json.dumps({
            "next_action": "inspect current checks", "source_sha": source,
            "completed_milestone": "request validator implemented",
            "evidence": "https://example.org/runs/17",
        }) + "\n```"
        monkeypatch.setattr(dispatcher, "_agent_report_text", AsyncMock(return_value=report))
        store.record_checkpoint_progress(100, head_sha="previous-local")
        current = claim(store, make_item(100))
        await dispatcher._apply_checkpoint_policy(
            current,
            OutcomeDecision(run_state=SUCCEEDED, outcome="waiting_ci"),
            pr=PUBLISHED_PR, heads={"branch_after": "local-only-source"},
            result=make_result(tmp_path),
        )
        assert store.checkpoint_progress(100)["revisions"] == (
            0 if source == PUBLISHED_PR.head_oid else 2
        )
        if source == PUBLISHED_PR.head_oid:
            for i in range(1, 4):
                monkeypatch.setattr(dispatcher, "_agent_report_text", AsyncMock(
                    return_value=report.replace("request validator implemented", f"step-{i}"),
                ))
                await dispatcher._apply_checkpoint_policy(
                    current,
                    OutcomeDecision(run_state=SUCCEEDED, outcome="waiting_ci"),
                    pr=PUBLISHED_PR, heads={"branch_after": f"local-{i}"},
                    result=make_result(tmp_path),
                )
                assert store.checkpoint_progress(100)["revisions"] == i
    finally:
        await client.aclose()
        store.close()


def test_local_commits_cannot_launder_unchanged_published_milestone_after_restart(
    tmp_path: Path,
) -> None:
    path = tmp_path / "engine.db"
    for index in range(6):
        with closing(Store(path)) as store:
            assert store.record_checkpoint_progress(
                100, head_sha=f"retained-{index}", published_head="published-a",
                milestone=f"step-{index}", waiting=True,
            ) == (0, index)
    # Publication catches up without another local edit: fresh source can count.
    with closing(Store(path)) as store:
        assert store.record_checkpoint_progress(
            100, head_sha="retained-5", published_head="retained-5",
            milestone="published result", waiting=True,
        ) == (0, 0)


async def test_new_remote_head_gets_current_checks_and_once_only_diagnosis_across_restart(
    tmp_path: Path,
) -> None:
    provider = FakeGitHub(pr=replace(PUBLISHED_PR, head_oid="new-published-source"), checks=PENDING)
    path = tmp_path / "engine.db"
    with closing(Store(path)) as store:
        store.set_resume_hold(
            100, repo="owner/demo", head_sha=PUBLISHED_PR.head_oid,
            pr_branch=PUBLISHED_PR.head_branch, ready_at="2026-01-01T00:00:00+00:00",
            reason="pending", diagnosis_allowed=True,
        )
        report = await gate(store, provider).refresh(now=due(store))
        assert report.extended and not report.cleared
        assert provider.checks_calls == [("owner/demo", "new-published-source")]
        provider.checks = ChecksSummary(
            state="pending", total=2, pending=1, failed=1,
            checks=(CheckResult("Unit tests", "failure", "https://example.org/runs/18"),),
        )
        assert (await gate(store, provider).refresh(now=due(store))).cleared
        assert store.latest_ci_diagnosis(100) == {
            "head_sha": "new-published-source", "fingerprint": "Unit tests",
        }
        store.set_resume_hold(
            100, repo="owner/demo", head_sha="new-published-source",
            pr_branch=PUBLISHED_PR.head_branch, ready_at="2026-01-01T00:00:00+00:00",
            reason="pending", diagnosis_allowed=True,
        )
    with closing(Store(path)) as store:
        assert (await gate(store, provider).refresh(now=due(store))).extended
        assert store.resume_hold(100) is not None
        provider.checks = ChecksSummary(state="failure", total=2, failed=1)
        assert (await gate(store, provider).refresh(now=due(store))).cleared


@pytest.mark.parametrize("missing", ["pr", "sha", "unavailable"])
async def test_gate_does_not_poll_stale_sha_when_current_pr_identity_is_unknown(
    missing: str,
) -> None:
    provider = FakeGitHub(
        pr=(replace(PUBLISHED_PR, head_oid=None) if missing == "sha" else None),
        error=GitHubError("provider unavailable") if missing == "unavailable" else None,
        checks=PENDING,
    )
    with closing(Store(":memory:")) as store:
        store.set_resume_hold(
            100, repo="owner/demo", head_sha=PUBLISHED_PR.head_oid,
            pr_branch=PUBLISHED_PR.head_branch, ready_at="2026-01-01T00:00:00+00:00",
            reason="pending",
        )
        report = await gate(store, provider).refresh(now=due(store))
        assert bool(report.extended) == (missing == "unavailable")
        assert bool(report.cleared) == (missing != "unavailable")
        assert provider.checks_calls == []


async def test_new_remote_head_registration_grace_is_not_renewed_on_restart(tmp_path: Path) -> None:
    path = tmp_path / "engine.db"
    provider = FakeGitHub(
        pr=replace(PUBLISHED_PR, head_oid="new-published-source"),
        checks=ChecksSummary(state="none"),
    )
    with closing(Store(path)) as store:
        store.set_resume_hold(
            100, repo="owner/demo", head_sha=PUBLISHED_PR.head_oid,
            pr_branch=PUBLISHED_PR.head_branch, ready_at="2026-01-01T00:00:00+00:00",
            reason="pending",
        )
        assert (await gate(store, provider).refresh(now=due(store))).extended
        assert not store.claim_checks_registration(100, "owner/demo", "new-published-source")
    with closing(Store(path)) as store:
        assert (await gate(store, provider).refresh(now=due(store))).cleared
        assert store.resume_hold(100) is None
        assert {sha for _, sha in provider.checks_calls} == {"new-published-source"}


async def test_legacy_hold_migrates_without_trusting_its_possibly_unpublished_sha(
    tmp_path: Path,
) -> None:
    path = tmp_path / "engine.db"
    with closing(Store(path)) as store:
        store.set_resume_hold(
            100, repo="owner/demo", head_sha="legacy-local-source",
            ready_at="2026-01-01T00:00:00+00:00", reason="pending", diagnosis_allowed=False,
        )
    # Emulate the previous schema without losing the existing hold.
    with sqlite3.connect(path) as connection:
        connection.execute("ALTER TABLE resume_holds DROP COLUMN pr_branch")
        connection.execute("DROP TABLE checkpoint_registrations")
    with closing(Store(path, read_only=True)) as reader:
        hold = reader.resume_hold(100)
        assert hold is not None and hold.pr_branch is None
    provider = FakeGitHub(pr=PUBLISHED_PR, checks=PENDING)
    with closing(Store(path)) as store:
        hold = store.resume_hold(100)
        assert hold is not None and hold.head_sha == "legacy-local-source"
        assert not hold.diagnosis_allowed and hold.pr_branch is None
        report = await gate(store, provider).refresh(now=due(store))
        assert report.cleared and not report.extended
        assert provider.checks_calls == [] and provider.calls == []
        assert store.claim_checks_registration(100, "owner/demo", "published-source")
    # Opening again is idempotent and preserves the new durable receipt.
    with closing(Store(path)) as store:
        assert not store.claim_checks_registration(100, "owner/demo", "published-source")


@pytest.mark.parametrize("state", ["success", "failure"])
async def test_new_remote_head_uses_its_settled_verdict_not_old_pending_checks(state: str) -> None:
    provider = FakeGitHub(
        pr=replace(PUBLISHED_PR, head_oid="new-published-source"),
        checks=ChecksSummary(state=state, total=1, failed=int(state == "failure")),
    )
    with closing(Store(":memory:")) as store:
        store.set_resume_hold(
            100, repo="owner/demo", head_sha=PUBLISHED_PR.head_oid,
            pr_branch=PUBLISHED_PR.head_branch, ready_at="2026-01-01T00:00:00+00:00",
            reason="pending",
        )
        report = await gate(store, provider).refresh(now=due(store))
        assert report.cleared and not report.extended
        assert provider.checks_calls == [("owner/demo", "new-published-source")]
        assert store.resume_hold(100) is None


@pytest.mark.parametrize("identity", ["missing", "unknown", "current", "newer"])
async def test_continuation_diagnosis_requires_current_provider_identity_not_matching_local_head(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, identity: str,
) -> None:
    dispatcher, _, store, client, _ = await make_dispatcher(
        tmp_path, monkeypatch, runner=FakeRunner(make_result(tmp_path)),
    )
    try:
        previous = claim(store, make_item(100), kind="resume")
        store.finish_run(previous.run_id, state=SUCCEEDED, outcome="waiting_ci")
        previous_dir = store.runs_dir / str(previous.run_id)
        previous_dir.mkdir(parents=True)
        (previous_dir / "meta.json").write_text(json.dumps({
            "heads": {"branch_after": "previously-published-source"},
        }))
        store.claim_ci_diagnosis(100, "previously-published-source", "Unit tests")
        heads = {"unknown": None, "current": "previously-published-source", "newer": "new-source"}
        pr = replace(PUBLISHED_PR, head_oid=heads[identity]) if identity != "missing" else None
        context = await dispatcher._continuation_context(
            claim(store, make_item(100), kind="resume"), open_pr=pr,
        )
        assert context is not None
        assert bool(context["hold_reason"]) == (identity == "current")
        assert context["branch_head"] == "previously-published-source"
    finally:
        await client.aclose()
        store.close()
