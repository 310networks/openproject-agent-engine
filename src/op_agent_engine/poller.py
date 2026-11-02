"""Poller: query candidate work packages, filter eligibility, order dispatch.

M2 is read-only: :meth:`Poller.poll` produces a :class:`DispatchPlan` and logs
it. It performs no OpenProject mutations and does not claim work packages
(claiming and execution are M3 and later).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from op_agent_engine.config import WorkflowConfig
from op_agent_engine.eligibility import (
    EligibilityContext,
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
    ) -> None:
        self._client = client
        self._config = config
        self._resolution = resolution
        self._max_concurrent_runs = max_concurrent_runs
        self._max_runs_per_agent = max_runs_per_agent

    async def poll(self) -> DispatchPlan:
        ctx = await self._build_context()
        candidates = await self._fetch_candidates(ctx)
        items: list[DispatchItem] = []
        skipped: list[tuple[WorkPackage, str]] = []
        blocked_actions: list[BlockAction] = []
        for wp in candidates:
            # Pre-check with empty external data: cheap rules (1-5) first so we
            # only pay for relation/child queries when they can matter.
            pre = evaluate_eligibility(wp, ctx)
            if not pre.eligible:
                self._log_skipped(wp, pre.reason or "ineligible", pre.scope_violation)
                skipped.append((wp, pre.reason or "ineligible"))
                continue
            blockers = await self._fetch_blockers(wp)
            unfinished = tuple(
                blocker for blocker in blockers if blocker.status_key != "closed"
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
            has_children = await self._fetch_has_children(wp)
            result = evaluate_eligibility(
                wp,
                ctx,
                blocker_status_keys=[
                    (blocker.id, blocker.status_key) for blocker in blockers
                ],
                has_children=has_children,
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

    async def _fetch_blockers(self, wp: WorkPackage) -> list[Blocker]:
        relations = await self._fetch_relations_for(wp.id)
        blocker_ids: set[int] = set()
        for relation in relations:
            relation_type = relation.get("type")
            from_id, to_id = _relation_endpoints(relation)
            if relation_type == "blocks" and to_id == wp.id and from_id is not None:
                blocker_ids.add(from_id)
            elif relation_type == "blocked" and from_id == wp.id and to_id is not None:
                blocker_ids.add(to_id)
        if not blocker_ids:
            return []
        status_by_id = {
            status.id: key for key, status in self._resolution.statuses.items()
        }
        result: list[Blocker] = []
        for blocker_id in sorted(blocker_ids):
            entry = await self._fetch_work_package(blocker_id)
            if entry is None:
                continue
            parsed = parse_work_package(
                entry, self._resolution.agent_field.field_id, {}
            )
            result.append(
                Blocker(
                    id=blocker_id,
                    display_id=parsed.display_id,
                    status_key=status_by_id.get(parsed.status_id, "unknown"),
                )
            )
        return result

    async def _fetch_has_children(self, wp: WorkPackage) -> bool:
        filters = [
            {"project": {"operator": "=", "values": [str(wp.project_id)]}},
            {"parent": {"operator": "=", "values": [str(wp.id)]}},
        ]
        payload = await self._client.get_json(
            "/api/v3/work_packages",
            params={"filters": json.dumps(filters), "pageSize": 1},
        )
        embedded = payload.get("_embedded")
        elements = embedded.get("elements") if isinstance(embedded, dict) else None
        return bool(elements)

    async def _fetch_relations_for(self, work_package_id: int) -> list[dict[str, Any]]:
        filters = [{"involved": {"operator": "=", "values": [str(work_package_id)]}}]
        return await self._client.get_collection(
            "/api/v3/relations", params={"filters": json.dumps(filters)}
        )

    async def _fetch_work_package(self, work_package_id: int) -> dict[str, Any] | None:
        try:
            payload = await self._client.get_json(
                f"/api/v3/work_packages/{work_package_id}"
            )
            return payload if isinstance(payload, dict) else None
        except Exception:  # a missing blocker must not kill the poll
            logger.warning("cannot fetch work package %s", work_package_id)
            return None

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
