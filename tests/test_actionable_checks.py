"""Early diagnosis is trusted, once-per-source, and is not merge qualification."""

from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path

from op_agent_engine.checkpoints import ResumeGate
from op_agent_engine.github import CheckResult, summarise_checks
from op_agent_engine.store import Store
from tests.test_checkpoints import FakeChecks
from tests.test_checkpoints import _hold as base_hold


def _hold(store: Store, **kwargs) -> None:
    base_hold(store, diagnosis_allowed=True, **kwargs)


def mixed_checks():
    return summarise_checks(
        {
            "check_runs": [
                {
                    "name": "Unit tests",
                    "status": "completed",
                    "conclusion": "failure",
                    "html_url": "run/1",
                },
                {"name": "Documentation", "status": "in_progress"},
            ]
        }
    )


def test_identities_survive_pending_aggregation() -> None:
    checks = mixed_checks()
    assert checks.state == "pending"
    assert CheckResult("Unit tests", "failure", "run/1") in checks.checks
    assert checks.actionable_failures(("Unit tests",)) == ("Unit tests",)
    assert checks.actionable_failures(("unit tests",)) == ()  # exact, no inferred required scope


async def test_required_failure_wakes_once_while_unrelated_ci_runs(tmp_path: Path) -> None:
    path = tmp_path / "engine.db"
    now = datetime.now(UTC) + timedelta(seconds=5)
    with closing(Store(path)) as store:
        _hold(store, ready_in_seconds=1)
        gate = ResumeGate(
            store=store,
            checks=FakeChecks(mixed_checks()),
            wait_seconds=60,
            max_attempts=8,
            actionable_checks={"acme/repo": ("Unit tests",)},
        )
        assert (await gate.refresh(now=now)).cleared
        assert store.resume_hold(100) is None
        _hold(store, ready_in_seconds=1)
        assert (await gate.refresh(now=now)).extended
    # Restart must not turn the same failure into another paid diagnosis.
    with closing(Store(path)) as store:
        gate = ResumeGate(
            store=store,
            checks=FakeChecks(mixed_checks()),
            wait_seconds=60,
            max_attempts=8,
            actionable_checks={"acme/repo": ("Unit tests",)},
        )
        assert not (await gate.refresh(now=now + timedelta(seconds=120))).cleared
        _hold(store, ready_in_seconds=1, head_sha="new-source")
        assert (await gate.refresh(now=now)).cleared


async def test_review_and_upgraded_holds_never_wake_before_checks_settle() -> None:
    with closing(Store(":memory:")) as store:
        base_hold(store, ready_in_seconds=1, diagnosis_allowed=False)
        gate = ResumeGate(
            store=store,
            checks=FakeChecks(mixed_checks()),
            wait_seconds=60,
            max_attempts=8,
            actionable_checks={"acme/repo": ("Unit tests",)},
        )
        report = await gate.refresh(now=datetime.now(UTC) + timedelta(seconds=5))
        assert report.extended and not report.cleared
        assert store.latest_ci_diagnosis(100) is None


async def test_unconfigured_optional_failure_does_not_wake_pending_agent() -> None:
    with closing(Store(":memory:")) as store:
        _hold(store, ready_in_seconds=1)
        gate = ResumeGate(
            store=store,
            checks=FakeChecks(mixed_checks()),
            wait_seconds=60,
            max_attempts=8,
            actionable_checks={"acme/repo": ("Different",)},
        )
        report = await gate.refresh(now=datetime.now(UTC) + timedelta(seconds=5))
        assert report.extended and not report.cleared
