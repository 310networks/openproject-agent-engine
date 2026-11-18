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
holds. Pending or unreadable checks remain passive holds, even beyond the
poll-warning threshold. A slow queue must not spend model sessions. A warning
is logged once per hold when that threshold is reached; a settled result (or
an operator clearing the hold) releases it.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Protocol

from op_agent_engine.config import EngineSettings
from op_agent_engine.github import ChecksSummary, GitHubError
from op_agent_engine.logging import get_logger
from op_agent_engine.openproject.client import (
    OpenProjectClient,
    OpenProjectError,
    OpenProjectNotFoundError,
)
from op_agent_engine.openproject.workpackage import parse_work_package
from op_agent_engine.store import ResumeHold, Store

logger = get_logger(__name__)

HoldActiveLookup = Callable[[int], Awaitable[bool | None]]


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
        actionable_checks: Mapping[str, tuple[str, ...]] | None = None,
        is_active: HoldActiveLookup | None = None,
    ) -> None:
        self._store = store
        self._checks = checks
        self._wait_seconds = max(1, wait_seconds)
        self._max_attempts = max(0, max_attempts)
        self._actionable_checks = actionable_checks or {}
        self._is_active = is_active

    def _release_hold(self, hold: ResumeHold, current: datetime) -> None:
        self._store.clear_resume_hold(hold.work_package_id)
        elapsed = 0.0
        try:
            created = datetime.fromisoformat(hold.created_at)
            if created.tzinfo is None:
                created = created.replace(tzinfo=UTC)
            elapsed = max(0.0, (current - created).total_seconds())
        except ValueError:
            logger.warning("invalid hold creation timestamp for %s", hold.work_package_id)
        logger.info(
            "CI passive hold released for %s after %.1fs (%d provider polls)",
            hold.work_package_id, elapsed, hold.attempts,
            extra={"work_package_id": hold.work_package_id, "head_sha": hold.head_sha,
                   "passive_ci_seconds": round(elapsed, 2), "provider_polls": hold.attempts},
        )

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
            if self._is_active is not None:
                active = await self._is_active(hold.work_package_id)
                if active is False:
                    self._release_hold(hold, current)
                    cleared.append(hold)
                    notes.append(f"{hold.work_package_id}: no longer in an active status")
                    continue
                if active is None:
                    self._store.extend_resume_hold(
                        hold.work_package_id,
                        ready_at=resume_ready_at(now=current, wait_seconds=self._wait_seconds),
                        reason="Work package status unavailable; passive recheck",
                    )
                    extended.append(hold)
                    log = logger.warning if hold.attempts == self._max_attempts else logger.debug
                    log("work package status unavailable for %s; retaining passive hold",
                        hold.work_package_id)
                    continue
            if not hold.head_sha:
                self._store.clear_resume_hold(hold.work_package_id)
                cleared.append(hold)
                notes.append(f"{hold.work_package_id}: no head recorded")
                continue
            try:
                checks = await self._checks.commit_checks(hold.repo, hold.head_sha)
            except GitHubError as error:
                # Provider trouble is not work for the task implementer.
                # Warn once, retaining a visible hold and the normal polling cadence.
                self._store.extend_resume_hold(
                    hold.work_package_id,
                    ready_at=resume_ready_at(now=current, wait_seconds=self._wait_seconds),
                    reason="CI state unavailable; operator should check provider access",
                )
                extended.append(hold)
                notes.append(f"{hold.work_package_id}: checks unavailable ({error})")
                log = logger.warning if hold.attempts == self._max_attempts else logger.debug
                log(
                    "CI state unavailable for %s (%s); passive recheck in %ds; "
                    "operator may clear the hold after repairing access",
                    hold.head_sha[:12], error, self._wait_seconds,
                    extra={"work_package_id": hold.work_package_id,
                           "head_sha": hold.head_sha, "attempts": hold.attempts + 1},
                )
                continue

            failures = checks.actionable_failures(self._actionable_checks.get(hold.repo, ()))
            if (hold.diagnosis_allowed and checks.is_pending and failures
                    and self._store.claim_ci_diagnosis(
                        hold.work_package_id, hold.head_sha, "|".join(failures),
                    )):
                # Diagnosis is useful now. This neither cancels other jobs nor
                # satisfies the later settle-all/review/merge qualification gate.
                self._release_hold(hold, current)
                cleared.append(hold)
                notes.append(f"{hold.work_package_id}: actionable failure: {', '.join(failures)}")
                logger.info("released one CI diagnosis: %s", notes[-1])
                continue

            if checks.is_pending:
                reason = checks.describe()
                if hold.attempts >= self._max_attempts:
                    reason += "; poll warning threshold exceeded: operator inspect queue/provider"
                self._store.extend_resume_hold(
                    hold.work_package_id,
                    ready_at=resume_ready_at(
                        now=current, wait_seconds=self._wait_seconds
                    ),
                    reason=reason,
                )
                extended.append(hold)
                log = logger.warning if hold.attempts == self._max_attempts else logger.debug
                log(
                    "CI remains pending; holding work package %s without a model run: %s",
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

            self._release_hold(hold, current)
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


def build_resume_gate(
    store: Store, settings: EngineSettings, checks: ChecksLookup, *,
    client: OpenProjectClient | None = None,
    active_status_ids: frozenset[int] | None = None,
) -> ResumeGate:
    """Configure the gate; engine entry points supply resolved active status IDs."""
    if client is not None and not active_status_ids:
        raise ValueError("active status IDs are required with a work-package client")

    async def is_active(work_package_id: int) -> bool | None:
        assert client is not None
        try:
            payload = await client.get_json(f"/api/v3/work_packages/{work_package_id}")
        except OpenProjectNotFoundError:
            return False
        except OpenProjectError:
            return None
        if not isinstance(payload, dict):
            return None
        status_id = parse_work_package(payload, "", {}).status_id
        return status_id in (active_status_ids or ()) if status_id else None

    return ResumeGate(
        store=store,
        checks=checks,
        wait_seconds=settings.checkpoint_wait_seconds,
        max_attempts=settings.max_checkpoint_waits,
        actionable_checks=settings.ci_actionable_checks,
        is_active=is_active if client is not None else None,
    )
