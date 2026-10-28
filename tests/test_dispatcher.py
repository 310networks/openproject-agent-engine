"""Tests for the dispatcher (PLAN section 4, milestone M4).

The dispatcher is exercised against the fake OpenProject (HAL-shaped
responses, lockVersion semantics) with a fake runner (no real process), a
fake GitHub and a stubbed workspace preparation (no real git).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from pydantic import SecretStr

from op_agent_engine.config import EngineSettings
from op_agent_engine.dispatcher import Dispatcher
from op_agent_engine.github import GitHubError, PullRequest
from op_agent_engine.openproject.client import OpenProjectClient, OpenProjectError
from op_agent_engine.openproject.workpackage import parse_work_package
from op_agent_engine.poller import DispatchItem
from op_agent_engine.prompts import PromptRenderer
from op_agent_engine.runners.base import OnSpawned, RunRequest, RunResult
from op_agent_engine.scheduler import ClaimedRun
from op_agent_engine.store import FAILED, RUNNING, SUCCEEDED, TIMEOUT, Store
from op_agent_engine.workspace import Workspace, WorkspaceError
from tests.fakes.openproject import FakeOpenProject
from tests.test_poller import PRIORITIES, wp
from tests.test_resolve import BASE_URL, TOKEN, make_config, make_fake, resolve

STATUS_IN_PROGRESS = 2
STATUS_READY_FOR_REVIEW = 3
STATUS_ON_HOLD = 5
STATUS_CLOSED = 7

OPTION_ID_TO_VALUE = {11: "IMPL", 22: "REVIEWER"}

PR = PullRequest(
    number=7,
    url="https://github.com/owner/demo/pull/7",
    head_branch="op-4-100",
    state="open",
)


# -- fakes -------------------------------------------------------------------


class FakeRunner:
    """Stands in for the runner abstraction: no process is spawned."""

    def __init__(self, result: RunResult) -> None:
        self.result = result
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
            env={"PATH": "/usr/bin", "GH_TOKEN": "super-secret"},
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
        if on_spawned is not None:
            on_spawned(4321, 4321, "2026-02-10T00:00:00+00:00")
        return self.result


class FakeGitHub:
    def __init__(
        self,
        pr: PullRequest | None = None,
        error: GitHubError | None = None,
    ) -> None:
        self.pr = pr
        self.error = error
        self.calls: list[tuple[str, str]] = []

    async def find_pr(
        self, repo: str, branch: str, *, state: str = "open"
    ) -> PullRequest | None:
        self.calls.append((repo, branch))
        if self.error is not None:
            raise self.error
        return self.pr


def make_result(
    tmp_path: Path,
    *,
    exit_code: int = 0,
    timed_out: bool = False,
    result_line: str | None = None,
    stdout: str = "",
    stderr: str = "",
) -> RunResult:
    stdout_path = tmp_path / "stdout.log"
    stderr_path = tmp_path / "stderr.log"
    stdout_path.write_text(stdout, encoding="utf-8")
    stderr_path.write_text(stderr, encoding="utf-8")
    return RunResult(
        exit_code=exit_code,
        timed_out=timed_out,
        duration_seconds=1.5,
        stdout_path=stdout_path,
        stderr_path=stderr_path,
        result_line=result_line,
    )


# -- fixtures ------------------------------------------------------------------


async def make_dispatcher(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    runner: FakeRunner,
    gh: FakeGitHub | None = None,
    fake: FakeOpenProject | None = None,
    work_packages: list[dict[str, Any]] | None = None,
    max_crash_retries: int = 2,
    config_overrides: dict[str, Any] | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
    prompts_template: str = (
        "PROMPT subject={{ wp.subject }} branch={{ branch }} "
        "repo={{ repo.github }} agent={{ agent.name }} "
        "project={{ project.identifier }} default={{ repo.default_branch }} "
        "url={{ wp.url }} type={{ wp.type }} desc={{ wp.description }}"
    ),
) -> tuple[Dispatcher, FakeOpenProject, Store, OpenProjectClient, EngineSettings]:
    if fake is None:
        fake = make_fake(
            work_packages=work_packages
            or [wp(100, status=STATUS_IN_PROGRESS, agent=11)],
            priorities=PRIORITIES,
        )
    config = make_config(**(config_overrides or {}))
    resolution = await resolve(fake, config)
    client = OpenProjectClient(
        BASE_URL,
        TOKEN,
        transport=transport if transport is not None else fake.transport,
        max_attempts=1,
    )
    store = Store(tmp_path / "engine.db")
    settings = EngineSettings(
        op_base_url=BASE_URL,
        op_engine_token=SecretStr(TOKEN),
        data_dir=tmp_path / "data",
        workspace_root=tmp_path / "ws",
        max_crash_retries=max_crash_retries,
        run_timeout_seconds=360,
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
    (prompts_dir / "implement.txt.j2").write_text(prompts_template, encoding="utf-8")

    dispatcher = Dispatcher(
        config=config,
        resolution=resolution,
        store=store,
        client=client,
        settings=settings,
        gh=gh if gh is not None else FakeGitHub(),
        runner=runner,
        prompts=PromptRenderer(prompts_dir),
    )
    return dispatcher, fake, store, client, settings


def make_item(work_package_id: int, *, status: int = STATUS_IN_PROGRESS) -> DispatchItem:
    entry = wp(work_package_id, status=status, agent=11)
    work_package = parse_work_package(entry, "customField3", OPTION_ID_TO_VALUE)
    return DispatchItem(
        work_package=work_package, agent_key="IMPL", status_key="new"
    )


def claim(store: Store, item: DispatchItem, *, kind: str = "implement") -> ClaimedRun:
    branch = f"op-{item.work_package.project_id}-{item.work_package.id}"
    run_id = store.begin_run(
        work_package_id=item.work_package.id,
        project_id=item.work_package.project_id,
        agent=item.agent_key,
        kind=kind,
        attempt=1,
        branch=branch,
        run_dir_template=str(store.runs_dir / "{id}"),
    )
    return ClaimedRun(
        run_id=run_id,
        item=item,
        kind=kind,
        attempt=1,
        branch=branch,
        run_dir=store.runs_dir / str(run_id),
    )


def status_href(fake: FakeOpenProject, work_package_id: int) -> str:
    entry = next(w for w in fake.work_packages if w.get("id") == work_package_id)
    return entry["_links"]["status"]["href"]


def posted_comments(fake: FakeOpenProject, work_package_id: int) -> list[str]:
    return [
        str(c["content"])
        for c in fake.comments
        if c.get("workPackageId") == work_package_id
    ]


# -- success path ---------------------------------------------------------------


async def test_implement_success_with_pr(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = FakeRunner(make_result(tmp_path))
    gh = FakeGitHub(pr=PR)
    dispatcher, fake, store, client, _ = await make_dispatcher(
        tmp_path, monkeypatch, runner=runner, gh=gh
    )
    try:
        claimed = claim(store, make_item(100))
        await dispatcher.execute(claimed)

        # Store: succeeded, process was recorded.
        run = store.get_run(1)
        assert run is not None
        assert run.state == SUCCEEDED
        assert run.outcome == "ready_for_review"
        assert run.exit_code == 0
        assert run.pid == 4321
        assert run.pgid == 4321
        assert run.proc_start == "2026-02-10T00:00:00+00:00"

        # OpenProject: moved to Ready for Review (status only, no assignee).
        assert status_href(fake, 100) == f"/api/v3/statuses/{STATUS_READY_FOR_REVIEW}"
        assert len(fake.patches) == 1
        body = fake.patches[0]["body"]
        assert body["_links"]["status"]["href"] == (
            f"/api/v3/statuses/{STATUS_READY_FOR_REVIEW}"
        )
        assert "assignee" not in body["_links"]
        # No engine comment on success.
        assert posted_comments(fake, 100) == []

        # GitHub was checked with the project repo and the run branch.
        assert gh.calls == [("owner/demo", "op-4-100")]

        # Per-run artifacts.
        run_dir = store.runs_dir / "1"
        prompt = (run_dir / "prompt.txt").read_text()
        assert "wp 100" in prompt
        assert "branch=op-4-100" in prompt
        assert "repo=owner/demo" in prompt
        assert "agent=impl" in prompt
        assert "project=demo" in prompt
        meta = json.loads((run_dir / "meta.json").read_text())
        assert meta["run_id"] == 1
        assert meta["work_package_id"] == 100
        assert meta["agent"] == "IMPL"
        assert meta["kind"] == "implement"
        assert meta["branch"] == "op-4-100"
        assert meta["state"] == "succeeded"
        assert meta["outcome"] == "ready_for_review"
        assert meta["transition"] == "ready_for_review"
        assert meta["pr_url"] == PR.url
        assert meta["exit_code"] == 0
        assert meta["argv"] == ["fake-runner"]
        # Secret env values are redacted in meta.json.
        assert meta["env"]["GH_TOKEN"] == "***"
        assert meta["env"]["PATH"] == "/usr/bin"
    finally:
        await client.aclose()
        store.close()


async def test_runner_receives_workspace_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = FakeRunner(make_result(tmp_path))
    dispatcher, _fake, store, client, _ = await make_dispatcher(
        tmp_path, monkeypatch, runner=runner, gh=FakeGitHub(pr=PR)
    )
    try:
        claimed = claim(store, make_item(100))
        await dispatcher.execute(claimed)

        request = runner.requests[0]
        assert request.cwd == tmp_path / "ws" / "impl" / "repositories" / "demo"
        assert request.run_id == 1
        assert request.work_package_id == 100
        assert request.agent == "IMPL"
        assert request.timeout_seconds == 360
        prompt_file = run_dir_prompt_path(store, 1)
        assert (prompt_file).is_file()
    finally:
        await client.aclose()
        store.close()


def run_dir_prompt_path(store: Store, run_id: int) -> Path:
    return store.runs_dir / str(run_id) / "prompt.txt"


async def test_standard_is_injected_when_configured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = FakeRunner(make_result(tmp_path))
    dispatcher, _fake, store, client, _ = await make_dispatcher(
        tmp_path,
        monkeypatch,
        runner=runner,
        gh=FakeGitHub(pr=PR),
        config_overrides={
            "runners": {
                "pi": {
                    "argv": ["pi", "-p", "@{prompt_file}"],
                    "inject_standard": True,
                }
            }
        },
    )
    try:
        claimed = claim(store, make_item(100))
        await dispatcher.execute(claimed)

        prompt = (store.runs_dir / "1" / "prompt.txt").read_text()
        assert prompt.startswith("# Engineering standard")
        assert "STANDARD CONTENT" in prompt
        assert "PROMPT subject=wp 100" in prompt
    finally:
        await client.aclose()
        store.close()


# -- blocked -------------------------------------------------------------------


async def test_implement_blocked_moves_to_on_hold(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = FakeRunner(
        make_result(tmp_path, result_line="RESULT: BLOCKED missing credentials")
    )
    # Even with a PR present, the blocked result wins.
    dispatcher, fake, store, client, _ = await make_dispatcher(
        tmp_path, monkeypatch, runner=runner, gh=FakeGitHub(pr=PR)
    )
    try:
        claimed = claim(store, make_item(100))
        await dispatcher.execute(claimed)

        run = store.get_run(1)
        assert run is not None
        assert run.state == SUCCEEDED
        assert run.outcome == "blocked"
        assert status_href(fake, 100) == f"/api/v3/statuses/{STATUS_ON_HOLD}"
        comments = posted_comments(fake, 100)
        assert len(comments) == 1
        assert comments[0] == "ENGINE: blocked\n\nmissing credentials"
    finally:
        await client.aclose()
        store.close()


# -- crash retries ---------------------------------------------------------------


async def test_no_pr_counts_as_crash_retry_and_stays_in_progress(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = FakeRunner(make_result(tmp_path))
    dispatcher, fake, store, client, _ = await make_dispatcher(
        tmp_path, monkeypatch, runner=runner, gh=FakeGitHub(pr=None)
    )
    try:
        claimed = claim(store, make_item(100))
        await dispatcher.execute(claimed)

        run = store.get_run(1)
        assert run is not None
        assert run.state == FAILED
        assert run.outcome == "no_pr"
        assert store.crash_count(100) == 1
        # No transition, no comment: the work package stays In Progress.
        assert status_href(fake, 100) == f"/api/v3/statuses/{STATUS_IN_PROGRESS}"
        assert fake.patches == []
        assert posted_comments(fake, 100) == []
    finally:
        await client.aclose()
        store.close()


async def test_nonzero_exit_counts_as_crash_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = FakeRunner(make_result(tmp_path, exit_code=3))
    dispatcher, fake, store, client, _ = await make_dispatcher(
        tmp_path, monkeypatch, runner=runner, gh=FakeGitHub(pr=None)
    )
    try:
        claimed = claim(store, make_item(100))
        await dispatcher.execute(claimed)

        run = store.get_run(1)
        assert run is not None
        assert run.state == FAILED
        assert run.outcome == "nonzero_exit"
        assert run.exit_code == 3
        assert store.crash_count(100) == 1
        assert status_href(fake, 100) == f"/api/v3/statuses/{STATUS_IN_PROGRESS}"
    finally:
        await client.aclose()
        store.close()


async def test_timeout_counts_as_crash_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = FakeRunner(make_result(tmp_path, timed_out=True))
    dispatcher, fake, store, client, _ = await make_dispatcher(
        tmp_path, monkeypatch, runner=runner, gh=FakeGitHub(pr=None)
    )
    try:
        claimed = claim(store, make_item(100))
        await dispatcher.execute(claimed)

        run = store.get_run(1)
        assert run is not None
        assert run.state == TIMEOUT
        assert run.outcome == "timeout"
        assert store.crash_count(100) == 1
        assert status_href(fake, 100) == f"/api/v3/statuses/{STATUS_IN_PROGRESS}"
    finally:
        await client.aclose()
        store.close()


async def test_crash_cap_escalates_to_on_hold(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Two crashes already recorded; this third failing run exceeds the cap.
    runner = FakeRunner(
        make_result(tmp_path, exit_code=1, stdout="line1\nline2\nline3\n")
    )
    dispatcher, fake, store, client, _ = await make_dispatcher(
        tmp_path, monkeypatch, runner=runner, gh=FakeGitHub(pr=None)
    )
    try:
        store.increment_crash_count(100)
        store.increment_crash_count(100)
        claimed = claim(store, make_item(100))
        await dispatcher.execute(claimed)

        run = store.get_run(1)
        assert run is not None
        assert run.state == FAILED
        assert store.crash_count(100) == 3
        assert status_href(fake, 100) == f"/api/v3/statuses/{STATUS_ON_HOLD}"
        comments = posted_comments(fake, 100)
        assert len(comments) == 1
        comment = comments[0]
        assert comment.startswith("ENGINE: escalated")
        assert "nonzero_exit" in comment
        assert "Exit code: 1" in comment
        assert "line3" in comment
    finally:
        await client.aclose()
        store.close()


# -- not executed yet / dry run ---------------------------------------------------


async def test_fix_kind_is_claimed_but_not_executed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = FakeRunner(make_result(tmp_path))
    dispatcher, _fake, store, client, _ = await make_dispatcher(
        tmp_path, monkeypatch, runner=runner
    )
    try:
        claimed = claim(store, make_item(100), kind="fix")
        await dispatcher.execute(claimed)

        # The run stays active; the runner was never called.
        run = store.get_run(1)
        assert run is not None
        assert run.state == RUNNING
        assert runner.requests == []
    finally:
        await client.aclose()
        store.close()


async def test_dry_run_claimed_is_not_executed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = FakeRunner(make_result(tmp_path))
    dispatcher, fake, store, client, _ = await make_dispatcher(
        tmp_path, monkeypatch, runner=runner
    )
    try:
        claimed = ClaimedRun(
            run_id=None,
            item=make_item(100),
            kind="implement",
            attempt=1,
            branch="op-4-100",
            run_dir=None,
        )
        await dispatcher.execute(claimed)

        assert runner.requests == []
        assert fake.patches == []
        assert fake.comments == []
        assert store.active_run_count() == 0
    finally:
        await client.aclose()
        store.close()


# -- workspace failures -------------------------------------------------------------


async def test_workspace_failure_fails_run_without_transition(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = FakeRunner(make_result(tmp_path))
    dispatcher, fake, store, client, _ = await make_dispatcher(
        tmp_path, monkeypatch, runner=runner
    )
    async def failing_workspace(**kwargs: Any) -> Workspace:
        raise WorkspaceError("clone failed: no such repository")

    monkeypatch.setattr(
        "op_agent_engine.dispatcher.prepare_workspace", failing_workspace
    )
    try:
        claimed = claim(store, make_item(100))
        await dispatcher.execute(claimed)

        run = store.get_run(1)
        assert run is not None
        assert run.state == FAILED
        assert run.outcome == "workspace_error"
        assert store.crash_count(100) == 1
        # Below the cap: no transition, no comment.
        assert status_href(fake, 100) == f"/api/v3/statuses/{STATUS_IN_PROGRESS}"
        assert fake.patches == []
        assert posted_comments(fake, 100) == []
        # The runner was never called.
        assert runner.requests == []
        # meta.json records the failure.
        meta = json.loads((store.runs_dir / "1" / "meta.json").read_text())
        assert meta["state"] == "failed"
        assert meta["outcome"] == "workspace_error"
        assert meta["argv"] is None
    finally:
        await client.aclose()
        store.close()


async def test_workspace_failure_escalates_at_cap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = FakeRunner(make_result(tmp_path))
    dispatcher, fake, store, client, _ = await make_dispatcher(
        tmp_path, monkeypatch, runner=runner
    )
    async def failing_workspace(**kwargs: Any) -> Workspace:
        raise WorkspaceError("clone failed: no such repository")

    monkeypatch.setattr(
        "op_agent_engine.dispatcher.prepare_workspace", failing_workspace
    )
    try:
        store.increment_crash_count(100)
        store.increment_crash_count(100)
        claimed = claim(store, make_item(100))
        await dispatcher.execute(claimed)

        assert store.crash_count(100) == 3
        assert status_href(fake, 100) == f"/api/v3/statuses/{STATUS_ON_HOLD}"
        comments = posted_comments(fake, 100)
        assert len(comments) == 1
        assert comments[0].startswith("ENGINE: escalated")
        assert "clone failed: no such repository" in comments[0]
    finally:
        await client.aclose()
        store.close()


# -- PR verification failure ---------------------------------------------------------


async def test_pr_verification_error_fails_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = FakeRunner(make_result(tmp_path, stdout="did some work\n"))
    gh = FakeGitHub(error=GitHubError("gh timed out"))
    dispatcher, _fake, store, client, _ = await make_dispatcher(
        tmp_path, monkeypatch, runner=runner, gh=gh
    )
    try:
        claimed = claim(store, make_item(100))
        await dispatcher.execute(claimed)

        run = store.get_run(1)
        assert run is not None
        assert run.state == FAILED
        assert run.outcome == "pr_verification_error"
        assert store.crash_count(100) == 1
        # The run log tail is included in the escalation only at the cap;
        # below the cap nothing is posted.
        meta = json.loads((store.runs_dir / "1" / "meta.json").read_text())
        assert meta["state"] == "failed"
        assert meta["outcome"] == "pr_verification_error"
    finally:
        await client.aclose()
        store.close()


# -- transition safety -----------------------------------------------------------------


async def test_transition_aborted_when_status_changed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = FakeRunner(make_result(tmp_path))
    dispatcher, fake, store, client, _ = await make_dispatcher(
        tmp_path, monkeypatch, runner=runner, gh=FakeGitHub(pr=PR)
    )
    try:
        claimed = claim(store, make_item(100))
        # A human closed the work package while the run was in flight.
        fake.work_packages[0]["_links"]["status"] = {"href": f"/api/v3/statuses/{STATUS_CLOSED}"}
        fake.work_packages[0]["lockVersion"] = 1

        await dispatcher.execute(claimed)

        run = store.get_run(1)
        assert run is not None
        assert run.state == FAILED
        assert (run.outcome or "").startswith("transition_aborted")
        assert "status changed" in (run.outcome or "")
        # No patch was applied; the work package stays Closed.
        assert fake.patches == []
        assert status_href(fake, 100) == f"/api/v3/statuses/{STATUS_CLOSED}"
    finally:
        await client.aclose()
        store.close()


async def test_transition_retries_once_on_conflict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Bump the lockVersion right after the dispatcher's first re-read so its
    # first PATCH conflicts; the retry must pick up the fresh version.
    fake = make_fake(
        work_packages=[wp(100, status=STATUS_IN_PROGRESS, agent=11)],
        priorities=PRIORITIES,
    )
    inner = fake.handle
    state = {"wp_gets": 0}

    def handle(request: httpx.Request) -> httpx.Response:
        response = inner(request)
        if (
            request.method == "GET"
            and request.url.path == "/api/v3/work_packages/100"
        ):
            state["wp_gets"] += 1
            if state["wp_gets"] == 1:
                fake.work_packages[0]["lockVersion"] += 1
        return response

    runner = FakeRunner(make_result(tmp_path))
    dispatcher, fake, store, client, _ = await make_dispatcher(
        tmp_path,
        monkeypatch,
        runner=runner,
        gh=FakeGitHub(pr=PR),
        fake=fake,
        transport=httpx.MockTransport(handle),
    )
    try:
        claimed = claim(store, make_item(100))
        await dispatcher.execute(claimed)

        run = store.get_run(1)
        assert run is not None
        assert run.state == SUCCEEDED
        assert run.outcome == "ready_for_review"
        # Two re-reads (first, then after the conflict), two PATCH requests,
        # one successful patch.
        assert state["wp_gets"] == 2
        assert len(fake.patches) == 1
        patch_requests = [
            request for request in fake.requests if request.method == "PATCH"
        ]
        assert len(patch_requests) == 2
        assert status_href(fake, 100) == f"/api/v3/statuses/{STATUS_READY_FOR_REVIEW}"
    finally:
        await client.aclose()
        store.close()


async def test_openproject_error_after_run_leaves_run_active(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = FakeRunner(make_result(tmp_path))
    dispatcher, fake, store, client, _ = await make_dispatcher(
        tmp_path,
        monkeypatch,
        runner=runner,
        gh=FakeGitHub(pr=PR),
    )
    try:
        claimed = claim(store, make_item(100))
        # The transition PATCH fails with a transient error.
        fake.patch_error = (503, "Service Unavailable")

        with pytest.raises(OpenProjectError):
            await dispatcher.execute(claimed)

        # The run stays active so the next engine start can recover it.
        run = store.get_run(1)
        assert run is not None
        assert run.state == RUNNING
        meta = json.loads((store.runs_dir / "1" / "meta.json").read_text())
        assert meta["state"] == "running"
        assert meta["outcome"] == "error"
    finally:
        await client.aclose()
        store.close()
