"""Resume holds: park an In Progress ticket until its CI checks settle.

A long task spends most of its wall clock waiting for CI. The engine used to
resume the agent immediately after every checkpoint run, so "the checks are
still running" cost a full model run each time (and a draft PR with nothing to
push was even scored as a crash). A resume hold replaces that with a cheap
hosting-provider poll:

* the dispatcher writes a hold with a ``ready_at`` deadline and the head SHA,
* the scheduler skips the ticket while the deadline is in the future,
* :class:`ResumeGate` runs on every engine loop, asks the hosting provider
  whether that commit's checks are still running and either extends the
  deadline or clears the hold so the next poll resumes the agent.

The gate never claims work or changes a ticket; it only drops or extends
holds. Everything degrades to "resume now" when the checks cannot be read.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Protocol

from op_agent_engine.config import EngineSettings
from op_agent_engine.github import ChecksSummary, GitHubError
from op_agent_engine.logging import get_logger
from op_agent_engine.store import ResumeHold, Store

logger = get_logger(__name__)


class ChecksLookup(Protocol):
    """The part of a hosting client the gate needs."""

    async def commit_checks(self, repo: str, sha: str) -> ChecksSummary: ...


def resume_ready_at(*, now: datetime, wait_seconds: int) -> str:
    """The ISO deadline of a new hold (never in the past)."""
    return (now + timedelta(seconds=max(1, wait_seconds))).isoformat()


@dataclass(frozen=True)
class GateReport:
    """What one gate pass did, for logging and tests."""

    extended: tuple[ResumeHold, ...] = ()
    cleared: tuple[ResumeHold, ...] = ()
    next_ready_at: str | None = None
    notes: tuple[str, ...] = field(default=())

    def to_dict(self) -> dict[str, object]:
        return {
            "extended": [hold.work_package_id for hold in self.extended],
            "cleared": [hold.work_package_id for hold in self.cleared],
            "next_ready_at": self.next_ready_at,
            "notes": list(self.notes),
        }


class ResumeGate:
    """Refresh due resume holds against the hosting provider's checks."""

    def __init__(
        self,
        *,
        store: Store,
        checks: ChecksLookup,
        wait_seconds: int,
        max_attempts: int,
    ) -> None:
        self._store = store
        self._checks = checks
        self._wait_seconds = max(1, wait_seconds)
        self._max_attempts = max(0, max_attempts)

    async def refresh(self, *, now: datetime | None = None) -> GateReport:
        """Extend holds whose checks still run; clear the rest.

        Only due holds are touched, so a 60 s poll loop does not hammer the
        provider while a five-minute hold is still counting down.
        """
        current = now or datetime.now(UTC)
        extended: list[ResumeHold] = []
        cleared: list[ResumeHold] = []
        notes: list[str] = []

        for hold in self._store.resume_holds():
            if not hold.ready(current):
                continue
            if not hold.head_sha:
                self._store.clear_resume_hold(hold.work_package_id)
                cleared.append(hold)
                notes.append(f"{hold.work_package_id}: no head recorded")
                continue
            try:
                checks = await self._checks.commit_checks(hold.repo, hold.head_sha)
            except GitHubError as error:
                # An unreadable check state must never block the ticket: the
                # agent can look itself, which is the old behaviour.
                self._store.clear_resume_hold(hold.work_package_id)
                cleared.append(hold)
                notes.append(f"{hold.work_package_id}: checks unavailable ({error})")
                logger.warning(
                    "could not read the CI state of %s (%s); resuming instead of "
                    "holding",
                    hold.head_sha[:12],
                    error,
                    extra={
                        "work_package_id": hold.work_package_id,
                        "repo": hold.repo,
                        "head_sha": hold.head_sha,
                    },
                )
                continue

            if checks.is_pending and hold.attempts < self._max_attempts:
                self._store.extend_resume_hold(
                    hold.work_package_id,
                    ready_at=resume_ready_at(
                        now=current, wait_seconds=self._wait_seconds
                    ),
                    reason=checks.describe(),
                )
                extended.append(hold)
                logger.info(
                    "holding the resume of work package %s: %s",
                    hold.work_package_id,
                    checks.describe(),
                    extra={
                        "work_package_id": hold.work_package_id,
                        "repo": hold.repo,
                        "head_sha": hold.head_sha,
                        "checks": checks.describe(),
                        "attempts": hold.attempts + 1,
                    },
                )
                continue

            if checks.is_pending:
                notes.append(
                    f"{hold.work_package_id}: wait budget spent, resuming anyway"
                )
            self._store.clear_resume_hold(hold.work_package_id)
            cleared.append(hold)
            logger.info(
                "resume of work package %s released: %s",
                hold.work_package_id,
                checks.describe(),
                extra={
                    "work_package_id": hold.work_package_id,
                    "checks": checks.describe(),
                },
            )

        holds = self._store.resume_holds()
        next_ready_at = holds[0].ready_at if holds else None
        return GateReport(
            extended=tuple(extended),
            cleared=tuple(cleared),
            next_ready_at=next_ready_at,
            notes=tuple(notes),
        )


def build_resume_gate(store: Store, settings: EngineSettings, checks: ChecksLookup) -> ResumeGate:
    """The gate configured from the engine settings."""
    return ResumeGate(
        store=store,
        checks=checks,
        wait_seconds=settings.checkpoint_wait_seconds,
        max_attempts=settings.max_checkpoint_waits,
    )
