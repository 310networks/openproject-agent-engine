"""Poller: query candidate work packages, filter eligibility, order dispatch.

The poller is read-only: :meth:`Poller.poll` produces a :class:`DispatchPlan`
and logs it. It performs no OpenProject mutations and does not claim work
packages.

Request budget matters: a poll runs every ``POLL_INTERVAL_SECONDS``, so every
query here is paid repeatedly. The rules are therefore applied in cost order —
runtime state (active runs, free slots) and the fields already in the candidate
payload reject a ticket before any dependency query is issued — and dependency
state (relations, blocker statuses, children) is fetched in one batched round
that runs on its own, slower ``BLOCKER_REFRESH_SECONDS`` timer.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from op_agent_engine.config import WorkflowConfig
from op_agent_engine.eligibility import (
    EligibilityContext,
    EligibilityResult,
    build_eligibility_context,
    evaluate_eligibility,
)
from op_agent_engine.logging import get_logger
from op_agent_engine.openproject.client import OpenProjectClient
from op_agent_engine.openproject.resolve import OpenProjectResolution
from op_agent_engine.openproject.workpackage import WorkPackage, parse_work_package

logger = get_logger(__name__)

# Statuses that start a fresh workflow step. ``Blocked`` is a candidate so a
# poll can move it back to In Progress once every blocker is Closed.
CANDIDATE_STATUS_KEYS = ("new", "rejected", "ready_for_review", "blocked")

# Statuses recovered after a crash or a retryable failure.
# They are only queried when assigned to an agent user, so a work package that
# a human put into In Progress/In Testing is never touched.
RECOVERY_STATUS_KEYS = ("in_progress", "in_testing")

# OpenProject rejects very long filter values, and one batched request is only
# useful while it stays a single round trip; both limits are far apart.
DEPENDENCY_CHUNK_SIZE = 100


@dataclass(frozen=True)
class Blocker:
    """A work package that blocks a candidate, with its current status."""

    id: int
    display_id: str
    status_key: str


@dataclass(frozen=True)
class BlockAction:
    """A New work package that must move to Blocked instead of being claimed."""

    work_package: WorkPackage
    agent_key: str
    blockers: tuple[Blocker, ...]


@dataclass(frozen=True)
class _DependencySnapshot:
    """Cached dependency state for one candidate work package.

    Refreshed in a batched round when ``checked_at`` is older than
    ``BLOCKER_REFRESH_SECONDS``, so a parked ``Blocked`` ticket costs no
    requests between refreshes.
    """

    blockers: tuple[Blocker, ...]
    has_children: bool
    checked_at: float


@dataclass(frozen=True)
class DispatchItem:
    work_package: WorkPackage
    agent_key: str
    status_key: str


@dataclass(frozen=True)
class DispatchPlan:
    items: tuple[DispatchItem, ...]
    skipped: tuple[tuple[WorkPackage, str], ...]
    blocked_actions: tuple[BlockAction, ...] = ()

    @property
    def work_package_ids(self) -> tuple[int, ...]:
        return tuple(item.work_package.id for item in self.items)

    def to_dict(self) -> dict[str, Any]:
        return {
            "items": [
                {
                    "work_package_id": item.work_package.id,
                    "display_id": item.work_package.display_id,
                    "subject": item.work_package.subject,
                    "status": item.status_key,
                    "agent": item.agent_key,
                }
                for item in self.items
            ],
            "skipped": [
                {"work_package_id": wp.id, "display_id": wp.display_id, "reason": reason}
                for wp, reason in self.skipped
            ],
            "blocked": [
                {
                    "work_package_id": action.work_package.id,
                    "display_id": action.work_package.display_id,
                    "agent": action.agent_key,
                    "blockers": [
                        {
                            "work_package_id": blocker.id,
                            "display_id": blocker.display_id,
                            "status": blocker.status_key,
                        }
                        for blocker in action.blockers
                    ],
                }
                for action in self.blocked_actions
            ],
        }


class Poller:
    def __init__(
        self,
        client: OpenProjectClient,
        config: WorkflowConfig,
        resolution: OpenProjectResolution,
        *,
        max_concurrent_runs: int = 1,
        max_runs_per_agent: int = 1,
        blocker_refresh_seconds: float = 0.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._client = client
        self._config = config
        self._resolution = resolution
        self._max_concurrent_runs = max_concurrent_runs
        self._max_runs_per_agent = max_runs_per_agent
        # ``0`` checks dependencies on every poll (the pre-cache behaviour).
        self._blocker_refresh_seconds = blocker_refresh_seconds
        self._clock = clock
        self._dependencies: dict[int, _DependencySnapshot] = {}

    async def poll(
        self,
        *,
        active_run_work_package_ids: frozenset[int] = frozenset(),
        active_run_counts_by_agent: Mapping[str, int] | None = None,
        global_slots_used: int = 0,
    ) -> DispatchPlan:
        """Build the dispatch plan for this poll.

        The runtime snapshot turns the slot rules into a first-class filter: a
        ticket whose agent is busy, which already has a run, or which cannot
        run because every slot is taken is dropped before any dependency query
        is issued. The scheduler still re-checks the rules against the store
        when it claims, so the snapshot only saves work, never authority.
        """
        ctx = await self._build_context()
        candidates = await self._fetch_candidates(ctx)
        active_counts = active_run_counts_by_agent or {}

        # Keep the cache bounded: tickets that are no longer candidates (status
        # changed, moved away, closed) must not accumulate snapshots forever.
        candidate_ids = {wp.id for wp in candidates}
        self._dependencies = {
            work_package_id: snapshot
            for work_package_id, snapshot in self._dependencies.items()
            if work_package_id in candidate_ids
        }

        items: list[DispatchItem] = []
        skipped: list[tuple[WorkPackage, str]] = []
        blocked_actions: list[BlockAction] = []
        pending: list[tuple[WorkPackage, EligibilityResult]] = []
        for wp in candidates:
            # Cheap rules first (project, type, status, AGENT, human assignee,
            # scope) plus the runtime slot rules, so we only pay for dependency
            # queries when they can change the decision.
            pre = evaluate_eligibility(
                wp,
                ctx,
                active_run_work_package_ids=active_run_work_package_ids,
                active_run_counts_by_agent=active_counts,
                global_slots_used=global_slots_used,
            )
            if not pre.eligible:
                self._log_skipped(wp, pre.reason or "ineligible", pre.scope_violation)
                skipped.append((wp, pre.reason or "ineligible"))
                continue
            pending.append((wp, pre))

        # One sequential, batched dependency round for the tickets that need it;
        # fresh snapshots are reused and cost nothing.
        await self._refresh_dependencies([wp for wp, _ in pending])

        for wp, pre in pending:
            snapshot = self._dependencies[wp.id]
            unfinished = tuple(
                blocker
                for blocker in snapshot.blockers
                if blocker.status_key != "closed"
            )
            # A New ticket with an unfinished blocker is not merely skipped:
            # the engine records the dependency on the ticket and parks it in
            # Blocked. It is picked up again by the Blocked candidate rule once
            # every blocker is Closed.
            if pre.status_key == "new" and unfinished:
                blocked_actions.append(
                    BlockAction(
                        work_package=wp,
                        agent_key=pre.agent_key or "",
                        blockers=unfinished,
                    )
                )
                continue
            result = evaluate_eligibility(
                wp,
                ctx,
                blocker_status_keys=[
                    (blocker.id, blocker.status_key) for blocker in snapshot.blockers
                ],
                has_children=snapshot.has_children,
                active_run_work_package_ids=active_run_work_package_ids,
                active_run_counts_by_agent=active_counts,
                global_slots_used=global_slots_used,
            )
            if not result.eligible:
                self._log_skipped(
                    wp, result.reason or "ineligible", result.scope_violation
                )
                skipped.append((wp, result.reason or "ineligible"))
                continue
            items.append(
                DispatchItem(wp, result.agent_key or "", result.status_key or "")
            )

        items.sort(key=lambda item: self._sort_key(item, ctx))
        plan = DispatchPlan(
            items=tuple(items),
            skipped=tuple(skipped),
            blocked_actions=tuple(blocked_actions),
        )
        self._log_plan(plan)
        return plan

    @staticmethod
    def _log_skipped(
        wp: WorkPackage, reason: str, scope_violation: bool
    ) -> None:
        """Warn about a mis-scoped agent; log other skips at debug level."""
        if scope_violation:
            logger.warning(
                "skipping %s: %s (project scope, work package left untouched)",
                wp.display_id,
                reason,
                extra={
                    "work_package_id": wp.id,
                    "project_id": wp.project_id,
                    "reason": reason,
                    "scope_violation": True,
                },
            )
        else:
            logger.debug("skipped %s: %s", wp.display_id, reason)

    # -- ordering --------------------------------------------------------

    def _sort_key(self, item: DispatchItem, ctx: EligibilityContext) -> tuple[int, int, int]:
        status_order = self._config.openproject.dispatch_priority
        try:
            status_index = status_order.index(item.status_key)
        except ValueError:
            status_index = len(status_order)
        priority_position = ctx.priority_position.get(item.work_package.priority_id, 0)
        return (status_index, -priority_position, item.work_package.id)

    # -- data fetching ---------------------------------------------------

    async def _build_context(self) -> EligibilityContext:
        priority_position = await self._fetch_priority_positions()
        return build_eligibility_context(
            self._config,
            self._resolution,
            priority_position=priority_position,
            max_concurrent_runs=self._max_concurrent_runs,
            max_runs_per_agent=self._max_runs_per_agent,
        )

    async def _fetch_priority_positions(self) -> dict[int, int]:
        positions: dict[int, int] = {}
        async for element in self._client.iter_collection("/api/v3/priorities"):
            element_id = element.get("id")
            if isinstance(element_id, int):
                position = element.get("position")
                positions[element_id] = position if isinstance(position, int) else 0
        return positions

    async def _fetch_candidates(self, ctx: EligibilityContext) -> list[WorkPackage]:
        candidates = await self._fetch_candidates_for(
            ctx, CANDIDATE_STATUS_KEYS
        )
        recovery_agent_user_ids = sorted(
            ctx.agent_user_ids - {ctx.resolution.engine_user.id}
        )
        if recovery_agent_user_ids:
            candidates.extend(
                await self._fetch_candidates_for(
                    ctx,
                    RECOVERY_STATUS_KEYS,
                    assignee_ids=recovery_agent_user_ids,
                )
            )
        return candidates

    async def _fetch_candidates_for(
        self,
        ctx: EligibilityContext,
        status_keys: tuple[str, ...],
        *,
        assignee_ids: list[int] | None = None,
    ) -> list[WorkPackage]:
        status_ids = [
            str(self._resolution.statuses[key].id)
            for key in status_keys
            if key in self._resolution.statuses
        ]
        if not status_ids:
            return []
        candidates: list[WorkPackage] = []
        for project in self._config.projects:
            if not project.enabled:
                continue
            resolved = self._resolution.projects.get(project.identifier)
            if resolved is None:
                continue
            type_ids = sorted(ctx.eligible_type_ids.get(resolved.id, frozenset()))
            if not type_ids:
                continue
            filters: list[dict[str, Any]] = [
                {"project": {"operator": "=", "values": [str(resolved.id)]}},
                {"status": {"operator": "=", "values": status_ids}},
                {"type": {"operator": "=", "values": [str(type_id) for type_id in type_ids]}},
            ]
            if assignee_ids is not None:
                filters.append(
                    {
                        "assignee": {
                            "operator": "=",
                            "values": [str(user_id) for user_id in assignee_ids],
                        }
                    }
                )
            async for element in self._client.iter_collection(
                "/api/v3/work_packages",
                params={
                    "filters": json.dumps(filters),
                    "sortBy": json.dumps(["priority:desc", "id:asc"]),
                },
            ):
                candidates.append(
                    parse_work_package(
                        element, ctx.agent_field_id, ctx.option_id_to_value
                    )
                )
        return candidates

    # -- dependency refresh ----------------------------------------------

    def _is_fresh(self, work_package_id: int, now: float) -> bool:
        snapshot = self._dependencies.get(work_package_id)
        if snapshot is None or self._blocker_refresh_seconds <= 0:
            return False
        return now - snapshot.checked_at < self._blocker_refresh_seconds

    async def _refresh_dependencies(self, work_packages: list[WorkPackage]) -> None:
        """Refresh relations, blocker statuses and children in one round.

        The queries are batched per chunk and issued sequentially, never in
        parallel, so a poll produces a short, predictable burst instead of one
        request per ticket.
        """
        now = self._clock()
        stale = [wp for wp in work_packages if not self._is_fresh(wp.id, now)]
        if not stale:
            return
        stale_ids = {wp.id for wp in stale}
        relations = await self._fetch_relations_for_many(sorted(stale_ids))
        blockers_by_wp = _blockers_by_work_package(relations, stale_ids)
        blocker_ids = sorted(
            {
                blocker_id
                for blocker_ids_for_wp in blockers_by_wp.values()
                for blocker_id in blocker_ids_for_wp
            }
        )
        blocker_details = await self._fetch_blocker_details(blocker_ids)
        parent_ids = await self._fetch_work_packages_with_children(
            sorted(stale_ids)
        )
        for wp in stale:
            self._dependencies[wp.id] = _DependencySnapshot(
                blockers=tuple(
                    blocker_details[blocker_id]
                    for blocker_id in blockers_by_wp.get(wp.id, ())
                    if blocker_id in blocker_details
                ),
                has_children=wp.id in parent_ids,
                checked_at=now,
            )

    async def _fetch_relations_for_many(
        self, work_package_ids: list[int]
    ) -> list[dict[str, Any]]:
        relations: list[dict[str, Any]] = []
        for chunk in _chunks(work_package_ids, DEPENDENCY_CHUNK_SIZE):
            filters = [
                {
                    "involved": {
                        "operator": "=",
                        "values": [str(work_package_id) for work_package_id in chunk],
                    }
                }
            ]
            relations.extend(
                await self._client.get_collection(
                    "/api/v3/relations", params={"filters": json.dumps(filters)}
                )
            )
        return relations

    async def _fetch_blocker_details(
        self, blocker_ids: list[int]
    ) -> dict[int, Blocker]:
        """Resolve every blocker's display id and current status in one query."""
        details: dict[int, Blocker] = {}
        if not blocker_ids:
            return details
        status_by_id = {
            status.id: key for key, status in self._resolution.statuses.items()
        }
        for chunk in _chunks(blocker_ids, DEPENDENCY_CHUNK_SIZE):
            filters = [
                {"id": {"operator": "=", "values": [str(item) for item in chunk]}}
            ]
            async for element in self._client.iter_collection(
                "/api/v3/work_packages", params={"filters": json.dumps(filters)}
            ):
                parsed = parse_work_package(
                    element, self._resolution.agent_field.field_id, {}
                )
                details[parsed.id] = Blocker(
                    id=parsed.id,
                    display_id=parsed.display_id,
                    status_key=status_by_id.get(parsed.status_id, "unknown"),
                )
        return details

    async def _fetch_work_packages_with_children(
        self, work_package_ids: list[int]
    ) -> set[int]:
        """Ids among ``work_package_ids`` that currently have a child."""
        if not self._config.openproject.skip_work_packages_with_children:
            return set()
        parents_with_children: set[int] = set()
        for chunk in _chunks(work_package_ids, DEPENDENCY_CHUNK_SIZE):
            filters = [
                {
                    "parent": {
                        "operator": "=",
                        "values": [str(item) for item in chunk],
                    }
                }
            ]
            async for element in self._client.iter_collection(
                "/api/v3/work_packages", params={"filters": json.dumps(filters)}
            ):
                links = element.get("_links")
                parent = links.get("parent") if isinstance(links, dict) else None
                parent_id = _href_id(parent)
                if parent_id is not None:
                    parents_with_children.add(parent_id)
        return parents_with_children

    # -- logging ---------------------------------------------------------

    def _log_plan(self, plan: DispatchPlan) -> None:
        if plan.items:
            logger.info(
                "dispatch plan: %d item(s)",
                len(plan.items),
                extra={"dispatch_plan": plan.to_dict()},
            )
            for item in plan.items:
                logger.info(
                    "  -> %s [%s] agent=%s status=%s",
                    item.work_package.display_id,
                    item.work_package.subject,
                    item.agent_key,
                    item.status_key,
                )
        else:
            logger.info("dispatch plan: no eligible work packages")
        for action in plan.blocked_actions:
            logger.info(
                "  -> block %s pending %s",
                action.work_package.display_id,
                ", ".join(blocker.display_id for blocker in action.blockers),
            )
        for wp, reason in plan.skipped:
            if "no access to project" not in reason:
                logger.debug("skipped %s: %s", wp.display_id, reason)


# ---------------------------------------------------------------------------
# HAL helpers
# ---------------------------------------------------------------------------


def _chunks(values: list[int], size: int) -> list[list[int]]:
    return [values[index : index + size] for index in range(0, len(values), size)]


def _blockers_by_work_package(
    relations: Sequence[dict[str, Any]], blocked_ids: set[int]
) -> dict[int, list[int]]:
    """Map each candidate id to the ids of the work packages blocking it.

    Covers both relation directions: ``A blocks B`` and ``B blocked by A``
    mean the same thing.
    """
    blockers: dict[int, set[int]] = {}
    for relation in relations:
        relation_type = relation.get("type")
        from_id, to_id = _relation_endpoints(relation)
        if from_id is None or to_id is None:
            continue
        if relation_type == "blocks":
            blocked, blocker = to_id, from_id
        elif relation_type == "blocked":
            blocked, blocker = from_id, to_id
        else:
            continue
        if blocked in blocked_ids:
            blockers.setdefault(blocked, set()).add(blocker)
    return {
        blocked: sorted(blocker_ids) for blocked, blocker_ids in blockers.items()
    }


def _relation_endpoints(
    relation: dict[str, Any],
) -> tuple[int | None, int | None]:
    embedded = relation.get("_embedded") or {}
    from_id = _work_package_id(embedded.get("from"))
    to_id = _work_package_id(embedded.get("to"))
    links = relation.get("_links") or {}
    if from_id is None:
        from_id = _href_id(links.get("from"))
    if to_id is None:
        to_id = _href_id(links.get("to"))
    return from_id, to_id


def _work_package_id(wp: Any) -> int | None:
    if isinstance(wp, dict) and isinstance(wp.get("id"), int):
        return int(wp["id"])
    return None


def _href_id(link: Any) -> int | None:
    if not isinstance(link, dict):
        return None
    href = link.get("href")
    if not isinstance(href, str):
        return None
    try:
        return int(href.rsplit("/", 1)[-1])
    except ValueError:
        return None
