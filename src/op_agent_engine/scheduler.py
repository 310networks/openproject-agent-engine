"""Scheduler: enforce per-agent and global limits, then claim work.

Before claims, a New work package with unfinished blockers is moved to
``Blocked`` and an engine comment names the blockers. Once every blocker is
Closed the poller offers the Blocked ticket here again as an implement item.

For each item in the :class:`~op_agent_engine.poller.DispatchPlan` (already
ordered by dispatch priority) the scheduler re-checks the slot rules against
the store — the source of truth for active runs — and then claims the work
package:

1. Insert a ``runs`` row with state ``running``. A unique partial index makes
   double-claiming a work package impossible; a failed insert means someone
   else won and the item is skipped. The per-agent limit is checked before
   the insert (``MAX_RUNS_PER_AGENT``).
2. Patch the OpenProject work package: status to ``In Progress`` (implement
   and fix) or ``In Testing`` (review) and assignee to the agent user, using
   the candidate's ``lockVersion``.
3. On HTTP 409 (stale lock) re-read the work package and retry once. If the
   status is no longer the expected candidate status, abort the claim.
4. If the patch fails, mark the run ``cancelled`` and skip.

Only after a successful patch is the work package considered claimed. Items
that cannot run because no slot is free are left completely untouched.
Execution (workspace, prompt, runner) is M4 and later.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from op_agent_engine.config import WorkflowConfig
from op_agent_engine.logging import get_logger
from op_agent_engine.openproject.client import (
    OpenProjectClient,
    OpenProjectError,
    OpenProjectHTTPError,
    OpenProjectNotFoundError,
)
from op_agent_engine.openproject.resolve import OpenProjectResolution
from op_agent_engine.openproject.workpackage import WorkPackage, parse_work_package
from op_agent_engine.outcomes import blocked_by_relations_comment
from op_agent_engine.poller import BlockAction, DispatchItem, DispatchPlan
from op_agent_engine.store import Store

logger = get_logger(__name__)

# Run kind per candidate status: implement, fix, resume or review.
# In Progress and In Testing are the crash-recovery candidates. A Blocked
# ticket whose blockers are all Closed is claimed as a fresh implement run.
KIND_BY_STATUS_KEY = {
    "new": "implement",
    "rejected": "fix",
    "ready_for_review": "review",
    "blocked": "implement",
    "in_progress": "resume",
    "in_testing": "review",
}


class ClaimError(Exception):
    """The claim failed after a run row was created (the row is cancelled)."""


@dataclass(frozen=True)
class ClaimedRun:
    """A successfully claimed work package with its run metadata.

    In dry-run mode ``run_id`` and ``run_dir`` are None: nothing was written
    to the store or to OpenProject.
    """

    run_id: int | None
    item: DispatchItem
    kind: str
    attempt: int
    branch: str
    run_dir: Path | None


@dataclass(frozen=True)
class SkippedItem:
    item: DispatchItem
    reason: str


@dataclass(frozen=True)
class ScheduleReport:
    claimed: tuple[ClaimedRun, ...]
    skipped: tuple[SkippedItem, ...]
    blocked: tuple[BlockAction, ...] = ()

    @property
    def claimed_count(self) -> int:
        return len(self.claimed)

    @property
    def skipped_count(self) -> int:
        return len(self.skipped)

    def to_dict(self) -> dict[str, Any]:
        return {
            "claimed": [
                {
                    "run_id": claimed.run_id,
                    "work_package_id": claimed.item.work_package.id,
                    "agent": claimed.item.agent_key,
                    "kind": claimed.kind,
                    "branch": claimed.branch,
                }
                for claimed in self.claimed
            ],
            "skipped": [
                {
                    "work_package_id": skipped.item.work_package.id,
                    "agent": skipped.item.agent_key,
                    "reason": skipped.reason,
                }
                for skipped in self.skipped
            ],
            "blocked": [
                {
                    "work_package_id": action.work_package.id,
                    "agent": action.agent_key,
                    "blockers": [blocker.id for blocker in action.blockers],
                }
                for action in self.blocked
            ],
        }


class Scheduler:
    def __init__(
        self,
        client: OpenProjectClient,
        config: WorkflowConfig,
        resolution: OpenProjectResolution,
        store: Store,
        *,
        max_concurrent_runs: int,
        max_runs_per_agent: int = 1,
    ) -> None:
        self._client = client
        self._config = config
        self._resolution = resolution
        self._store = store
        self._max_concurrent_runs = max_concurrent_runs
        self._max_runs_per_agent = max_runs_per_agent

    async def schedule(
        self, plan: DispatchPlan, *, dry_run: bool = False
    ) -> ScheduleReport:
        """Claim as many plan items as the slot rules allow, in plan order.

        With ``dry_run`` the slot rules are evaluated and the decisions are
        logged, but nothing is written to the store or to OpenProject.
        """
        claimed: list[ClaimedRun] = []
        skipped: list[SkippedItem] = []
        blocked: list[BlockAction] = []
        for action in plan.blocked_actions:
            if dry_run:
                blocked.append(action)
                logger.info(
                    "[dry-run] would block %s (agent %s) by %s",
                    action.work_package.display_id,
                    action.agent_key,
                    ", ".join(blocker.display_id for blocker in action.blockers),
                    extra={
                        "work_package_id": action.work_package.id,
                        "agent": action.agent_key,
                        "blockers": [blocker.id for blocker in action.blockers],
                        "dry_run": True,
                    },
                )
                continue
            try:
                await self._block(action)
                blocked.append(action)
            except (ClaimError, OpenProjectError) as error:
                logger.warning(
                    "could not mark %s as Blocked: %s",
                    action.work_package.display_id,
                    error,
                    extra={
                        "work_package_id": action.work_package.id,
                        "reason": str(error),
                    },
                )
        active_work_packages = self._store.active_work_package_ids()
        active_by_agent = self._store.active_run_counts_by_agent()
        active_count = self._store.active_run_count()
        for item in plan.items:
            blocker = self._slot_blocker(
                item, active_work_packages, active_by_agent, active_count
            )
            if blocker is not None:
                skipped.append(SkippedItem(item, blocker))
                logger.info(
                    "not claiming %s (agent %s): %s",
                    item.work_package.display_id,
                    item.agent_key,
                    blocker,
                    extra={
                        "work_package_id": item.work_package.id,
                        "agent": item.agent_key,
                        "reason": blocker,
                    },
                )
                continue
            if dry_run:
                kind = KIND_BY_STATUS_KEY[item.status_key]
                branch = f"op-{item.work_package.project_id}-{item.work_package.id}"
                claimed.append(
                    ClaimedRun(
                        run_id=None,
                        item=item,
                        kind=kind,
                        attempt=1,
                        branch=branch,
                        run_dir=None,
                    )
                )
                logger.info(
                    "[dry-run] would claim %s (agent %s, kind %s, branch %s)",
                    item.work_package.display_id,
                    item.agent_key,
                    kind,
                    branch,
                    extra={
                        "work_package_id": item.work_package.id,
                        "agent": item.agent_key,
                        "kind": kind,
                        "branch": branch,
                        "dry_run": True,
                    },
                )
                # Simulate the slot consumption so later items see it.
                active_work_packages = active_work_packages | {item.work_package.id}
                active_by_agent[item.agent_key] = (
                    active_by_agent.get(item.agent_key, 0) + 1
                )
                active_count += 1
                continue
            try:
                claimed.append(await self._claim(item))
                active_work_packages = active_work_packages | {item.work_package.id}
                active_by_agent[item.agent_key] = (
                    active_by_agent.get(item.agent_key, 0) + 1
                )
                active_count += 1
            except ClaimError as error:
                skipped.append(SkippedItem(item, str(error)))
                logger.warning(
                    "claim failed for %s (agent %s): %s",
                    item.work_package.display_id,
                    item.agent_key,
                    error,
                    extra={
                        "work_package_id": item.work_package.id,
                        "agent": item.agent_key,
                        "reason": str(error),
                    },
                )
        report = ScheduleReport(
            claimed=tuple(claimed), skipped=tuple(skipped), blocked=tuple(blocked)
        )
        logger.info(
            "schedule: %d claimed, %d skipped, %d blocked",
            report.claimed_count,
            report.skipped_count,
            len(report.blocked),
            extra={"schedule_report": report.to_dict(), "dry_run": dry_run},
        )
        return report

    # -- slot rules (eligibility rules 7-9, evaluated against the store) --

    def _slot_blocker(
        self,
        item: DispatchItem,
        active_work_packages: frozenset[int],
        active_by_agent: dict[str, int],
        active_count: int,
    ) -> str | None:
        if item.work_package.id in active_work_packages:
            return "work package already has an active run"
        if active_by_agent.get(item.agent_key, 0) >= self._max_runs_per_agent:
            if self._max_runs_per_agent == 1:
                return f"agent '{item.agent_key}' already has an active run"
            return (
                f"agent '{item.agent_key}' already has "
                f"{self._max_runs_per_agent} active runs"
            )
        if active_count >= self._max_concurrent_runs:
            return "no global run slot available"
        return None

    # -- claim protocol ----------------------------------------------------

    async def _claim(self, item: DispatchItem) -> ClaimedRun:
        wp = item.work_package
        kind = KIND_BY_STATUS_KEY[item.status_key]
        branch = f"op-{wp.project_id}-{wp.id}"
        run_id = self._store.begin_run(
            work_package_id=wp.id,
            project_id=wp.project_id,
            agent=item.agent_key,
            kind=kind,
            attempt=1,
            branch=branch,
            run_dir_template=str(self._store.runs_dir / "{id}"),
            display_id=wp.display_id,
            subject=wp.subject,
        )

        try:
            status_id, user_id = self._claim_targets(item)
            lock_version = wp.lock_version
            if lock_version is None:
                # The candidate entry carried no lockVersion (it is required
                # in the PATCH body); fetch the work package to get the
                # current one.
                fresh = await self._fetch_work_package(wp.id)
                if fresh is None:
                    raise ClaimError("no longer exists")
                if fresh.status_id != wp.status_id:
                    raise ClaimError(
                        f"status changed from '{wp.status_id}' to "
                        f"'{fresh.status_id}' before the claim"
                    )
                lock_version = fresh.lock_version
            if lock_version is None:
                raise ClaimError("work package payload has no lockVersion")

            await self._patch_status_assignee(
                wp.id, lock_version, status_id, user_id, expected_status_id=wp.status_id
            )
        except OpenProjectNotFoundError as error:
            raise await self._abort_claim(run_id, wp, "no longer exists") from error
        except OpenProjectError as error:
            raise await self._abort_claim(
                run_id, wp, f"patch failed: {error}"
            ) from error
        except ClaimError as error:
            raise await self._abort_claim(run_id, wp, str(error)) from error

        logger.info(
            "claimed %s as run %d (kind=%s, agent=%s, branch=%s)",
            wp.display_id,
            run_id,
            kind,
            item.agent_key,
            branch,
            extra={
                "run_id": run_id,
                "work_package_id": wp.id,
                "agent": item.agent_key,
                "kind": kind,
                "branch": branch,
            },
        )
        return ClaimedRun(
            run_id=run_id,
            item=item,
            kind=kind,
            attempt=1,
            branch=branch,
            run_dir=self._store.runs_dir / str(run_id),
        )


    async def _block(self, action: BlockAction) -> None:
        """Move a New work package with unfinished blockers to Blocked.

        The status patch uses the same optimistic-locking protocol as a claim;
        the blocking relation is explained in an engine comment afterwards.
        """
        wp = action.work_package
        lock_version = wp.lock_version
        if lock_version is None:
            fresh = await self._fetch_work_package(wp.id)
            if fresh is None:
                raise ClaimError("no longer exists")
            if fresh.status_id != wp.status_id:
                raise ClaimError(
                    f"status changed from '{wp.status_id}' to "
                    f"'{fresh.status_id}' before blocking"
                )
            lock_version = fresh.lock_version
        if lock_version is None:
            raise ClaimError("work package payload has no lockVersion")

        await self._patch_status_assignee(
            wp.id,
            lock_version,
            self._resolution.statuses["blocked"].id,
            None,
            expected_status_id=wp.status_id,
        )
        await self._post_comment(
            wp.id,
            blocked_by_relations_comment(
                [blocker.display_id for blocker in action.blockers]
            ),
        )
        logger.info(
            "blocked %s pending %s",
            wp.display_id,
            ", ".join(blocker.display_id for blocker in action.blockers),
            extra={
                "work_package_id": wp.id,
                "agent": action.agent_key,
                "blockers": [blocker.id for blocker in action.blockers],
            },
        )

    def _claim_targets(self, item: DispatchItem) -> tuple[int, int]:
        """Target status id and assignee user id for a claim."""
        agent = self._config.agents[item.agent_key]
        status_key = (
            "in_testing"
            if item.status_key in ("ready_for_review", "in_testing")
            else "in_progress"
        )
        return (
            self._resolution.statuses[status_key].id,
            self._resolution.users[agent.op_login].id,
        )

    async def _patch_status_assignee(
        self,
        work_package_id: int,
        lock_version: int,
        status_id: int,
        user_id: int | None,
        *,
        expected_status_id: int,
    ) -> None:
        """PATCH status (and optionally assignee) with optimistic locking.

        On HTTP 409 the work package is re-read: if its status is no longer
        ``expected_status_id`` the claim is aborted, otherwise the patch is
        retried once with the fresh lockVersion.
        """
        links: dict[str, Any] = {
            "status": {"href": f"/api/v3/statuses/{status_id}"}
        }
        if user_id is not None:
            links["assignee"] = {"href": f"/api/v3/users/{user_id}"}
        body = {"lockVersion": lock_version, "_links": links}
        path = f"/api/v3/work_packages/{work_package_id}"
        try:
            await self._client.patch_json(path, json_body=body)
            return
        except OpenProjectHTTPError as error:
            if error.status_code != 409:
                raise

        # Stale lock: re-read and check the status before retrying once.
        fresh = await self._fetch_work_package(work_package_id)
        if fresh is None:
            raise ClaimError(
                f"work package {work_package_id} no longer exists after a "
                "conflict"
            )
        if fresh.status_id != expected_status_id:
            raise ClaimError(
                f"work package {work_package_id} status changed from "
                f"'{expected_status_id}' to '{fresh.status_id}' before the claim"
            )
        if fresh.lock_version is None:
            raise ClaimError(
                f"work package {work_package_id} payload has no lockVersion"
            )
        try:
            await self._client.patch_json(
                path, json_body={**body, "lockVersion": fresh.lock_version}
            )
        except OpenProjectHTTPError as error:
            raise ClaimError(
                f"retry of claim for work package {work_package_id} failed "
                f"after conflict: {error}"
            ) from error

    async def _post_comment(self, work_package_id: int, comment: str) -> None:
        await self._client.post_json(
            f"/api/v3/work_packages/{work_package_id}/activities",
            json_body={"comment": {"raw": comment}},
        )
        logger.info(
            "engine comment posted for work package %s",
            work_package_id,
            extra={
                "work_package_id": work_package_id,
                "comment_event": comment.split("\n", 1)[0],
            },
        )

    async def _fetch_work_package(self, work_package_id: int) -> WorkPackage | None:
        try:
            payload = await self._client.get_json(
                f"/api/v3/work_packages/{work_package_id}"
            )
        except OpenProjectNotFoundError:
            return None
        if not isinstance(payload, dict):
            return None
        return parse_work_package(
            payload,
            self._resolution.agent_field.field_id,
            self._option_id_to_value(),
        )

    def _option_id_to_value(self) -> dict[int, str]:
        return {
            option_id: value
            for value, option_id in self._resolution.agent_field.options.items()
        }

    async def _abort_claim(self, run_id: int, wp: WorkPackage, reason: str) -> ClaimError:
        """Cancel the run row and return the error to report."""
        self._store.cancel_run(run_id, outcome=f"claim aborted: {reason}")
        logger.warning(
            "claim aborted for %s (run %d): %s",
            wp.display_id,
            run_id,
            reason,
            extra={"run_id": run_id, "work_package_id": wp.id, "reason": reason},
        )
        return ClaimError(f"claim aborted: {reason}")
