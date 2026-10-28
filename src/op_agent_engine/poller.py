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

# Candidate statuses for M2 (matches eligibility.DISPATCH_STATUS_KEYS).
CANDIDATE_STATUS_KEYS = ("new", "rejected", "ready_for_review")


@dataclass(frozen=True)
class DispatchItem:
    work_package: WorkPackage
    agent_key: str
    status_key: str


@dataclass(frozen=True)
class DispatchPlan:
    items: tuple[DispatchItem, ...]
    skipped: tuple[tuple[WorkPackage, str], ...]

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
        }


class Poller:
    def __init__(
        self,
        client: OpenProjectClient,
        config: WorkflowConfig,
        resolution: OpenProjectResolution,
        *,
        max_concurrent_runs: int = 1,
    ) -> None:
        self._client = client
        self._config = config
        self._resolution = resolution
        self._max_concurrent_runs = max_concurrent_runs

    async def poll(self) -> DispatchPlan:
        ctx = await self._build_context()
        candidates = await self._fetch_candidates(ctx)
        items: list[DispatchItem] = []
        skipped: list[tuple[WorkPackage, str]] = []
        for wp in candidates:
            # Pre-check with empty external data: cheap rules (1-5) first so we
            # only pay for relation/child queries when they can matter.
            pre = evaluate_eligibility(wp, ctx)
            if not pre.eligible:
                skipped.append((wp, pre.reason or "ineligible"))
                continue
            blocker_status_keys = await self._fetch_blocker_status_keys(wp)
            has_children = await self._fetch_has_children(wp)
            result = evaluate_eligibility(
                wp,
                ctx,
                blocker_status_keys=blocker_status_keys,
                has_children=has_children,
            )
            if not result.eligible:
                skipped.append((wp, result.reason or "ineligible"))
                continue
            items.append(
                DispatchItem(wp, result.agent_key or "", result.status_key or "")
            )

        items.sort(key=lambda item: self._sort_key(item, ctx))
        plan = DispatchPlan(items=tuple(items), skipped=tuple(skipped))
        self._log_plan(plan)
        return plan

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
        status_ids = [
            str(self._resolution.statuses[key].id)
            for key in CANDIDATE_STATUS_KEYS
            if key in self._resolution.statuses
        ]
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
            filters = [
                {"project": {"operator": "=", "values": [str(resolved.id)]}},
                {"status": {"operator": "=", "values": status_ids}},
                {"type": {"operator": "=", "values": [str(type_id) for type_id in type_ids]}},
            ]
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

    async def _fetch_blocker_status_keys(
        self, wp: WorkPackage
    ) -> list[tuple[int, str]]:
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
        result: list[tuple[int, str]] = []
        for blocker_id in sorted(blocker_ids):
            entry = await self._fetch_work_package(blocker_id)
            if entry is None:
                continue
            parsed = parse_work_package(
                entry, self._resolution.agent_field.field_id, {}
            )
            result.append(
                (blocker_id, status_by_id.get(parsed.status_id, "unknown"))
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
        for wp, reason in plan.skipped:
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
