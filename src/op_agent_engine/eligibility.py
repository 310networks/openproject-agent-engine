"""Eligibility rules for dispatching work packages (PLAN section 8).

Each rule is evaluated in order; the first failure short-circuits with a
reason. Rules that depend on data the poller must fetch (blockers, children)
or on runtime state (active runs, global slots) take that data as explicit
arguments so the rules stay pure and table-testable.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from op_agent_engine.config import WorkflowConfig
from op_agent_engine.openproject.resolve import OpenProjectResolution
from op_agent_engine.openproject.workpackage import WorkPackage

# Statuses dispatchable in M2. The recovery statuses (in_progress, in_testing)
# are M7 and are intentionally excluded here.
DISPATCH_STATUS_KEYS = frozenset({"new", "rejected", "ready_for_review"})


@dataclass(frozen=True)
class EligibilityContext:
    """Precomputed, per-poll lookups shared by every eligibility evaluation."""

    config: WorkflowConfig
    resolution: OpenProjectResolution
    enabled_project_ids: frozenset[int]
    eligible_type_ids: dict[int, frozenset[int]]
    status_key_by_id: dict[int, str]
    agent_user_ids: frozenset[int]
    agent_keys: frozenset[str]
    reviewer_agent_key: str | None
    agent_field_id: str
    option_id_to_value: dict[int, str]
    priority_position: dict[int, int]
    max_concurrent_runs: int


def build_eligibility_context(
    config: WorkflowConfig,
    resolution: OpenProjectResolution,
    *,
    priority_position: dict[int, int],
    max_concurrent_runs: int,
) -> EligibilityContext:
    enabled_project_ids = frozenset(
        resolution.projects[project.identifier].id
        for project in config.projects
        if project.enabled and project.identifier in resolution.projects
    )

    eligible_type_names = set(config.openproject.eligible_types)
    type_ids_by_project: dict[int, set[int]] = {}
    for project in config.projects:
        if not project.enabled:
            continue
        resolved = resolution.projects.get(project.identifier)
        if resolved is None:
            continue
        type_ids = {
            resolved_type.id
            for name, resolved_type in resolved.types.items()
            if name in eligible_type_names
        }
        if type_ids:
            type_ids_by_project[resolved.id] = type_ids
    eligible_type_ids = {
        project_id: frozenset(ids) for project_id, ids in type_ids_by_project.items()
    }

    status_key_by_id = {status.id: key for key, status in resolution.statuses.items()}

    agent_user_id_set = set()
    for agent in config.agents.values():
        user = resolution.users.get(agent.op_login)
        if user is not None:
            agent_user_id_set.add(user.id)
    agent_user_id_set.add(resolution.engine_user.id)
    agent_user_ids = frozenset(agent_user_id_set)

    reviewer_agent_key = next(
        (key for key, agent in config.agents.items() if agent.kind == "reviewer"),
        None,
    )

    option_id_to_value = {
        option_id: value
        for value, option_id in resolution.agent_field.options.items()
    }

    return EligibilityContext(
        config=config,
        resolution=resolution,
        enabled_project_ids=enabled_project_ids,
        eligible_type_ids=eligible_type_ids,
        status_key_by_id=status_key_by_id,
        agent_user_ids=agent_user_ids,
        agent_keys=frozenset(config.agents),
        reviewer_agent_key=reviewer_agent_key,
        agent_field_id=resolution.agent_field.field_id,
        option_id_to_value=option_id_to_value,
        priority_position=priority_position,
        max_concurrent_runs=max_concurrent_runs,
    )


@dataclass(frozen=True)
class EligibilityResult:
    eligible: bool
    reason: str | None
    agent_key: str | None = None
    status_key: str | None = None


def evaluate_eligibility(
    wp: WorkPackage,
    ctx: EligibilityContext,
    *,
    blocker_status_keys: Sequence[tuple[int, str]] = (),
    has_children: bool = False,
    active_run_work_package_ids: frozenset[int] = frozenset(),
    active_run_agent_keys: frozenset[str] = frozenset(),
    global_slots_used: int = 0,
) -> EligibilityResult:
    """Apply the PLAN section 8 rules in order.

    ``blocker_status_keys`` is a sequence of ``(work_package_id, status_key)``
    for each unfinished blocker of ``wp``. A blocker whose status is ``closed``
    does not block.
    """
    status_key = ctx.status_key_by_id.get(wp.status_id)

    # Rule 1: project is enabled and configured.
    if wp.project_id not in ctx.enabled_project_ids:
        return _reject("project is not enabled or configured")

    # Rule 2: work package type is eligible for the project.
    if wp.type_id not in ctx.eligible_type_ids.get(wp.project_id, frozenset()):
        return _reject(f"type '{wp.type_name}' is not eligible")

    # Rule 3: status is dispatchable.
    if status_key not in DISPATCH_STATUS_KEYS:
        return _reject(f"status '{wp.status_name}' is not dispatchable")

    # Rules 3/4: determine the target agent from status and the AGENT field.
    if status_key in ("new", "rejected"):
        agent_key = wp.agent_value
        if agent_key is None:
            return _reject("AGENT field is not set")
        if agent_key not in ctx.agent_keys:
            return _reject(
                f"AGENT field value '{agent_key}' is not a configured agent"
            )
    else:  # ready_for_review
        if ctx.reviewer_agent_key is None:
            return _reject("no reviewer agent configured")
        agent_key = ctx.reviewer_agent_key

    # Rule 5: not assigned to a human (unassigned is eligible).
    if (
        ctx.config.openproject.skip_if_assigned_to_human
        and wp.assignee_id is not None
        and wp.assignee_id not in ctx.agent_user_ids
    ):
        return _reject(
            "assigned to a human",
            agent_key=agent_key,
            status_key=status_key,
        )

    # Rule 6: no unfinished blockers and no children (when configured).
    unfinished = sorted(blocker_id for blocker_id, key in blocker_status_keys if key != "closed")
    if unfinished:
        return _reject(
            f"has unfinished blocker(s) {unfinished}",
            agent_key=agent_key,
            status_key=status_key,
        )
    if ctx.config.openproject.skip_work_packages_with_children and has_children:
        return _reject(
            "has children", agent_key=agent_key, status_key=status_key
        )

    # Rule 7: no active run for this work package.
    if wp.id in active_run_work_package_ids:
        return _reject(
            "already has an active run", agent_key=agent_key, status_key=status_key
        )

    # Rule 8: the target agent has no active run (one per agent).
    if agent_key in active_run_agent_keys:
        return _reject(
            f"agent '{agent_key}' already has an active run",
            agent_key=agent_key,
            status_key=status_key,
        )

    # Rule 9: a global run slot is available.
    if global_slots_used >= ctx.max_concurrent_runs:
        return _reject(
            "no global run slot available",
            agent_key=agent_key,
            status_key=status_key,
        )

    return EligibilityResult(True, None, agent_key=agent_key, status_key=status_key)


def _reject(
    reason: str,
    *,
    agent_key: str | None = None,
    status_key: str | None = None,
) -> EligibilityResult:
    return EligibilityResult(False, reason, agent_key=agent_key, status_key=status_key)
