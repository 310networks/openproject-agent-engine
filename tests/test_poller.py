"""Tests for the poller: dispatch ordering, candidate filtering, and dry-run."""

from __future__ import annotations

import json
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
) -> tuple[Poller, FakeOpenProject]:
    fake = make_fake(
        work_packages=work_packages,
        relations=relations or [],
        priorities=PRIORITIES,
    )
    cfg = config or make_config()
    client = OpenProjectClient(BASE_URL, TOKEN, transport=fake.transport)
    resolution = await resolve_workflow(client, cfg)
    poller = Poller(client, cfg, resolution, max_concurrent_runs=max_concurrent_runs)
    return poller, fake


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
    # Candidate statuses: ready_for_review=3, rejected=6, new=1.
    assert sorted(conditions["status"]["values"]) == ["1", "3", "6"]
    assert conditions["project"]["values"] == ["4"]


async def test_blocker_and_children_are_respected() -> None:
    work_packages = [
        wp(100, status=1, agent=11),          # eligible
        wp(104, status=1, agent=11),          # has a child -> skipped
        wp(300, status=1, parent=104, agent=11),
        wp(105, status=1, agent=11),          # has an unfinished blocker -> skipped
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
    assert skipped[105] == "has unfinished blocker(s) [500]"


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
