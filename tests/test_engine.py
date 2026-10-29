"""Integration tests for the engine loop (PLAN section 3, milestones M4/M5).

The engine runs against the fake OpenProject with a real store and a
dispatcher whose runner is a fake (no process is spawned) and whose
workspace preparation is stubbed (no git).
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from pydantic import SecretStr

from op_agent_engine.config import EngineSettings
from op_agent_engine.dispatcher import Dispatcher
from op_agent_engine.engine import Engine
from op_agent_engine.github import PullRequest
from op_agent_engine.openproject.client import OpenProjectClient
from op_agent_engine.openproject.resolve import OpenProjectResolution
from op_agent_engine.prompts import PromptRenderer
from op_agent_engine.provision import TokenVerificationError
from op_agent_engine.runners.base import OnSpawned, RunRequest, RunResult
from op_agent_engine.store import FAILED, INTERRUPTED, SUCCEEDED, Store
from op_agent_engine.workspace import Workspace
from tests.fakes.openproject import FakeOpenProject
from tests.test_poller import PRIORITIES, wp
from tests.test_resolve import BASE_URL, TOKEN, make_config, make_fake

STATUS_NEW = 1
STATUS_IN_PROGRESS = 2
STATUS_READY_FOR_REVIEW = 3
STATUS_IN_TESTING = 4
STATUS_ON_HOLD = 5
STATUS_REJECTED = 6
STATUS_CLOSED = 7

# The claim assigns the work package to the target agent's user (the
# "impl" login, user 11 in the fake), not to the engine user.
AGENT_USER_ID = 11

PR = PullRequest(
    number=7,
    url="https://github.com/owner/demo/pull/7",
    head_branch="op-4-100",
    state="open",
)

MERGED_PR = PullRequest(
    number=7,
    url="https://github.com/owner/demo/pull/7",
    head_branch="op-4-100",
    state="merged",
)

# The reviewer agent ("reviewer" login) in the fake.
REVIEWER_USER_ID = 12


class FakeRunner:
    def __init__(
        self,
        result: RunResult | None = None,
        *,
        delay: float = 0.0,
        on_run: Callable[[], None] | None = None,
    ) -> None:
        self.result = result
        self.delay = delay
        self.on_run = on_run
        self.requests: list[RunRequest] = []

    def build_request(
        self,
        agent_key: str,
        *,
        run_id: int,
        work_package_id: int,
        run_dir: Path,
        repo_dir: Path,
        workspace_dir: Path,
        branch: str,
        prompt_file: Path,
        timeout_seconds: float,
    ) -> RunRequest:
        request = RunRequest(
            argv=("fake-runner",),
            cwd=repo_dir,
            env={"PATH": "/usr/bin"},
            stdin_file=None,
            timeout_seconds=timeout_seconds,
            run_dir=run_dir,
            run_id=run_id,
            work_package_id=work_package_id,
            agent=agent_key,
        )
        self.requests.append(request)
        return request

    async def run(
        self, request: RunRequest, *, on_spawned: OnSpawned | None = None
    ) -> RunResult:
        if self.delay:
            await asyncio.sleep(self.delay)
        if on_spawned is not None:
            on_spawned(4321, 4321, "2026-02-10T00:00:00+00:00")
        if self.on_run is not None:
            self.on_run()
        assert self.result is not None
        return self.result


class FakeGitHub:
    def __init__(
        self,
        pr: PullRequest | None = None,
        merged: PullRequest | None = None,
    ) -> None:
        self.pr = pr
        self.merged = merged
        self.states: list[str] = []

    async def find_pr(
        self, repo: str, branch: str, *, state: str = "open"
    ) -> PullRequest | None:
        self.states.append(state)
        if state == "merged":
            return self.merged
        if state == "open":
            return self.pr
        return None


def make_result(tmp_path: Path, **kwargs: Any) -> RunResult:
    stdout_path = tmp_path / "stdout.log"
    stderr_path = tmp_path / "stderr.log"
    stdout_path.write_text(kwargs.pop("stdout", ""), encoding="utf-8")
    stderr_path.write_text(kwargs.pop("stderr", ""), encoding="utf-8")
    defaults: dict[str, Any] = {
        "exit_code": 0,
        "timed_out": False,
        "duration_seconds": 1.0,
        "stdout_path": stdout_path,
        "stderr_path": stderr_path,
        "result_line": None,
    }
    defaults.update(kwargs)
    return RunResult(**defaults)


def make_settings(tmp_path: Path, **overrides: Any) -> EngineSettings:
    base: dict[str, Any] = {
        "op_base_url": BASE_URL,
        "op_engine_token": SecretStr(TOKEN),
        "data_dir": tmp_path / "data",
        "workspace_root": tmp_path / "ws",
        "poll_interval_seconds": 1,
        "max_concurrent_runs": 1,
        "run_timeout_seconds": 360,
        "stop_grace_seconds": 5,
    }
    base.update(overrides)
    return EngineSettings(**base)


async def make_engine(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    work_packages: list[dict[str, Any]],
    runner: FakeRunner,
    gh: FakeGitHub | None = None,
    settings_overrides: dict[str, Any] | None = None,
    activities: list[dict[str, Any]] | None = None,
) -> tuple[Engine, FakeOpenProject, OpenProjectClient, Path]:
    fake = make_fake(
        work_packages=work_packages,
        priorities=PRIORITIES,
        activities=activities or [],
    )
    config = make_config()
    settings = make_settings(tmp_path, **(settings_overrides or {}))
    client = OpenProjectClient(
        BASE_URL, TOKEN, transport=fake.transport, max_attempts=1
    )

    # Stub the workspace preparation: no real git in these tests.
    agent_dir = tmp_path / "ws" / "impl"
    repo_dir = agent_dir / "repositories" / "demo"
    repo_dir.mkdir(parents=True)
    standard = agent_dir / "AGENTS.md"
    standard.write_text("STANDARD CONTENT", encoding="utf-8")
    workspace = Workspace(
        repo_dir=repo_dir, branch="op-4-100", standard_path=standard
    )

    async def fake_prepare_workspace(**kwargs: Any) -> Workspace:
        return workspace

    monkeypatch.setattr(
        "op_agent_engine.dispatcher.prepare_workspace", fake_prepare_workspace
    )

    prompts_dir = tmp_path / "prompts"
    prompts_dir.mkdir()
    (prompts_dir / "implement.txt.j2").write_text(
        "PROMPT {{ wp.subject }} branch={{ branch }}", encoding="utf-8"
    )
    (prompts_dir / "review.txt.j2").write_text(
        "REVIEW {{ wp.subject }} pr={{ pr.url }} review={{ review_number }}",
        encoding="utf-8",
    )
    (prompts_dir / "fix.txt.j2").write_text(
        "FIX {{ wp.subject }} attempt={{ attempt }} of {{ max_rejections }} "
        "feedback={{ feedback.text }}",
        encoding="utf-8",
    )
    (prompts_dir / "resume.txt.j2").write_text(
        "RESUME {{ wp.subject }} branch={{ branch }}", encoding="utf-8"
    )
    prompts = PromptRenderer(prompts_dir)

    def factory(
        resolution: OpenProjectResolution,
        store: Store,
        dispatcher_client: OpenProjectClient,
    ) -> Dispatcher:
        return Dispatcher(
            config=config,
            resolution=resolution,
            store=store,
            client=dispatcher_client,
            settings=settings,
            gh=gh if gh is not None else FakeGitHub(),
            runner=runner,
            prompts=prompts,
        )

    engine = Engine(config, settings, client=client, dispatcher_factory=factory)
    return engine, fake, client, settings.data_dir / "engine.db"


def status_href(fake: FakeOpenProject, work_package_id: int) -> str:
    entry = next(w for w in fake.work_packages if w.get("id") == work_package_id)
    return entry["_links"]["status"]["href"]


def assignee_href(fake: FakeOpenProject, work_package_id: int) -> str | None:
    entry = next(w for w in fake.work_packages if w.get("id") == work_package_id)
    assignee = entry["_links"].get("assignee")
    return assignee["href"] if assignee is not None else None


def runs_for(store: Store, work_package_id: int) -> list[Any]:
    return [
        run
        for run in store.finished_runs()
        if run.work_package_id == work_package_id
    ]


# -- the M4 happy path -----------------------------------------------------------


async def test_engine_polls_claims_and_executes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = FakeRunner(make_result(tmp_path))
    engine, fake, client, store_path = await make_engine(
        tmp_path,
        monkeypatch,
        work_packages=[wp(100, status=STATUS_NEW, agent=11)],
        runner=runner,
        gh=FakeGitHub(pr=PR),
    )
    try:
        exit_code = await engine.run(max_polls=1)
        assert exit_code == 0

        # The work package moved to Ready for Review and the agent user is
        # the assignee (set by the claim).
        assert status_href(fake, 100) == f"/api/v3/statuses/{STATUS_READY_FOR_REVIEW}"
        assert assignee_href(fake, 100) == f"/api/v3/users/{AGENT_USER_ID}"

        # The run was recorded as succeeded.
        store = Store(store_path)
        try:
            runs = runs_for(store, 100)
            assert len(runs) == 1
            assert runs[0].state == SUCCEEDED
            assert runs[0].outcome == "ready_for_review"
            assert runs[0].kind == "implement"
            assert runs[0].branch == "op-4-100"
        finally:
            store.close()

        # The lock is released after a clean stop.
        from op_agent_engine.store import EngineLock

        lock = EngineLock(engine.lock_path)
        assert lock.acquire()
        lock.release()
    finally:
        await client.aclose()


async def test_engine_blocked_run_posts_comment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = FakeRunner(
        make_result(tmp_path, result_line="RESULT: BLOCKED missing credentials")
    )
    engine, fake, client, store_path = await make_engine(
        tmp_path,
        monkeypatch,
        work_packages=[wp(100, status=STATUS_NEW, agent=11)],
        runner=runner,
        gh=FakeGitHub(pr=PR),
    )
    try:
        exit_code = await engine.run(max_polls=1)
        assert exit_code == 0
        assert status_href(fake, 100) == f"/api/v3/statuses/{STATUS_ON_HOLD}"
        comments = [
            str(c["content"])
            for c in fake.comments
            if c.get("workPackageId") == 100
        ]
        assert comments == ["ENGINE: blocked\n\nmissing credentials"]

        store = Store(store_path)
        try:
            runs = runs_for(store, 100)
            assert runs[0].state == SUCCEEDED
            assert runs[0].outcome == "blocked"
        finally:
            store.close()
    finally:
        await client.aclose()


async def test_engine_waits_for_in_flight_runs_on_stop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The runner is slower than the poll interval: the engine must wait for
    # the in-flight run (within the grace period) before returning.
    runner = FakeRunner(make_result(tmp_path), delay=0.3)
    engine, _fake, client, store_path = await make_engine(
        tmp_path,
        monkeypatch,
        work_packages=[wp(100, status=STATUS_NEW, agent=11)],
        runner=runner,
        gh=FakeGitHub(pr=PR),
    )
    try:
        exit_code = await engine.run(max_polls=1)
        assert exit_code == 0
        # The run completed (it was waited for, not cancelled).
        store = Store(store_path)
        try:
            runs = runs_for(store, 100)
            assert runs[0].state == SUCCEEDED
        finally:
            store.close()
    finally:
        await client.aclose()


# -- the M6 fix pass --------------------------------------------------------------


def fix_history() -> list[dict[str, Any]]:
    return [
        {
            "id": 201,
            "workPackageId": 100,
            "_type": "Activity",
            "details": [
                {"raw": "Status changed from In Testing to Rejected"}
            ],
            "createdAt": "2026-02-09T12:00:00Z",
            "_links": {"user": {"href": "/api/v3/users/10"}},
        },
        {
            "id": 202,
            "workPackageId": 100,
            "_type": "Activity::Comment",
            "comment": {"raw": "Please fix the failing test."},
            "details": [],
            "createdAt": "2026-02-09T12:01:00Z",
            "_links": {"user": {"href": f"/api/v3/users/{REVIEWER_USER_ID}"}},
        },
    ]


async def test_fix_claim_is_executed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = FakeRunner(make_result(tmp_path))
    engine, fake, client, store_path = await make_engine(
        tmp_path,
        monkeypatch,
        work_packages=[wp(100, status=STATUS_REJECTED, agent=11)],
        runner=runner,
        gh=FakeGitHub(pr=PR),
        activities=fix_history(),
    )
    try:
        exit_code = await engine.run(max_polls=1)
        assert exit_code == 0

        # Claimed (In Progress, assigned to the agent user) and executed.
        assert status_href(fake, 100) == f"/api/v3/statuses/{STATUS_READY_FOR_REVIEW}"
        assert assignee_href(fake, 100) == f"/api/v3/users/{AGENT_USER_ID}"
        assert len(runner.requests) == 1

        store = Store(store_path)
        try:
            runs = runs_for(store, 100)
            assert len(runs) == 1
            assert runs[0].state == SUCCEEDED
            assert runs[0].outcome == "ready_for_review"
            assert runs[0].kind == "fix"
            assert runs[0].attempt == 1
            prompt = (
                Path(runs[0].run_dir) / "prompt.txt"
            ).read_text()
            assert "FIX wp 100 attempt=1 of 3" in prompt
            assert "feedback=Please fix the failing test." in prompt
        finally:
            store.close()
    finally:
        await client.aclose()


# -- dry run -----------------------------------------------------------------------


async def test_dry_run_makes_no_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = FakeRunner(make_result(tmp_path))
    engine, fake, client, store_path = await make_engine(
        tmp_path,
        monkeypatch,
        work_packages=[wp(100, status=STATUS_NEW, agent=11)],
        runner=runner,
        gh=FakeGitHub(pr=PR),
    )
    try:
        exit_code = await engine.run(max_polls=1, dry_run=True)
        assert exit_code == 0

        # Nothing was claimed or executed.
        assert status_href(fake, 100) == f"/api/v3/statuses/{STATUS_NEW}"
        assert fake.patches == []
        assert fake.comments == []
        assert runner.requests == []
        # No durable store was created.
        assert not store_path.exists()
    finally:
        await client.aclose()


async def test_startup_fails_on_agent_token_mismatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = FakeRunner(make_result(tmp_path))
    engine, fake, client, _ = await make_engine(
        tmp_path,
        monkeypatch,
        work_packages=[wp(100, status=STATUS_NEW, agent=11)],
        runner=runner,
        gh=FakeGitHub(pr=PR),
    )

    async def fail_tokens(*args: Any, **kwargs: Any) -> Any:
        raise TokenVerificationError(
            "agent token/login mismatch: OP_TOKEN_IMPL belongs to 'reviewer', "
            "expected 'impl'"
        )

    monkeypatch.setattr("op_agent_engine.engine.enforce_agent_tokens", fail_tokens)
    try:
        assert await engine.run(max_polls=1) == 1
        # The token guard runs before any poll, claim or spawn.
        assert fake.patches == []
        assert runner.requests == []
    finally:
        await client.aclose()


# -- paused ------------------------------------------------------------------------


async def test_paused_engine_claims_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = FakeRunner(make_result(tmp_path))
    engine, fake, client, store_path = await make_engine(
        tmp_path,
        monkeypatch,
        work_packages=[wp(100, status=STATUS_NEW, agent=11)],
        runner=runner,
        settings_overrides={"engine_paused": True},
    )
    try:
        exit_code = await engine.run(max_polls=1)
        assert exit_code == 0
        assert status_href(fake, 100) == f"/api/v3/statuses/{STATUS_NEW}"
        assert fake.patches == []
        assert runner.requests == []
        store = Store(store_path)
        try:
            assert store.active_run_count() == 0
        finally:
            store.close()
    finally:
        await client.aclose()


# -- locking -----------------------------------------------------------------------


async def test_engine_refuses_to_start_when_lock_held(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = FakeRunner(make_result(tmp_path))
    engine, fake, client, _store_path = await make_engine(
        tmp_path,
        monkeypatch,
        work_packages=[wp(100, status=STATUS_NEW, agent=11)],
        runner=runner,
    )
    from op_agent_engine.store import EngineLock

    lock = EngineLock(engine.lock_path)
    assert lock.acquire()
    try:
        try:
            exit_code = await engine.run(max_polls=1)
            assert exit_code == 1
            # Nothing happened.
            assert status_href(fake, 100) == f"/api/v3/statuses/{STATUS_NEW}"
            assert fake.patches == []
            assert runner.requests == []
        finally:
            await client.aclose()
    finally:
        lock.release()


# -- crash recovery ------------------------------------------------------------------


async def test_stale_running_run_is_marked_interrupted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = FakeRunner(make_result(tmp_path))
    engine, _fake, client, store_path = await make_engine(
        tmp_path,
        monkeypatch,
        work_packages=[wp(100, status=STATUS_NEW, agent=11)],
        runner=runner,
        gh=FakeGitHub(pr=PR),
    )
    # Simulate a previous crash: a run that was claimed but never finished.
    store = Store(store_path)
    stale_run_id = store.begin_run(
        work_package_id=200,
        project_id=4,
        agent="REVIEWER",
        kind="review",
        attempt=1,
        branch="op-4-200",
        run_dir_template=str(store.runs_dir / "{id}"),
    )
    store.close()

    try:
        exit_code = await engine.run(max_polls=1)
        assert exit_code == 0

        store = Store(store_path)
        try:
            stale = store.get_run(stale_run_id)
            assert stale is not None
            assert stale.state == INTERRUPTED
            # The new run for wp 100 still succeeded.
            runs = runs_for(store, 100)
            assert runs[0].state == SUCCEEDED
        finally:
            store.close()
    finally:
        await client.aclose()


# -- review runs (M5) ---------------------------------------------------------------


async def test_engine_reviews_and_closes_a_merged_pr(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = FakeRunner(make_result(tmp_path, result_line="RESULT: APPROVED"))
    engine, fake, client, store_path = await make_engine(
        tmp_path,
        monkeypatch,
        work_packages=[wp(100, status=STATUS_READY_FOR_REVIEW, agent=22)],
        runner=runner,
        gh=FakeGitHub(pr=PR, merged=MERGED_PR),
    )
    try:
        exit_code = await engine.run(max_polls=1)
        assert exit_code == 0

        # Claimed for the reviewer (In Testing, assignee reviewer user), then
        # closed because the PR is merged.
        assert status_href(fake, 100) == f"/api/v3/statuses/{STATUS_CLOSED}"

        prompt = tmp_path / "data" / "runs" / "1" / "prompt.txt"
        assert prompt.is_file()
        assert "REVIEW wp 100" in prompt.read_text()
        assert f"pr={PR.url}" in prompt.read_text()

        store = Store(store_path)
        try:
            runs = runs_for(store, 100)
            assert len(runs) == 1
            assert runs[0].state == SUCCEEDED
            assert runs[0].kind == "review"
            assert runs[0].outcome == "approved"
        finally:
            store.close()
    finally:
        await client.aclose()


async def test_engine_runs_a_review_that_rejects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = FakeRunner(
        make_result(tmp_path, result_line="RESULT: REJECTED the tests are missing")
    )
    engine, fake, client, store_path = await make_engine(
        tmp_path,
        monkeypatch,
        work_packages=[wp(100, status=STATUS_READY_FOR_REVIEW, agent=22)],
        runner=runner,
        gh=FakeGitHub(pr=PR),
    )

    def post_reviewer_comment() -> None:
        fake.activities.append(
            {
                "id": 1,
                "workPackageId": 100,
                "_type": "Activity::Comment",
                "comment": {"raw": "Rejected: the tests are missing."},
                "details": [],
                "createdAt": datetime.now(UTC).isoformat(),
                "_links": {"user": {"href": f"/api/v3/users/{REVIEWER_USER_ID}"}},
            }
        )

    runner.on_run = post_reviewer_comment
    try:
        exit_code = await engine.run(max_polls=1)
        assert exit_code == 0

        assert status_href(fake, 100) == f"/api/v3/statuses/{STATUS_REJECTED}"

        store = Store(store_path)
        try:
            runs = runs_for(store, 100)
            assert runs[0].state == SUCCEEDED
            assert runs[0].kind == "review"
            assert runs[0].outcome == "rejected"
        finally:
            store.close()
    finally:
        await client.aclose()


async def test_engine_review_without_pr_moves_to_on_hold(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = FakeRunner(make_result(tmp_path))
    engine, fake, client, _store_path = await make_engine(
        tmp_path,
        monkeypatch,
        work_packages=[wp(100, status=STATUS_READY_FOR_REVIEW, agent=22)],
        runner=runner,
        gh=FakeGitHub(pr=None),
    )
    try:
        exit_code = await engine.run(max_polls=1)
        assert exit_code == 0

        # The reviewer was not started; the claim moved it to In Testing and
        # the engine then put it On Hold with an explanation.
        assert runner.requests == []
        assert status_href(fake, 100) == f"/api/v3/statuses/{STATUS_ON_HOLD}"
        comments = [
            str(c["content"])
            for c in fake.comments
            if c.get("workPackageId") == 100
        ]
        assert len(comments) == 1
        assert comments[0].startswith("ENGINE: no PR found")
    finally:
        await client.aclose()


# -- M7: recovery, shutdown, pause and health ---------------------------------------


async def test_startup_recovers_in_progress_with_a_resume_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = FakeRunner(make_result(tmp_path))
    engine, fake, client, store_path = await make_engine(
        tmp_path,
        monkeypatch,
        work_packages=[
            wp(100, status=STATUS_IN_PROGRESS, agent=11, assignee=AGENT_USER_ID)
        ],
        runner=runner,
        gh=FakeGitHub(pr=PR),
    )
    try:
        assert await engine.run(max_polls=1) == 0

        # The claim keeps the recovery status and the implementation is
        # verified as usual.
        assert status_href(fake, 100) == f"/api/v3/statuses/{STATUS_READY_FOR_REVIEW}"
        assert assignee_href(fake, 100) == f"/api/v3/users/{AGENT_USER_ID}"
        assert len(runner.requests) == 1

        store = Store(store_path)
        try:
            runs = runs_for(store, 100)
            assert len(runs) == 1
            assert runs[0].kind == "resume"
            assert runs[0].state == SUCCEEDED
            assert runs[0].outcome == "ready_for_review"
            prompt = (Path(runs[0].run_dir) / "prompt.txt").read_text()
            assert "RESUME wp 100" in prompt
        finally:
            store.close()
    finally:
        await client.aclose()


async def test_startup_recovers_in_testing_with_a_re_review(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = FakeRunner(make_result(tmp_path, result_line="RESULT: APPROVED"))
    engine, fake, client, store_path = await make_engine(
        tmp_path,
        monkeypatch,
        work_packages=[
            wp(100, status=STATUS_IN_TESTING, agent=22, assignee=REVIEWER_USER_ID)
        ],
        runner=runner,
        gh=FakeGitHub(pr=PR, merged=MERGED_PR),
    )
    try:
        assert await engine.run(max_polls=1) == 0

        assert status_href(fake, 100) == f"/api/v3/statuses/{STATUS_CLOSED}"
        assert assignee_href(fake, 100) == f"/api/v3/users/{REVIEWER_USER_ID}"

        store = Store(store_path)
        try:
            runs = runs_for(store, 100)
            assert len(runs) == 1
            assert runs[0].kind == "review"
            assert runs[0].state == SUCCEEDED
            assert runs[0].outcome == "approved"
            prompt = (Path(runs[0].run_dir) / "prompt.txt").read_text()
            assert "REVIEW wp 100" in prompt
        finally:
            store.close()
    finally:
        await client.aclose()


async def test_interrupted_run_does_not_count_as_a_crash_but_the_resume_failure_does(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = FakeRunner(make_result(tmp_path))  # no PR -> failure
    engine, _fake, client, store_path = await make_engine(
        tmp_path,
        monkeypatch,
        work_packages=[
            wp(100, status=STATUS_IN_PROGRESS, agent=11, assignee=AGENT_USER_ID)
        ],
        runner=runner,
        gh=FakeGitHub(pr=None),
    )
    # Simulate an engine crash: a run was claimed but never finished.
    store = Store(store_path)
    stale_run_id = store.begin_run(
        work_package_id=100,
        project_id=4,
        agent="IMPL",
        kind="implement",
        attempt=1,
        branch="op-4-100",
        run_dir_template=str(store.runs_dir / "{id}"),
    )
    store.close()

    try:
        assert await engine.run(max_polls=1) == 0

        store = Store(store_path)
        try:
            stale = store.get_run(stale_run_id)
            assert stale is not None
            assert stale.state == INTERRUPTED
            # Only the failed resume run counts; interruption did not.
            assert store.crash_count(100) == 1
            runs = [
                run
                for run in store.finished_runs()
                if run.work_package_id == 100
            ]
            assert any(run.kind == "resume" and run.state == FAILED for run in runs)
        finally:
            store.close()
    finally:
        await client.aclose()


async def test_shutdown_marks_in_flight_runs_interrupted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The run is still sleeping when the grace period (0s) expires.
    runner = FakeRunner(make_result(tmp_path), delay=30.0)
    engine, _fake, client, store_path = await make_engine(
        tmp_path,
        monkeypatch,
        work_packages=[wp(100, status=STATUS_NEW, agent=11)],
        runner=runner,
        gh=FakeGitHub(pr=PR),
        settings_overrides={"stop_grace_seconds": 0},
    )
    try:
        assert await engine.run(max_polls=1) == 0

        store = Store(store_path)
        try:
            runs = runs_for(store, 100)
            assert len(runs) == 1
            assert runs[0].state == INTERRUPTED
            # An interrupted run must not count towards the crash cap.
            assert store.crash_count(100) == 0
        finally:
            store.close()
    finally:
        await client.aclose()


async def test_pause_file_stops_new_dispatches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = FakeRunner(make_result(tmp_path))
    engine, fake, client, store_path = await make_engine(
        tmp_path,
        monkeypatch,
        work_packages=[wp(100, status=STATUS_NEW, agent=11)],
        runner=runner,
        gh=FakeGitHub(pr=PR),
    )
    data_dir = store_path.parent
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "PAUSE").write_text("paused\n", encoding="utf-8")
    try:
        assert await engine.run(max_polls=1) == 0

        assert fake.patches == []
        assert runner.requests == []
        assert status_href(fake, 100) == f"/api/v3/statuses/{STATUS_NEW}"
    finally:
        await client.aclose()


async def test_engine_writes_a_heartbeat_after_each_poll(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = FakeRunner(make_result(tmp_path))
    engine, _fake, client, store_path = await make_engine(
        tmp_path,
        monkeypatch,
        work_packages=[],
        runner=runner,
    )
    try:
        assert await engine.run(max_polls=1) == 0
        heartbeat = store_path.parent / "heartbeat"
        assert heartbeat.is_file()
        datetime.fromisoformat(heartbeat.read_text().strip())
    finally:
        await client.aclose()
