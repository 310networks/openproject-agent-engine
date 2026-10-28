"""Table-driven tests for every eligibility rule (PLAN section 8)."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest

from op_agent_engine.config import WorkflowConfig
from op_agent_engine.eligibility import (
    build_eligibility_context,
    evaluate_eligibility,
)
from op_agent_engine.openproject.client import OpenProjectClient
from op_agent_engine.openproject.resolve import resolve_workflow
from op_agent_engine.openproject.workpackage import WorkPackage
from tests.test_resolve import BASE_URL, TOKEN, make_fake, workflow_mapping

# Standard resolution: project demo=4, types Task=1/Feature=4/Bug=7,
# statuses New=1/In Progress=2/Ready for Review=3/In Testing=4/On Hold=5/
# Rejected=6/Closed=7, agent users impl=11/reviewer=12/engine=10,
# AGENT options IMPL=11/REVIEWER=22.


def config_with(**op_overrides: Any) -> WorkflowConfig:
    mapping = workflow_mapping()
    mapping["openproject"].update(op_overrides)
    return WorkflowConfig.from_mapping(mapping, environ={})


async def make_ctx(
    *,
    config: WorkflowConfig | None = None,
    max_concurrent_runs: int = 2,
) -> Any:
    fake = make_fake()
    cfg = config or config_with()
    client = OpenProjectClient(BASE_URL, TOKEN, transport=fake.transport)
    try:
        resolution = await resolve_workflow(client, cfg)
    finally:
        await client.aclose()
    return build_eligibility_context(
        cfg, resolution, priority_position={}, max_concurrent_runs=max_concurrent_runs
    )


def wp(**overrides: Any) -> WorkPackage:
    base: dict[str, Any] = {
        "id": 100,
        "display_id": "OP#100",
        "subject": "task",
        "project_id": 4,
        "type_id": 1,
        "type_name": "Task",
        "status_id": 1,
        "status_name": "New",
        "priority_id": 8,
        "priority_name": "Normal",
        "assignee_id": None,
        "agent_value": "IMPL",
    }
    base.update(overrides)
    return WorkPackage(**base)


# Each case: wp overrides, optional config overrides, optional external data
# passed to evaluate_eligibility, optional context overrides, and the expected
# outcome.
CASES: list[dict[str, Any]] = [
    # Happy path: New + Task + IMPL, unassigned.
    {
        "id": "happy_path_new_impl",
        "eligible": True,
        "agent_key": "IMPL",
        "status_key": "new",
    },
    # Rule 1: project not enabled or configured.
    {
        "id": "rule1_unknown_project",
        "wp": {"project_id": 999},
        "eligible": False,
        "reason": "project is not enabled or configured",
    },
    # Rule 2: type not eligible.
    {
        "id": "rule2_ineligible_type",
        "wp": {"type_id": 9, "type_name": "Milestone"},
        "eligible": False,
        "reason": "type 'Milestone' is not eligible",
    },
    # Rule 3: status not dispatchable.
    {
        "id": "rule3_in_progress",
        "wp": {"status_id": 2, "status_name": "In Progress"},
        "eligible": False,
        "reason": "status 'In Progress' is not dispatchable",
    },
    {
        "id": "rule3_in_testing",
        "wp": {"status_id": 4, "status_name": "In Testing"},
        "eligible": False,
        "reason": "status 'In Testing' is not dispatchable",
    },
    {
        "id": "rule3_on_hold",
        "wp": {"status_id": 5, "status_name": "On Hold"},
        "eligible": False,
        "reason": "status 'On Hold' is not dispatchable",
    },
    {
        "id": "rule3_closed",
        "wp": {"status_id": 7, "status_name": "Closed"},
        "eligible": False,
        "reason": "status 'Closed' is not dispatchable",
    },
    # Rule 4: AGENT field routing for New/Rejected.
    {
        "id": "rule4_agent_field_unset",
        "wp": {"agent_value": None},
        "eligible": False,
        "reason": "AGENT field is not set",
    },
    {
        "id": "rule4_agent_field_unknown_value",
        "wp": {"agent_value": "NOPE"},
        "eligible": False,
        "reason": "AGENT field value 'NOPE' is not a configured agent",
    },
    {
        "id": "rule4_rejected_routes_to_agent",
        "wp": {"status_id": 6, "status_name": "Rejected", "agent_value": "REVIEWER"},
        "eligible": True,
        "agent_key": "REVIEWER",
        "status_key": "rejected",
    },
    # Rule 4 (review): Ready for Review routes to the reviewer agent.
    {
        "id": "rule4_ready_for_review_routes_to_reviewer",
        "wp": {"status_id": 3, "status_name": "Ready for Review", "agent_value": None},
        "eligible": True,
        "agent_key": "REVIEWER",
        "status_key": "ready_for_review",
    },
    {
        "id": "rule4_ready_for_review_no_reviewer_configured",
        "wp": {"status_id": 3, "status_name": "Ready for Review"},
        "ctx_overrides": {"reviewer_agent_key": None},
        "eligible": False,
        "reason": "no reviewer agent configured",
    },
    # Rule 5: not assigned to a human.
    {
        "id": "rule5_assigned_to_human",
        "wp": {"assignee_id": 99},
        "eligible": False,
        "reason": "assigned to a human",
    },
    {
        "id": "rule5_assigned_to_agent_user_ok",
        "wp": {"assignee_id": 11},
        "eligible": True,
        "agent_key": "IMPL",
        "status_key": "new",
    },
    {
        "id": "rule5_skip_disabled_assigned_to_human_ok",
        "wp": {"assignee_id": 99},
        "config": config_with(skip_if_assigned_to_human=False),
        "eligible": True,
        "agent_key": "IMPL",
        "status_key": "new",
    },
    # Rule 6: blockers and children.
    {
        "id": "rule6_unfinished_blocker",
        "external": {"blocker_status_keys": [(500, "new")]},
        "eligible": False,
        "reason": "has unfinished blocker(s) [500]",
    },
    {
        "id": "rule6_multiple_unfinished_blockers",
        "external": {"blocker_status_keys": [(502, "new"), (501, "in_progress")]},
        "eligible": False,
        "reason": "has unfinished blocker(s) [501, 502]",
    },
    {
        "id": "rule6_closed_blocker_ok",
        "external": {"blocker_status_keys": [(500, "closed")]},
        "eligible": True,
        "agent_key": "IMPL",
        "status_key": "new",
    },
    {
        "id": "rule6_has_children",
        "external": {"has_children": True},
        "eligible": False,
        "reason": "has children",
    },
    {
        "id": "rule6_skip_children_disabled_ok",
        "external": {"has_children": True},
        "config": config_with(skip_work_packages_with_children=False),
        "eligible": True,
        "agent_key": "IMPL",
        "status_key": "new",
    },
    # Rule 7: no active run for this work package.
    {
        "id": "rule7_active_run_for_work_package",
        "external": {"active_run_work_package_ids": frozenset({100})},
        "eligible": False,
        "reason": "already has an active run",
    },
    # Rule 8: target agent has no active run.
    {
        "id": "rule8_active_run_for_agent",
        "external": {"active_run_agent_keys": frozenset({"IMPL"})},
        "eligible": False,
        "reason": "agent 'IMPL' already has an active run",
    },
    # Rule 9: global slot available.
    {
        "id": "rule9_global_slots_full",
        "external": {"global_slots_used": 2},
        "eligible": False,
        "reason": "no global run slot available",
    },
    {
        "id": "rule9_global_slot_available",
        "external": {"global_slots_used": 1},
        "eligible": True,
        "agent_key": "IMPL",
        "status_key": "new",
    },
]


@pytest.mark.parametrize("case", CASES, ids=[case["id"] for case in CASES])
async def test_eligibility_rule(case: dict[str, Any]) -> None:
    ctx = await make_ctx(
        config=case.get("config"),
        max_concurrent_runs=case.get("max_concurrent_runs", 2),
    )
    if case.get("ctx_overrides"):
        ctx = replace(ctx, **case["ctx_overrides"])

    result = evaluate_eligibility(wp(**case.get("wp", {})), ctx, **case.get("external", {}))

    assert result.eligible is case["eligible"], (
        f"eligible={result.eligible} reason={result.reason!r}"
    )
    if case["eligible"]:
        assert result.reason is None
        assert result.agent_key == case["agent_key"]
        assert result.status_key == case["status_key"]
    else:
        assert result.reason == case["reason"]


async def test_build_context_derives_eligible_types_and_agent_users() -> None:
    ctx = await make_ctx()

    # demo=4 enables Task=1, Feature=4, Bug=7 (all eligible).
    assert ctx.enabled_project_ids == frozenset({4})
    assert ctx.eligible_type_ids == {4: frozenset({1, 4, 7})}
    # impl=11, reviewer=12, engine=10.
    assert ctx.agent_user_ids == frozenset({10, 11, 12})
    assert ctx.reviewer_agent_key == "REVIEWER"
    assert ctx.agent_field_id == "customField3"
    assert ctx.option_id_to_value == {11: "IMPL", 22: "REVIEWER"}
    assert ctx.status_key_by_id[1] == "new"
    assert ctx.status_key_by_id[3] == "ready_for_review"
