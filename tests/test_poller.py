"""Tests for the poller: dispatch ordering, candidate filtering, and dry-run."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from op_agent_engine.config import WorkflowConfig
from op_agent_engine.openproject.client import OpenProjectClient
from op_agent_engine.openproject.resolve import resolve_workflow
from op_agent_engine.poller import Poller
from tests.fakes import non_write_requests
from tests.fakes.openproject import FakeOpenProject
from tests.test_resolve import (
    BASE_URL,
    TOKEN,
    make_config,
    make_fake,
    schema,
    schemas,
    workflow_mapping,
)


def _set_agent_tokens(monkeypatch) -> None:
    for name in (
        "OP_TOKEN_FRONTEND",
        "OP_TOKEN_BACKEND",
        "OP_TOKEN_DESIGNER",
        "OP_TOKEN_VFX",
        "OP_TOKEN_AUDIO",
        "OP_TOKEN_REVIEWER",
    ):
        monkeypatch.setenv(name, "token")


PRIORITIES: list[dict[str, Any]] = [
    {"id": 7, "name": "Low", "position": 1},
    {"id": 8, "name": "Normal", "position": 2},
    {"id": 9, "name": "High", "position": 3},
    {"id": 10, "name": "Immediate", "position": 4},
]


def wp(
    id: int,
    *,
    status: int = 1,
    ptype: int = 1,
    prio: int = 8,
    assignee: int | None = None,
    agent: int | None = None,
    parent: int | None = None,
    proj: int = 4,
) -> dict[str, Any]:
    links: dict[str, Any] = {
        "status": {"href": f"/api/v3/statuses/{status}"},
        "type": {"href": f"/api/v3/types/{ptype}"},
        "priority": {"href": f"/api/v3/priorities/{prio}"},
        "project": {"href": f"/api/v3/projects/{proj}"},
    }
    if assignee is not None:
        links["assignee"] = {"href": f"/api/v3/users/{assignee}"}
    if parent is not None:
        links["parent"] = {"href": f"/api/v3/work_packages/{parent}"}
    entry: dict[str, Any] = {
        "id": id,
        "displayId": f"OP#{id}",
        "subject": f"wp {id}",
        "_links": links,
    }
    if agent is not None:
        entry["customField3"] = [
            {"_links": {"self": {"href": f"/api/v3/custom_options/{agent}"}}, "title": "x"}
        ]
    return entry


async def make_poller(
    *,
    work_packages: list[dict[str, Any]],
    relations: list[dict[str, Any]] | None = None,
    config: WorkflowConfig | None = None,
    max_concurrent_runs: int = 2,
    blocker_refresh_seconds: float = 0.0,
    clock: Callable[[], float] | None = None,
) -> tuple[Poller, FakeOpenProject]:
    fake = make_fake(
        work_packages=work_packages,
        relations=relations or [],
        priorities=PRIORITIES,
    )
    cfg = config or make_config()
    client = OpenProjectClient(BASE_URL, TOKEN, transport=fake.transport)
    resolution = await resolve_workflow(client, cfg)
    poller = Poller(
        client,
        cfg,
        resolution,
        max_concurrent_runs=max_concurrent_runs,
        blocker_refresh_seconds=blocker_refresh_seconds,
        **({"clock": clock} if clock is not None else {}),
    )
    return poller, fake


def work_package_queries(
    fake: FakeOpenProject, field: str
) -> list[Any]:
    """Work package collection requests whose filters contain ``field``."""
    matches: list[Any] = []
    for request in fake.requests:
        if request.url.path != "/api/v3/work_packages":
            continue
        raw = request.url.params.get("filters")
        if raw is None:
            continue
        conditions = json.loads(raw)
        if any(field in condition for condition in conditions):
            matches.append(request)
    return matches


def relation_queries(fake: FakeOpenProject) -> list[Any]:
    return [
        request
        for request in fake.requests
        if request.url.path == "/api/v3/relations"
    ]


async def test_dispatch_ordering() -> None:
    work_packages = [
        wp(200, status=3, prio=8, agent=None),   # ready_for_review Normal
        wp(201, status=3, prio=9, agent=None),   # ready_for_review High
        wp(150, status=6, prio=8, agent=11),     # rejected IMPL
        wp(100, status=1, prio=8, agent=11),     # new Normal
        wp(101, status=1, prio=9, agent=11),     # new High
        wp(102, status=1, prio=8, agent=11),     # new Normal
        wp(103, status=1, prio=10, agent=11),    # new Immediate
    ]
    poller, _ = await make_poller(work_packages=work_packages)
    plan = await poller.poll()

    # dispatch_priority: ready_for_review, rejected, new.
    # Within a status: higher OP priority first, then lower id first.
    assert [item.work_package.id for item in plan.items] == [
        201, 200, 150, 103, 101, 100, 102
    ]
    assert [item.status_key for item in plan.items] == [
        "ready_for_review",
        "ready_for_review",
        "rejected",
        "new",
        "new",
        "new",
        "new",
    ]
    # ready_for_review routes to the reviewer agent.
    assert plan.items[0].agent_key == "REVIEWER"
    assert plan.items[2].agent_key == "IMPL"


async def test_candidate_query_filters_by_type_and_status() -> None:
    work_packages = [
        wp(100, status=1, agent=11),          # New Task IMPL -> candidate
        wp(200, status=2, agent=11),          # In Progress -> not a candidate status
        wp(300, status=1, ptype=9, agent=11), # Milestone -> not an eligible type
    ]
    poller, fake = await make_poller(work_packages=work_packages)
    plan = await poller.poll()

    assert [item.work_package.id for item in plan.items] == [100]
    assert plan.skipped == ()

    # The candidate query must filter by eligible type ids and candidate statuses.
    candidate_requests = [
        request
        for request in fake.requests
        if request.url.path == "/api/v3/work_packages" and "filters" in request.url.params
    ]
    assert candidate_requests, "no candidate query was issued"
    filters = json.loads(candidate_requests[0].url.params["filters"])
    conditions: dict[str, dict[str, Any]] = {}
    for entry in filters:
        for field, spec in entry.items():
            conditions[field] = spec
    # Eligible types for demo: Task=1, Feature=4, Bug=7.
    assert conditions["type"]["values"] == ["1", "4", "7"]
    # Candidate statuses: ready_for_review=3, rejected=6, new=1, blocked=8.
    assert sorted(conditions["status"]["values"]) == ["1", "3", "6", "8"]
    assert conditions["project"]["values"] == ["4"]


async def test_blocker_and_children_are_respected() -> None:
    work_packages = [
        wp(100, status=1, agent=11),          # eligible
        wp(104, status=1, agent=11),          # has a child -> skipped
        wp(300, status=1, parent=104, agent=11),
        wp(105, status=1, agent=11),          # has an unfinished blocker -> Blocked
        wp(500, status=1, agent=11),          # the blocker (New)
    ]
    relations = [
        {"id": 1, "type": "blocks", "_embedded": {"from": {"id": 500}, "to": {"id": 105}}},
    ]
    poller, _ = await make_poller(work_packages=work_packages, relations=relations)
    plan = await poller.poll()

    assert [item.work_package.id for item in plan.items] == [100, 300, 500]
    skipped = {wp.id: reason for wp, reason in plan.skipped}
    assert skipped[104] == "has children"
    assert 105 not in skipped
    assert [action.work_package.id for action in plan.blocked_actions] == [105]
    action = plan.blocked_actions[0]
    assert action.agent_key == "IMPL"
    assert [(blocker.id, blocker.status_key) for blocker in action.blockers] == [
        (500, "new")
    ]


async def test_new_work_package_with_closed_blocker_is_dispatched() -> None:
    work_packages = [
        wp(105, status=1, agent=11),
        wp(500, status=7, agent=11),  # Closed
    ]
    relations = [
        {"id": 1, "type": "blocks", "_embedded": {"from": {"id": 500}, "to": {"id": 105}}},
    ]
    poller, _ = await make_poller(work_packages=work_packages, relations=relations)
    plan = await poller.poll()

    assert plan.blocked_actions == ()
    assert [(item.work_package.id, item.status_key) for item in plan.items] == [
        (105, "new")
    ]


async def test_blocked_work_package_unblocks_only_when_all_blockers_are_closed() -> None:
    work_packages = [
        wp(105, status=8, agent=11),  # Blocked
        wp(500, status=7),            # Closed
        wp(501, status=2),            # In Progress
    ]
    relations = [
        {"id": 1, "type": "blocks", "_embedded": {"from": {"id": 500}, "to": {"id": 105}}},
        {"id": 2, "type": "blocks", "_embedded": {"from": {"id": 501}, "to": {"id": 105}}},
    ]
    poller, _ = await make_poller(work_packages=work_packages, relations=relations)
    plan = await poller.poll()

    assert plan.items == ()
    skipped = {work_package.id: reason for work_package, reason in plan.skipped}
    assert skipped[105] == "has unfinished blocker(s) [501]"

    # Closing the last blocker makes the ticket dispatchable again.
    work_packages[2]["_links"]["status"] = {"href": "/api/v3/statuses/7"}
    work_packages[2]["lockVersion"] = work_packages[2].get("lockVersion", 0) + 1
    poller, _ = await make_poller(work_packages=work_packages, relations=relations)
    plan = await poller.poll()

    assert plan.blocked_actions == ()
    assert [(item.work_package.id, item.status_key) for item in plan.items] == [
        (105, "blocked")
    ]
    assert plan.items[0].agent_key == "IMPL"


async def test_runtime_state_rejects_before_dependency_queries() -> None:
    work_packages = [wp(100, status=1, agent=11)]
    poller, fake = await make_poller(work_packages=work_packages)

    # An active run for this work package rejects it immediately.
    plan = await poller.poll(active_run_work_package_ids=frozenset({100}))
    assert plan.items == ()
    skipped = {work_package.id: reason for work_package, reason in plan.skipped}
    assert skipped[100] == "already has an active run"
    assert relation_queries(fake) == []
    assert work_package_queries(fake, "parent") == []
    assert work_package_queries(fake, "id") == []

    # A full global slot budget rejects it the same way.
    fake.requests.clear()
    plan = await poller.poll(global_slots_used=2)
    assert plan.items == ()
    skipped = {work_package.id: reason for work_package, reason in plan.skipped}
    assert skipped[100] == "no global run slot available"
    assert relation_queries(fake) == []


def test_all_agents_busy_is_recognised_by_the_engine(tmp_path) -> None:
    from op_agent_engine.engine import Engine
    from tests.test_engine import make_settings

    engine = Engine(make_config(), make_settings(tmp_path))
    assert engine._idle_reason(0, {}) is None
    assert engine._idle_reason(1, {"IMPL": 1}) == "all 1 run slot(s) in use"

    roomy = Engine(
        make_config(),
        make_settings(
            tmp_path, max_concurrent_runs=5, max_runs_per_agent=1
        ),
    )
    assert roomy._idle_reason(1, {"IMPL": 1}) is None
    assert (
        roomy._idle_reason(2, {"IMPL": 1, "REVIEWER": 1})
        == "every agent already has an active run"
    )

    paused = Engine(make_config(), make_settings(tmp_path, engine_paused=True))
    assert paused._idle_reason(0, {}) == "engine is paused"


async def test_dependency_queries_are_batched_and_sequential() -> None:
    work_packages = [
        wp(100, status=1, agent=11),
        wp(101, status=1, agent=11),
        wp(102, status=1, agent=11),
        wp(500, status=2),
        wp(501, status=2),
        wp(502, status=2),
    ]
    relations = [
        # 500 blocks 100, 501 blocks 101, 102 blocked by 502.
        {"id": 1, "type": "blocks", "_embedded": {"from": {"id": 500}, "to": {"id": 100}}},
        {"id": 2, "type": "blocks", "_embedded": {"from": {"id": 501}, "to": {"id": 101}}},
        {"id": 3, "type": "blocked", "_embedded": {"from": {"id": 102}, "to": {"id": 502}}},
    ]
    poller, fake = await make_poller(
        work_packages=work_packages, relations=relations
    )
    plan = await poller.poll()

    assert sorted(
        action.work_package.id for action in plan.blocked_actions
    ) == [100, 101, 102]
    assert {
        (action.work_package.id, action.blockers[0].id)
        for action in plan.blocked_actions
    } == {(100, 500), (101, 501), (102, 502)}

    # One relations query, one blocker lookup and one children query, however
    # many candidates there are.
    assert len(relation_queries(fake)) == 1
    blocker_queries = work_package_queries(fake, "id")
    assert len(blocker_queries) == 1
    assert sorted(
        json.loads(blocker_queries[0].url.params["filters"])[0]["id"]["values"]
    ) == ["500", "501", "502"]
    assert len(work_package_queries(fake, "parent")) == 1


async def test_dependency_checks_use_their_own_timer() -> None:
    now = [1000.0]
    work_packages = [wp(105, status=8, agent=11), wp(500, status=2)]
    relations = [
        {"id": 1, "type": "blocks", "_embedded": {"from": {"id": 500}, "to": {"id": 105}}}
    ]
    poller, fake = await make_poller(
        work_packages=work_packages,
        relations=relations,
        blocker_refresh_seconds=300,
        clock=lambda: now[0],
    )

    assert (await poller.poll()).items == ()
    assert len(relation_queries(fake)) == 1

    # Inside the refresh window a poll costs no dependency requests at all.
    now[0] += 60
    plan = await poller.poll()
    assert plan.items == ()
    assert {work_package.id: reason for work_package, reason in plan.skipped}[105] == (
        "has unfinished blocker(s) [500]"
    )
    assert len(relation_queries(fake)) == 1

    # The blocker closes, but the cached snapshot is reused until the timer
    # fires; the ticket is not dispatched on stale data either way.
    work_packages[1]["_links"]["status"] = {"href": "/api/v3/statuses/7"}
    assert (await poller.poll()).items == ()
    assert len(relation_queries(fake)) == 1

    # After the interval the snapshot is refreshed and the ticket unblocks.
    now[0] += 300
    plan = await poller.poll()
    assert [item.work_package.id for item in plan.items] == [105]
    assert len(relation_queries(fake)) == 2


async def test_children_query_is_skipped_when_the_rule_is_off() -> None:
    config = make_config(
        openproject={"skip_work_packages_with_children": False}
    )
    work_packages = [wp(100, status=1, agent=11), wp(300, status=1, agent=11, parent=100)]
    poller, fake = await make_poller(work_packages=work_packages, config=config)
    plan = await poller.poll()

    assert {item.work_package.id for item in plan.items} == {100, 300}
    assert work_package_queries(fake, "parent") == []


async def test_mis_scoped_agent_value_is_skipped_without_claiming() -> None:
    mapping = workflow_mapping()
    mapping["projects"].append(
        {
            "identifier": "other",
            "repo": {"github": "owner/other", "dir": "other"},
            "enabled": True,
        }
    )
    # IMPL may only work in demo; REVIEWER is wildcard.
    mapping["agents"]["IMPL"]["projects"] = ["demo"]
    mapping["agents"]["REVIEWER"]["projects"] = ["*"]
    config = WorkflowConfig.from_mapping(mapping, environ={})

    fake = make_fake(
        projects=[
            {"id": 4, "identifier": "demo", "name": "Demo"},
            {"id": 5, "identifier": "other", "name": "Other"},
        ],
        types={
            "demo": [{"id": 1, "name": "Task"}],
            "other": [{"id": 1, "name": "Task"}],
        },
        schemas={**schemas(), "5-1": schema()},
        work_packages=[
            wp(100, status=1, agent=11),
            wp(300, status=1, agent=11, proj=5),
        ],
        priorities=PRIORITIES,
    )
    client = OpenProjectClient(BASE_URL, TOKEN, transport=fake.transport)
    try:
        resolution = await resolve_workflow(client, config)
        poller = Poller(client, config, resolution, max_concurrent_runs=2)
        plan = await poller.poll()
    finally:
        await client.aclose()

    assert [item.work_package.id for item in plan.items] == [100]
    skipped = {work_package.id: reason for work_package, reason in plan.skipped}
    assert skipped[300] == "agent 'IMPL' has no access to project 5"
    # The poller is read-only; a mis-scoped ticket is never modified.
    assert fake.patches == []


async def test_dry_run_writes_nothing() -> None:
    work_packages = [wp(100, status=1, agent=11), wp(200, status=3, agent=None)]
    poller, fake = await make_poller(work_packages=work_packages)
    plan = await poller.poll()

    assert len(plan.items) == 2
    # A dry run only reads; it must not create, update or delete anything.
    # (The form endpoint is a read-only schema preview, not a write.)
    assert len(non_write_requests(fake.requests)) == len(fake.requests), [
        f"{request.method} {request.url.path}" for request in fake.requests
    ]


def test_cli_dry_run_logs_plan_and_writes_nothing(tmp_path, monkeypatch) -> None:
    import op_agent_engine.cli as cli
    import op_agent_engine.engine as engine

    work_packages = [wp(100, status=1, agent=11), wp(200, status=3, agent=None)]
    fake = make_fake(work_packages=work_packages, priorities=PRIORITIES)

    def factory(base_url: str, token: str, **kwargs: Any) -> OpenProjectClient:
        return OpenProjectClient(base_url, token, transport=fake.transport, **kwargs)

    monkeypatch.setattr(engine, "OpenProjectClient", factory)
    monkeypatch.setenv("OP_BASE_URL", BASE_URL)
    monkeypatch.setenv("OP_ENGINE_TOKEN", TOKEN)
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    _set_agent_tokens(monkeypatch)
    monkeypatch.setattr(cli, "_load_config", lambda settings=None: make_config())

    exit_code = cli.main(["dry-run"])

    assert exit_code == 0
    assert len(non_write_requests(fake.requests)) == len(fake.requests)
    # Dry-run must not create the store file.
    assert not (tmp_path / "data" / "engine.db").exists()


def test_cli_once_runs_a_poll_cycle(tmp_path, monkeypatch) -> None:
    import op_agent_engine.cli as cli
    import op_agent_engine.engine as engine

    work_packages = [wp(100, status=1, agent=11)]
    fake = make_fake(work_packages=work_packages, priorities=PRIORITIES)

    def factory(base_url: str, token: str, **kwargs: Any) -> OpenProjectClient:
        return OpenProjectClient(base_url, token, transport=fake.transport, **kwargs)

    monkeypatch.setattr(engine, "OpenProjectClient", factory)
    monkeypatch.setenv("OP_BASE_URL", BASE_URL)
    monkeypatch.setenv("OP_ENGINE_TOKEN", TOKEN)
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    _set_agent_tokens(monkeypatch)
    monkeypatch.setattr(cli, "_load_config", lambda settings=None: make_config())

    exit_code = cli.main(["once"])

    assert exit_code == 0
    # In M3, `once` claims: exactly one PATCH, and the work package is
    # moved to In Progress and assigned to the agent.
    patches = [request for request in fake.requests if request.method == "PATCH"]
    assert len(patches) == 1
    assert patches[0].url.path == "/api/v3/work_packages/100"
    claimed = fake.work_packages[0]
    assert claimed["_links"]["status"]["href"] == "/api/v3/statuses/2"
    assert claimed["_links"]["assignee"]["href"] == "/api/v3/users/11"
    assert claimed["lockVersion"] == 1
