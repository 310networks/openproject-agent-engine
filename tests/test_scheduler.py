"""Tests for the scheduler and the claim protocol (PLAN section 9).

The claim protocol is exercised against the fake OpenProject, which mirrors
the verified live behaviour: ``lockVersion`` is required in the PATCH body,
a stale or missing value yields 409 ``UpdateConflict``, and a successful
patch applies the status/assignee links and increments the lockVersion.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from op_agent_engine.openproject.client import OpenProjectClient
from op_agent_engine.openproject.workpackage import parse_work_package
from op_agent_engine.poller import DispatchItem, DispatchPlan
from op_agent_engine.scheduler import Scheduler
from op_agent_engine.store import CANCELLED, RUNNING, Store
from tests.fakes.openproject import FakeOpenProject
from tests.fakes import non_write_requests
from tests.test_poller import PRIORITIES, wp
from tests.test_resolve import BASE_URL, TOKEN, make_config, make_fake, resolve

OPTION_ID_TO_VALUE = {11: "IMPL", 22: "REVIEWER"}


def make_item(
    work_package_id: int,
    *,
    status: int = 1,
    status_key: str = "new",
    agent_key: str = "IMPL",
    agent: int | None = 11,
) -> DispatchItem:
    return make_item_from_entry(
        wp(work_package_id, status=status, agent=agent),
        status_key=status_key,
        agent_key=agent_key,
    )


def make_item_from_entry(
    entry: dict[str, Any],
    *,
    status_key: str = "new",
    agent_key: str = "IMPL",
) -> DispatchItem:
    """Build an item from an entry already normalised by the fake.

    Unlike :func:`make_item`, the resulting work package carries the
    ``lockVersion`` the fake holds, which is what the poller would produce.
    """
    work_package = parse_work_package(entry, "customField3", OPTION_ID_TO_VALUE)
    return DispatchItem(
        work_package=work_package, agent_key=agent_key, status_key=status_key
    )


def plan(*items: DispatchItem) -> DispatchPlan:
    return DispatchPlan(items=tuple(items), skipped=())


async def make_scheduler(
    tmp_path: Path,
    *,
    work_packages: list[dict[str, Any]] | None = None,
    max_concurrent_runs: int = 2,
    client_kwargs: dict[str, Any] | None = None,
    **fake_overrides: Any,
) -> tuple[Scheduler, FakeOpenProject, Store, OpenProjectClient]:
    fake = make_fake(
        work_packages=work_packages or [], priorities=PRIORITIES, **fake_overrides
    )
    config = make_config()
    resolution = await resolve(fake, config)
    client = OpenProjectClient(
        BASE_URL, TOKEN, transport=fake.transport, **(client_kwargs or {})
    )
    store = Store(tmp_path / "engine.db")
    scheduler = Scheduler(
        client, config, resolution, store, max_concurrent_runs=max_concurrent_runs
    )
    return scheduler, fake, store, client


def seed_entries(*specs: dict[str, Any]) -> list[dict[str, Any]]:
    return list(specs)


def preexisting_run(
    store: Store,
    *,
    work_package_id: int,
    agent: str,
    kind: str = "implement",
) -> None:
    store.begin_run(
        work_package_id=work_package_id,
        project_id=4,
        agent=agent,
        kind=kind,
        attempt=1,
        branch=f"op-4-{work_package_id}",
        run_dir_template="/tmp/runs/{id}",
    )


# -- happy paths -----------------------------------------------------------


async def test_claim_new_work_package_implements(tmp_path: Path) -> None:
    scheduler, fake, store, client = await make_scheduler(
        tmp_path, work_packages=seed_entries(wp(100, status=1, agent=11))
    )
    try:
        report = await scheduler.schedule(plan(make_item(100)))

        assert report.claimed_count == 1
        assert report.skipped_count == 0
        claimed = report.claimed[0]
        assert claimed.run_id == 1
        assert claimed.kind == "implement"
        assert claimed.attempt == 1
        assert claimed.branch == "op-4-100"
        assert claimed.run_dir == store.runs_dir / "1"

        # The PATCH carried the current lockVersion and the claim targets.
        assert len(fake.patches) == 1
        body = fake.patches[0]["body"]
        assert body["lockVersion"] == 0
        assert body["_links"]["status"]["href"] == "/api/v3/statuses/2"
        assert body["_links"]["assignee"]["href"] == "/api/v3/users/11"

        # The work package moved to In Progress and is assigned to the agent.
        updated = fake.work_packages[0]
        assert updated["_links"]["status"]["href"] == "/api/v3/statuses/2"
        assert updated["_links"]["assignee"]["href"] == "/api/v3/users/11"
        assert updated["lockVersion"] == 1

        # The store holds the active run.
        run = store.get_run(1)
        assert run is not None
        assert run.state == RUNNING
        assert run.work_package_id == 100
        assert run.agent == "IMPL"
        assert run.kind == "implement"
        assert run.branch == "op-4-100"
    finally:
        await client.aclose()
        store.close()


async def test_claim_ready_for_review_reviews(tmp_path: Path) -> None:
    scheduler, fake, store, client = await make_scheduler(
        tmp_path,
        work_packages=seed_entries(wp(200, status=3, agent=22)),
    )
    try:
        report = await scheduler.schedule(
            plan(
                make_item(
                    200,
                    status=3,
                    status_key="ready_for_review",
                    agent_key="REVIEWER",
                    agent=22,
                )
            )
        )

        assert report.claimed_count == 1
        assert report.claimed[0].kind == "review"
        body = fake.patches[0]["body"]
        # Review moves the work package to In Testing and assigns the reviewer.
        assert body["_links"]["status"]["href"] == "/api/v3/statuses/4"
        assert body["_links"]["assignee"]["href"] == "/api/v3/users/12"
    finally:
        await client.aclose()
        store.close()


async def test_claim_rejected_work_package_fixes(tmp_path: Path) -> None:
    scheduler, fake, store, client = await make_scheduler(
        tmp_path, work_packages=seed_entries(wp(300, status=6, agent=11))
    )
    try:
        report = await scheduler.schedule(
            plan(make_item(300, status=6, status_key="rejected"))
        )

        assert report.claimed_count == 1
        assert report.claimed[0].kind == "fix"
        body = fake.patches[0]["body"]
        assert body["_links"]["status"]["href"] == "/api/v3/statuses/2"
    finally:
        await client.aclose()
        store.close()


# -- conflict handling -----------------------------------------------------


async def test_stale_lock_is_retried_once_with_fresh_version(tmp_path: Path) -> None:
    entries = seed_entries(wp(100, status=1, agent=11))
    scheduler, fake, store, client = await make_scheduler(
        tmp_path, work_packages=entries
    )
    try:
        item = make_item_from_entry(entries[0])
        # Someone else bumped the lockVersion between the poll and the claim.
        assert item.work_package.lock_version == 0
        fake.work_packages[0]["lockVersion"] = 5

        report = await scheduler.schedule(plan(item))

        assert report.claimed_count == 1
        # One successful PATCH; two PATCH requests (stale, then fresh).
        assert len(fake.patches) == 1
        patch_requests = [
            request for request in fake.requests if request.method == "PATCH"
        ]
        assert len(patch_requests) == 2
        assert fake.work_packages[0]["lockVersion"] == 6
    finally:
        await client.aclose()
        store.close()


async def test_conflict_with_status_change_aborts_the_claim(tmp_path: Path) -> None:
    entries = seed_entries(wp(100, status=1, agent=11))
    scheduler, fake, store, client = await make_scheduler(
        tmp_path, work_packages=entries
    )
    try:
        item = make_item_from_entry(entries[0])
        assert item.work_package.lock_version == 0
        # The work package moved on (e.g. someone closed it) before the claim.
        fake.work_packages[0]["_links"]["status"] = {"href": "/api/v3/statuses/7"}
        fake.work_packages[0]["lockVersion"] = 1

        report = await scheduler.schedule(plan(item))

        assert report.claimed_count == 0
        assert report.skipped_count == 1
        assert "status changed" in report.skipped[0].reason

        # The run row was cancelled; nothing is active.
        assert store.active_run_count() == 0
        run = store.get_run(1)
        assert run is not None
        assert run.state == CANCELLED
        assert "claim aborted" in (run.outcome or "")
        # No successful patch happened.
        assert fake.patches == []
    finally:
        await client.aclose()
        store.close()


async def test_missing_lock_version_is_fetched_before_the_patch(tmp_path: Path) -> None:
    # The candidate entry carries no lockVersion; the scheduler must fetch
    # the work package first.
    scheduler, fake, store, client = await make_scheduler(
        tmp_path, work_packages=seed_entries(wp(100, status=1, agent=11))
    )
    try:
        item = make_item(100)
        assert item.work_package.lock_version is None
        report = await scheduler.schedule(plan(item))

        assert report.claimed_count == 1
        assert fake.patches[0]["body"]["lockVersion"] == 0
        # A single-work-package GET happened before the PATCH.
        gets = [
            request
            for request in fake.requests
            if request.method == "GET"
            and request.url.path == "/api/v3/work_packages/100"
        ]
        assert len(gets) == 1
    finally:
        await client.aclose()
        store.close()


async def test_patch_error_cancels_the_run(tmp_path: Path) -> None:
    scheduler, fake, store, client = await make_scheduler(
        tmp_path,
        work_packages=seed_entries(wp(100, status=1, agent=11)),
        patch_error=(500, "boom"),
        client_kwargs={"max_attempts": 1},  # no retry/backoff in the test
    )
    try:
        report = await scheduler.schedule(plan(make_item(100)))

        assert report.claimed_count == 0
        assert report.skipped_count == 1
        assert "patch failed" in report.skipped[0].reason
        run = store.get_run(1)
        assert run is not None
        assert run.state == CANCELLED
        assert fake.patches == []
    finally:
        await client.aclose()
        store.close()


async def test_deleted_work_package_aborts_the_claim(tmp_path: Path) -> None:
    scheduler, fake, store, client = await make_scheduler(
        tmp_path, work_packages=seed_entries(wp(100, status=1, agent=11))
    )
    try:
        item = make_item(100)
        fake.work_packages.clear()  # deleted between poll and claim

        report = await scheduler.schedule(plan(item))

        assert report.claimed_count == 0
        assert report.skipped_count == 1
        assert "no longer exists" in report.skipped[0].reason
        run = store.get_run(1)
        assert run is not None
        assert run.state == CANCELLED
    finally:
        await client.aclose()
        store.close()


# -- slot rules ------------------------------------------------------------


async def test_skips_work_package_with_an_active_run(tmp_path: Path) -> None:
    scheduler, fake, store, client = await make_scheduler(
        tmp_path, work_packages=seed_entries(wp(100, status=1, agent=11))
    )
    try:
        preexisting_run(store, work_package_id=100, agent="REVIEWER", kind="review")
        report = await scheduler.schedule(plan(make_item(100)))

        assert report.claimed_count == 0
        assert report.skipped[0].reason == "work package already has an active run"
        assert fake.patches == []
        assert store.active_run_count() == 1  # only the pre-existing run
    finally:
        await client.aclose()
        store.close()


async def test_skips_agent_with_an_active_run(tmp_path: Path) -> None:
    scheduler, fake, store, client = await make_scheduler(
        tmp_path, work_packages=seed_entries(wp(100, status=1, agent=11))
    )
    try:
        preexisting_run(store, work_package_id=999, agent="IMPL")
        report = await scheduler.schedule(plan(make_item(100)))

        assert report.claimed_count == 0
        assert report.skipped[0].reason == "agent 'IMPL' already has an active run"
        assert fake.patches == []
    finally:
        await client.aclose()
        store.close()


async def test_skips_when_no_global_slot_available(tmp_path: Path) -> None:
    scheduler, fake, store, client = await make_scheduler(
        tmp_path,
        work_packages=seed_entries(wp(100, status=1, agent=11)),
        max_concurrent_runs=1,
    )
    try:
        preexisting_run(store, work_package_id=999, agent="REVIEWER", kind="review")
        report = await scheduler.schedule(plan(make_item(100)))

        assert report.claimed_count == 0
        assert report.skipped[0].reason == "no global run slot available"
        assert fake.patches == []
    finally:
        await client.aclose()
        store.close()


async def test_second_item_for_same_agent_is_skipped_within_a_pass(tmp_path: Path) -> None:
    scheduler, _fake, store, client = await make_scheduler(
        tmp_path,
        work_packages=seed_entries(
            wp(100, status=1, agent=11), wp(101, status=1, agent=11)
        ),
    )
    try:
        report = await scheduler.schedule(plan(make_item(100), make_item(101)))

        assert report.claimed_count == 1
        assert report.claimed[0].item.work_package.id == 100
        assert report.skipped_count == 1
        assert report.skipped[0].item.work_package.id == 101
        assert report.skipped[0].reason == "agent 'IMPL' already has an active run"
    finally:
        await client.aclose()
        store.close()


async def test_global_limit_spans_agents_in_one_pass(tmp_path: Path) -> None:
    scheduler, _fake, store, client = await make_scheduler(
        tmp_path,
        work_packages=seed_entries(
            wp(100, status=1, agent=11), wp(101, status=3, agent=22)
        ),
        max_concurrent_runs=1,
    )
    try:
        report = await scheduler.schedule(
            plan(
                make_item(100),
                make_item(
                    101,
                    status=3,
                    status_key="ready_for_review",
                    agent_key="REVIEWER",
                    agent=22,
                ),
            )
        )

        assert report.claimed_count == 1
        assert report.claimed[0].item.work_package.id == 100
        assert report.skipped[0].reason == "no global run slot available"
    finally:
        await client.aclose()
        store.close()


# -- dry run ---------------------------------------------------------------


async def test_dry_run_claims_nothing(tmp_path: Path) -> None:
    scheduler, fake, store, client = await make_scheduler(
        tmp_path,
        work_packages=seed_entries(
            wp(100, status=1, agent=11), wp(101, status=3, agent=22)
        ),
    )
    try:
        report = await scheduler.schedule(
            plan(
                make_item(100),
                make_item(
                    101,
                    status=3,
                    status_key="ready_for_review",
                    agent_key="REVIEWER",
                    agent=22,
                ),
            ),
            dry_run=True,
        )

        # Both items "would be claimed" (slots are simulated in memory).
        assert report.claimed_count == 2
        assert all(claimed.run_id is None for claimed in report.claimed)
        assert all(claimed.run_dir is None for claimed in report.claimed)
        assert report.claimed[0].kind == "implement"
        assert report.claimed[0].branch == "op-4-100"
        assert report.claimed[1].kind == "review"

        # Nothing was written anywhere.
        assert fake.patches == []
        assert len(non_write_requests(fake.requests)) == len(fake.requests)
        assert store.active_run_count() == 0
    finally:
        await client.aclose()
        store.close()


async def test_dry_run_simulates_slot_consumption_within_a_pass(
    tmp_path: Path,
) -> None:
    scheduler, fake, store, client = await make_scheduler(
        tmp_path,
        work_packages=seed_entries(
            wp(100, status=1, agent=11), wp(101, status=1, agent=11)
        ),
    )
    try:
        report = await scheduler.schedule(
            plan(make_item(100), make_item(101)), dry_run=True
        )

        # One active run per agent, even in the dry-run simulation.
        assert report.claimed_count == 1
        assert report.claimed[0].item.work_package.id == 100
        assert report.skipped[0].reason == "agent 'IMPL' already has an active run"
        assert fake.patches == []
    finally:
        await client.aclose()
        store.close()


async def test_dry_run_respects_existing_active_runs(tmp_path: Path) -> None:
    scheduler, fake, store, client = await make_scheduler(
        tmp_path, work_packages=seed_entries(wp(100, status=1, agent=11))
    )
    try:
        preexisting_run(store, work_package_id=100, agent="IMPL")
        report = await scheduler.schedule(plan(make_item(100)), dry_run=True)

        assert report.claimed_count == 0
        assert report.skipped[0].reason == "work package already has an active run"
        assert fake.patches == []
    finally:
        await client.aclose()
        store.close()


# -- report ----------------------------------------------------------------


async def test_report_to_dict(tmp_path: Path) -> None:
    scheduler, _fake, store, client = await make_scheduler(
        tmp_path,
        work_packages=seed_entries(
            wp(100, status=1, agent=11), wp(101, status=1, agent=11)
        ),
    )
    try:
        report = await scheduler.schedule(plan(make_item(100), make_item(101)))

        payload = report.to_dict()
        assert payload["claimed"][0] == {
            "run_id": 1,
            "work_package_id": 100,
            "agent": "IMPL",
            "kind": "implement",
            "branch": "op-4-100",
        }
        assert payload["skipped"][0]["work_package_id"] == 101
        assert "IMPL" in payload["skipped"][0]["reason"]
    finally:
        await client.aclose()
        store.close()
