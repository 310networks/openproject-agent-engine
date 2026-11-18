"""Tests for the resume gate that parks an In Progress ticket on CI."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from op_agent_engine.checkpoints import ResumeGate, resume_ready_at
from op_agent_engine.github import ChecksSummary, GitHubError, PullRequest
from op_agent_engine.store import Store


class FakeChecks:
    """Stands in for ``gh``/GitLab: always returns one summary or raises."""

    def __init__(
        self,
        summary: ChecksSummary | None = None,
        error: GitHubError | None = None,
    ) -> None:
        self.summary = summary or ChecksSummary(state="none")
        self.error = error
        self.calls: list[tuple[str, str]] = []

    async def find_pr(
        self, repo: str, branch: str, *, state: str = "open"
    ) -> PullRequest:
        return PullRequest(
            number=1, url="https://example.org/pulls/1", head_branch=branch,
            state=state, head_oid=branch.removeprefix("task-"),
        )

    async def commit_checks(self, repo: str, sha: str) -> ChecksSummary:
        self.calls.append((repo, sha))
        if self.error is not None:
            raise self.error
        return self.summary


def _store() -> Store:
    return Store(":memory:")


def _hold(
    store: Store,
    *,
    ready_in_seconds: int,
    head_sha: str | None = "abc123",
    attempts: int = 1,
    diagnosis_allowed: bool = False,
) -> None:
    store.set_resume_hold(
        100,
        repo="acme/repo",
        head_sha=head_sha,
        pr_branch=f"task-{head_sha}",
        ready_at=resume_ready_at(
            now=datetime.now(UTC), wait_seconds=ready_in_seconds
        ),
        reason="2 of 3 check(s) still running",
        attempts=attempts,
        diagnosis_allowed=diagnosis_allowed,
    )


async def test_pending_checks_extend_the_hold() -> None:
    store = _store()
    checks = FakeChecks(ChecksSummary(state="pending", total=3, pending=2))
    gate = ResumeGate(store=store, checks=checks, wait_seconds=300, max_attempts=8)
    _hold(store, ready_in_seconds=1)
    before = store.resume_hold(100)
    assert before is not None

    report = await gate.refresh(now=datetime.now(UTC) + timedelta(seconds=5))

    hold = store.resume_hold(100)
    assert hold is not None
    assert hold.attempts == before.attempts + 1
    assert hold.ready_at > before.ready_at
    assert report.extended and report.cleared == ()
    assert checks.calls == [("acme/repo", "abc123")]
    assert report.next_ready_at == hold.ready_at


async def test_settled_checks_release_the_hold() -> None:
    store = _store()
    checks = FakeChecks(ChecksSummary(state="failure", total=3, failed=1))
    gate = ResumeGate(store=store, checks=checks, wait_seconds=300, max_attempts=8)
    _hold(store, ready_in_seconds=1)

    report = await gate.refresh(now=datetime.now(UTC) + timedelta(seconds=5))

    assert store.resume_hold(100) is None
    assert report.cleared and report.extended == ()
    assert report.next_ready_at is None


async def test_a_hold_that_is_not_due_costs_no_provider_call() -> None:
    store = _store()
    checks = FakeChecks(ChecksSummary(state="pending", total=1, pending=1))
    gate = ResumeGate(store=store, checks=checks, wait_seconds=300, max_attempts=8)
    _hold(store, ready_in_seconds=300)

    report = await gate.refresh(now=datetime.now(UTC))

    assert checks.calls == []
    assert report.extended == () and report.cleared == ()
    hold = store.resume_hold(100)
    assert hold is not None
    assert report.next_ready_at == hold.ready_at


async def test_unreadable_checks_extend_instead_of_hot_looping() -> None:
    """A missing token permission must not start an agent run every minute."""
    store = _store()
    checks = FakeChecks(error=GitHubError("gh api failed (403)"))
    gate = ResumeGate(store=store, checks=checks, wait_seconds=300, max_attempts=8)
    _hold(store, ready_in_seconds=1, attempts=1)

    report = await gate.refresh(now=datetime.now(UTC) + timedelta(seconds=5))

    hold = store.resume_hold(100)
    assert hold is not None
    assert hold.attempts == 2
    assert not hold.ready()
    assert "CI state unavailable" in hold.reason
    assert report.extended and report.cleared == ()
    assert any("checks unavailable" in note for note in report.notes)


async def test_unreadable_checks_remain_passive_beyond_warning_threshold() -> None:
    store = _store()
    checks = FakeChecks(error=GitHubError("gh api failed (403)"))
    gate = ResumeGate(store=store, checks=checks, wait_seconds=300, max_attempts=2)
    _hold(store, ready_in_seconds=1, attempts=2)

    report = await gate.refresh(now=datetime.now(UTC) + timedelta(seconds=5))

    assert store.resume_hold(100) is not None
    assert report.extended and not report.cleared
    assert any("checks unavailable" in note for note in report.notes)


async def test_a_hold_without_a_head_is_released() -> None:
    store = _store()
    checks = FakeChecks(ChecksSummary(state="pending", total=1, pending=1))
    gate = ResumeGate(store=store, checks=checks, wait_seconds=300, max_attempts=8)
    _hold(store, ready_in_seconds=1, head_sha=None)

    report = await gate.refresh(now=datetime.now(UTC) + timedelta(seconds=5))

    assert checks.calls == []
    assert store.resume_hold(100) is None
    assert report.cleared


async def test_pending_checks_never_spend_model_sessions_after_warning_threshold() -> None:
    """A long CI queue is cheap passive polling, not an implementer task."""
    store = _store()
    checks = FakeChecks(ChecksSummary(state="pending", total=1, pending=1))
    gate = ResumeGate(store=store, checks=checks, wait_seconds=300, max_attempts=2)
    _hold(store, ready_in_seconds=1, attempts=2)

    report = await gate.refresh(now=datetime.now(UTC) + timedelta(seconds=5))

    hold = store.resume_hold(100)
    assert hold is not None and hold.attempts == 3
    assert report.extended and not report.cleared
    for n in range(12):
        report = await gate.refresh(now=datetime.now(UTC) + timedelta(seconds=400 * (n + 1)))
        assert store.resume_hold(100) is not None
        assert report.extended and not report.cleared


async def test_refresh_counts_extensions_and_releases_separately() -> None:
    store = _store()
    checks = FakeChecks(ChecksSummary(state="pending", total=2, pending=1))
    gate = ResumeGate(store=store, checks=checks, wait_seconds=60, max_attempts=8)
    _hold(store, ready_in_seconds=1)
    store.set_resume_hold(
        200,
        repo="acme/repo",
        head_sha="def456",
        pr_branch="task-def456",
        ready_at=resume_ready_at(now=datetime.now(UTC), wait_seconds=1),
        reason="1 of 1 check still running",
        attempts=1,
    )

    report = await gate.refresh(now=datetime.now(UTC) + timedelta(seconds=2))

    assert [hold.work_package_id for hold in report.extended] == [100, 200]
    assert report.cleared == ()


def test_resume_ready_at_is_never_in_the_past() -> None:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    ready = datetime.fromisoformat(resume_ready_at(now=now, wait_seconds=0))
    assert ready == now + timedelta(seconds=1)
