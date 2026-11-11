"""Tests for the dispatcher.

The dispatcher is exercised against the fake OpenProject (HAL-shaped
responses, lockVersion semantics) with a fake runner (no real process), a
fake GitHub and a stubbed workspace preparation (no real git).
"""

from __future__ import annotations

import json
import shutil
import subprocess
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
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
STATUS_IN_TESTING = 4
STATUS_ON_HOLD = 5
STATUS_REJECTED = 6
STATUS_CLOSED = 7

REVIEWER_USER_ID = 12

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

    def __init__(
        self,
        result: RunResult,
        *,
        on_run: Callable[[], None] | None = None,
    ) -> None:
        self.result = result
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
        if self.on_run is not None:
            self.on_run()
        return self.result


class FakeGitHub:
    def __init__(
        self,
        pr: PullRequest | None = None,
        error: GitHubError | None = None,
        merged: PullRequest | None = None,
    ) -> None:
        self.pr = pr
        self.merged = merged
        self.error = error
        self.calls: list[tuple[str, str]] = []
        self.states: list[str] = []

    async def find_pr(
        self, repo: str, branch: str, *, state: str = "open"
    ) -> PullRequest | None:
        self.calls.append((repo, branch))
        self.states.append(state)
        if self.error is not None:
            raise self.error
        if state == "merged":
            return self.merged
        if state == "open":
            return self.pr
        return None


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
    settings_overrides: dict[str, Any] | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
    prompts_template: str = (
        "PROMPT subject={{ wp.subject }} branch={{ branch }} "
        "repo={{ repo.github }} agent={{ agent.name }} "
        "project={{ project.identifier }} default={{ repo.default_branch }} "
        "url={{ wp.url }} type={{ wp.type }} desc={{ wp.description }}"
    ),
    review_template: str = (
        "REVIEWPROMPT subject={{ wp.subject }} branch={{ branch }} "
        "pr={{ pr.url }} review={{ review_number }} agent={{ agent.name }} "
        "repo={{ repo.github }} desc={{ wp.description }}"
    ),
    fix_template: str = (
        "FIXPROMPT subject={{ wp.subject }} attempt={{ attempt }} "
        "of {{ max_rejections }} branch={{ branch }} repo={{ repo.github }} "
        "agent={{ agent.name }} feedback={{ feedback.author }}|"
        "{{ feedback.created_at }}|{{ feedback.text }} "
        "human={% for c in later_human_comments %}"
        "[{{ c.author }}|{{ c.created_at }}|{{ c.text }}]{% endfor %}"
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
        **(settings_overrides or {}),
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
    (prompts_dir / "review.txt.j2").write_text(review_template, encoding="utf-8")
    (prompts_dir / "fix.txt.j2").write_text(fix_template, encoding="utf-8")
    (prompts_dir / "resume.txt.j2").write_text(prompts_template, encoding="utf-8")

    dispatcher = Dispatcher(
        config=config,
        resolution=resolution,
        store=store,
        client=client,
        settings=settings,
        gh=gh if gh is not None else (
            FakeGitHub() if settings.git_host == "github.com" else None
        ),
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


def make_review_item(work_package_id: int) -> DispatchItem:
    entry = wp(work_package_id, status=STATUS_IN_TESTING, agent=22)
    work_package = parse_work_package(entry, "customField3", OPTION_ID_TO_VALUE)
    return DispatchItem(
        work_package=work_package,
        agent_key="REVIEWER",
        status_key="ready_for_review",
    )


def make_fix_item(work_package_id: int) -> DispatchItem:
    entry = wp(work_package_id, status=STATUS_IN_PROGRESS, agent=11)
    work_package = parse_work_package(entry, "customField3", OPTION_ID_TO_VALUE)
    return DispatchItem(
        work_package=work_package, agent_key="IMPL", status_key="rejected"
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


async def test_reviewer_workspace_uses_its_dedicated_github_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = FakeRunner(make_result(tmp_path))
    dispatcher, _fake, store, client, settings = await make_dispatcher(
        tmp_path, monkeypatch, runner=runner, gh=FakeGitHub(pr=PR)
    )
    settings.gh_token = SecretStr("shared-token")
    dispatcher._config.agents["REVIEWER"].env["GH_TOKEN"] = "reviewer-token"

    captured: dict[str, Any] = {}

    async def capture_workspace(**kwargs: Any) -> Workspace:
        captured.update(kwargs)
        return Workspace(
            repo_dir=tmp_path / "repo",
            branch="op-4-100",
            standard_path=tmp_path / "AGENTS.md",
        )

    monkeypatch.setattr(
        "op_agent_engine.dispatcher.prepare_workspace", capture_workspace
    )
    try:
        await dispatcher._prepare_workspace(claim(store, make_review_item(101)), 1)
        assert captured["gh_token"] == "reviewer-token"
        assert captured["role"] == "reviewer"

        captured.clear()
        await dispatcher._prepare_workspace(claim(store, make_item(102)), 2)
        assert captured["gh_token"] == "shared-token"
        assert captured["role"] == "implementer"
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
    runner = FakeRunner(make_result(tmp_path, stdout="thinking about it\n"))
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


# -- fix runs (M6) and dry run ----------------------------------------------------


async def test_fix_kind_is_executed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = FakeRunner(make_result(tmp_path))
    fake = make_fake(
        work_packages=[wp(100, status=STATUS_IN_PROGRESS, agent=11)],
        priorities=PRIORITIES,
        activities=[
            activity_comment(100, "Please fix the failing test.", author_id=REVIEWER_USER_ID),
            status_change(100, "Rejected"),
        ],
    )
    dispatcher, _fake, store, client, _ = await make_dispatcher(
        tmp_path, monkeypatch, runner=runner, gh=FakeGitHub(pr=PR), fake=fake
    )
    try:
        claimed = claim(store, make_fix_item(100), kind="fix")
        await dispatcher.execute(claimed)

        run = store.get_run(1)
        assert run is not None
        assert run.state == SUCCEEDED
        assert run.outcome == "ready_for_review"
        assert run.kind == "fix"
        # The fix pass number equals the rejection count in the history.
        assert run.attempt == 1

        prompt = (store.runs_dir / "1" / "prompt.txt").read_text()
        assert "FIXPROMPT" in prompt
        assert "attempt=1 of 3" in prompt
        assert "Please fix the failing test." in prompt
        assert "(reviewer)" in prompt
        assert status_href(fake, 100) == f"/api/v3/statuses/{STATUS_READY_FOR_REVIEW}"
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


# -- review runs (M5) ---------------------------------------------------------------

MERGED_PR = PullRequest(
    number=7,
    url="https://github.com/owner/demo/pull/7",
    head_branch="op-4-100",
    state="merged",
)


def activity_comment(
    work_package_id: int,
    text: str,
    *,
    author_id: int = REVIEWER_USER_ID,
    created_at: datetime | None = None,
    activity_id: int | None = None,
    author_title: str | None = None,
) -> dict[str, Any]:
    """A comment activity shaped like the live API."""
    user: dict[str, Any] = {"href": f"/api/v3/users/{author_id}"}
    if author_title is not None:
        user["title"] = author_title
    return {
        "id": activity_id if activity_id is not None else 900 + work_package_id,
        "workPackageId": work_package_id,
        "_type": "Activity::Comment",
        "comment": {"raw": text},
        "details": [],
        "createdAt": (created_at or datetime.now(UTC)).isoformat(),
        "_links": {"user": user},
    }


def status_change(
    work_package_id: int,
    target: str,
    *,
    previous: str = "In Testing",
    activity_id: int | None = None,
    created_at: datetime | None = None,
) -> dict[str, Any]:
    """A status-change activity shaped like the live API."""
    return {
        "id": activity_id if activity_id is not None else 800 + work_package_id,
        "workPackageId": work_package_id,
        "_type": "Activity",
        "details": [
            {"format": "custom", "raw": f"Status changed from {previous} to {target}"}
        ],
        "createdAt": (created_at or datetime.now(UTC)).isoformat(),
        "_links": {"user": {"href": "/api/v3/users/10"}},
    }


def reviewer_comment(
    work_package_id: int,
    text: str = "Rejected: the tests do not cover the new branch.",
    *,
    author_id: int = REVIEWER_USER_ID,
    created_at: datetime | None = None,
) -> dict[str, Any]:
    """An activity entry shaped like the live API."""
    return activity_comment(
        work_package_id,
        text,
        author_id=author_id,
        created_at=created_at,
    )


async def make_review_dispatcher(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    runner: FakeRunner,
    gh: FakeGitHub,
    fake: FakeOpenProject | None = None,
    max_crash_retries: int = 2,
) -> tuple[Dispatcher, FakeOpenProject, Store, OpenProjectClient, EngineSettings]:
    return await make_dispatcher(
        tmp_path,
        monkeypatch,
        runner=runner,
        gh=gh,
        fake=fake
        or make_fake(
            work_packages=[wp(100, status=STATUS_IN_TESTING, agent=22)],
            priorities=PRIORITIES,
        ),
        max_crash_retries=max_crash_retries,
    )


async def test_review_merged_pr_closes_the_work_package(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = FakeRunner(make_result(tmp_path, result_line="RESULT: APPROVED"))
    gh = FakeGitHub(pr=PR, merged=MERGED_PR)
    dispatcher, fake, store, client, _ = await make_review_dispatcher(
        tmp_path, monkeypatch, runner=runner, gh=gh
    )
    try:
        claimed = claim(store, make_review_item(100), kind="review")
        await dispatcher.execute(claimed)

        run = store.get_run(1)
        assert run is not None
        assert run.state == SUCCEEDED
        assert run.outcome == "approved"
        assert run.kind == "review"

        # Closed, status only, no engine comment (the review was clean).
        assert status_href(fake, 100) == f"/api/v3/statuses/{STATUS_CLOSED}"
        assert fake.patches[0]["body"]["_links"]["status"]["href"] == (
            f"/api/v3/statuses/{STATUS_CLOSED}"
        )
        assert "assignee" not in fake.patches[0]["body"]["_links"]
        assert posted_comments(fake, 100) == []

        # Both lookups used the project repo and the run branch; the merged
        # check asked for merged PRs specifically.
        assert gh.calls == [("owner/demo", "op-4-100"), ("owner/demo", "op-4-100")]
        assert gh.states == ["open", "merged"]

        # The review prompt was rendered with the PR and the attempt number.
        prompt = (store.runs_dir / "1" / "prompt.txt").read_text()
        assert "REVIEWPROMPT" in prompt
        assert f"pr={PR.url}" in prompt
        assert "review=1" in prompt
        assert "branch=op-4-100" in prompt
        assert "agent=reviewer" in prompt

        meta = json.loads((store.runs_dir / "1" / "meta.json").read_text())
        assert meta["kind"] == "review"
        assert meta["state"] == "succeeded"
        assert meta["outcome"] == "approved"
        assert meta["transition"] == "closed"
        assert meta["pr_url"] == MERGED_PR.url
    finally:
        await client.aclose()
        store.close()


async def test_review_rejection_with_comment_moves_to_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = make_fake(
        work_packages=[wp(100, status=STATUS_IN_TESTING, agent=22)],
        priorities=PRIORITIES,
    )

    def post_reviewer_comment() -> None:
        fake.activities.append(reviewer_comment(100))

    runner = FakeRunner(
        make_result(
            tmp_path,
            result_line="RESULT: REJECTED the tests do not cover the new branch",
        ),
        on_run=post_reviewer_comment,
    )
    gh = FakeGitHub(pr=PR, merged=None)
    dispatcher, fake, store, client, _ = await make_review_dispatcher(
        tmp_path, monkeypatch, runner=runner, gh=gh, fake=fake
    )
    try:
        claimed = claim(store, make_review_item(100), kind="review")
        await dispatcher.execute(claimed)

        run = store.get_run(1)
        assert run is not None
        assert run.state == SUCCEEDED
        assert run.outcome == "rejected"

        assert status_href(fake, 100) == f"/api/v3/statuses/{STATUS_REJECTED}"
        # The reviewer wrote the rejection comment; the engine adds none.
        assert posted_comments(fake, 100) == []
        assert store.crash_count(100) == 0
    finally:
        await client.aclose()
        store.close()


async def test_review_rejection_without_comment_is_a_crash_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = FakeRunner(make_result(tmp_path, result_line="RESULT: REJECTED nope"))
    gh = FakeGitHub(pr=PR, merged=None)
    dispatcher, fake, store, client, _ = await make_review_dispatcher(
        tmp_path, monkeypatch, runner=runner, gh=gh
    )
    try:
        claimed = claim(store, make_review_item(100), kind="review")
        await dispatcher.execute(claimed)

        run = store.get_run(1)
        assert run is not None
        assert run.state == FAILED
        assert run.outcome == "rejected_without_comment"
        assert store.crash_count(100) == 1

        # Below the cap: the work package stays In Testing, nothing is patched.
        assert status_href(fake, 100) == f"/api/v3/statuses/{STATUS_IN_TESTING}"
        assert fake.patches == []
        assert posted_comments(fake, 100) == []
    finally:
        await client.aclose()
        store.close()


async def test_review_comment_by_someone_else_does_not_count(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = make_fake(
        work_packages=[wp(100, status=STATUS_IN_TESTING, agent=22)],
        priorities=PRIORITIES,
    )

    def post_human_comment() -> None:
        # A human (the implementer's user) commented, not the reviewer.
        fake.activities.append(reviewer_comment(100, author_id=11))

    runner = FakeRunner(
        make_result(tmp_path, result_line="RESULT: REJECTED looks wrong"),
        on_run=post_human_comment,
    )
    dispatcher, fake, store, client, _ = await make_review_dispatcher(
        tmp_path, monkeypatch, runner=runner, gh=FakeGitHub(pr=PR), fake=fake
    )
    try:
        claimed = claim(store, make_review_item(100), kind="review")
        await dispatcher.execute(claimed)

        run = store.get_run(1)
        assert run is not None
        assert run.outcome == "rejected_without_comment"
        assert status_href(fake, 100) == f"/api/v3/statuses/{STATUS_IN_TESTING}"
    finally:
        await client.aclose()
        store.close()


async def test_review_comment_from_before_the_run_does_not_count(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stale = datetime(2020, 1, 1, tzinfo=UTC)
    fake = make_fake(
        work_packages=[wp(100, status=STATUS_IN_TESTING, agent=22)],
        priorities=PRIORITIES,
        activities=[reviewer_comment(100, "old rejection", created_at=stale)],
    )
    runner = FakeRunner(make_result(tmp_path, result_line="RESULT: REJECTED nope"))
    dispatcher, fake, store, client, _ = await make_review_dispatcher(
        tmp_path, monkeypatch, runner=runner, gh=FakeGitHub(pr=PR), fake=fake
    )
    try:
        claimed = claim(store, make_review_item(100), kind="review")
        await dispatcher.execute(claimed)

        run = store.get_run(1)
        assert run is not None
        assert run.outcome == "rejected_without_comment"
    finally:
        await client.aclose()
        store.close()


async def test_review_blocked_moves_to_on_hold(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = FakeRunner(
        make_result(tmp_path, result_line="RESULT: BLOCKED no access to the CI logs")
    )
    dispatcher, fake, store, client, _ = await make_review_dispatcher(
        tmp_path, monkeypatch, runner=runner, gh=FakeGitHub(pr=PR)
    )
    try:
        claimed = claim(store, make_review_item(100), kind="review")
        await dispatcher.execute(claimed)

        run = store.get_run(1)
        assert run is not None
        assert run.state == SUCCEEDED
        assert run.outcome == "blocked"
        assert status_href(fake, 100) == f"/api/v3/statuses/{STATUS_ON_HOLD}"
        assert posted_comments(fake, 100) == [
            "ENGINE: blocked\n\nno access to the CI logs"
        ]
    finally:
        await client.aclose()
        store.close()


async def test_ready_for_review_without_pr_moves_to_on_hold(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = FakeRunner(make_result(tmp_path))
    dispatcher, fake, store, client, _ = await make_review_dispatcher(
        tmp_path, monkeypatch, runner=runner, gh=FakeGitHub(pr=None)
    )
    try:
        claimed = claim(store, make_review_item(100), kind="review")
        await dispatcher.execute(claimed)

        # The reviewer was never started.
        assert runner.requests == []

        run = store.get_run(1)
        assert run is not None
        assert run.state == FAILED
        assert run.outcome == "no_pr_for_branch"

        assert status_href(fake, 100) == f"/api/v3/statuses/{STATUS_ON_HOLD}"
        comments = posted_comments(fake, 100)
        assert len(comments) == 1
        assert comments[0].startswith("ENGINE: no PR found")
        assert "op-4-100" in comments[0]

        meta = json.loads((store.runs_dir / "1" / "meta.json").read_text())
        assert meta["outcome"] == "no_pr_for_branch"
        assert meta["transition"] == "on_hold"
        assert meta["argv"] is None
    finally:
        await client.aclose()
        store.close()


async def test_review_closes_an_already_merged_pr_without_running_the_agent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """M7 recovery: merge landed just before the engine crashed."""
    runner = FakeRunner(make_result(tmp_path))
    dispatcher, fake, store, client, _ = await make_review_dispatcher(
        tmp_path,
        monkeypatch,
        runner=runner,
        gh=FakeGitHub(pr=None, merged=MERGED_PR),
    )
    try:
        claimed = claim(store, make_review_item(100), kind="review")
        await dispatcher.execute(claimed)

        # The reviewer was never started; the merged PR is authoritative.
        assert runner.requests == []
        assert status_href(fake, 100) == f"/api/v3/statuses/{STATUS_CLOSED}"

        run = store.get_run(1)
        assert run is not None
        assert run.state == SUCCEEDED
        assert run.outcome == "approved"
        assert run.result_line is None

        meta = json.loads((store.runs_dir / "1" / "meta.json").read_text())
        assert meta["transition"] == "closed"
        assert meta["pr_url"] == MERGED_PR.url
        assert meta["argv"] is None
    finally:
        await client.aclose()
        store.close()


async def test_merged_pr_wins_over_a_rejection_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """External verification is authoritative."""
    runner = FakeRunner(make_result(tmp_path, result_line="RESULT: REJECTED nope"))
    dispatcher, fake, store, client, _ = await make_review_dispatcher(
        tmp_path, monkeypatch, runner=runner, gh=FakeGitHub(pr=PR, merged=MERGED_PR)
    )
    try:
        claimed = claim(store, make_review_item(100), kind="review")
        await dispatcher.execute(claimed)

        run = store.get_run(1)
        assert run is not None
        assert run.outcome == "approved"
        assert status_href(fake, 100) == f"/api/v3/statuses/{STATUS_CLOSED}"
    finally:
        await client.aclose()
        store.close()


async def test_review_nonzero_exit_is_a_crash_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = FakeRunner(make_result(tmp_path, exit_code=2))
    dispatcher, fake, store, client, _ = await make_review_dispatcher(
        tmp_path, monkeypatch, runner=runner, gh=FakeGitHub(pr=PR)
    )
    try:
        claimed = claim(store, make_review_item(100), kind="review")
        await dispatcher.execute(claimed)

        run = store.get_run(1)
        assert run is not None
        assert run.state == FAILED
        assert run.outcome == "nonzero_exit"
        assert store.crash_count(100) == 1
        # The work package stays In Testing for the next review run.
        assert status_href(fake, 100) == f"/api/v3/statuses/{STATUS_IN_TESTING}"
        assert fake.patches == []
    finally:
        await client.aclose()
        store.close()


async def test_review_timeout_is_a_crash_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = FakeRunner(make_result(tmp_path, timed_out=True))
    dispatcher, fake, store, client, _ = await make_review_dispatcher(
        tmp_path, monkeypatch, runner=runner, gh=FakeGitHub(pr=PR)
    )
    try:
        claimed = claim(store, make_review_item(100), kind="review")
        await dispatcher.execute(claimed)

        run = store.get_run(1)
        assert run is not None
        assert run.state == TIMEOUT
        assert run.outcome == "timeout"
        assert store.crash_count(100) == 1
        assert status_href(fake, 100) == f"/api/v3/statuses/{STATUS_IN_TESTING}"
    finally:
        await client.aclose()
        store.close()


async def test_review_crash_cap_escalates_from_in_testing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = FakeRunner(make_result(tmp_path, stdout="review log line\n"))
    dispatcher, fake, store, client, _ = await make_review_dispatcher(
        tmp_path, monkeypatch, runner=runner, gh=FakeGitHub(pr=PR)
    )
    try:
        store.increment_crash_count(100)
        store.increment_crash_count(100)
        claimed = claim(store, make_review_item(100), kind="review")
        await dispatcher.execute(claimed)

        run = store.get_run(1)
        assert run is not None
        assert run.state == FAILED
        assert run.outcome == "not_merged"
        assert store.crash_count(100) == 3

        assert status_href(fake, 100) == f"/api/v3/statuses/{STATUS_ON_HOLD}"
        comments = posted_comments(fake, 100)
        assert len(comments) == 1
        assert comments[0].startswith("ENGINE: escalated")
        assert "not_merged" in comments[0]
        assert "review log line" in comments[0]
    finally:
        await client.aclose()
        store.close()


async def test_review_merged_check_error_fails_the_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = FakeRunner(make_result(tmp_path))
    dispatcher, fake, store, client, _ = await make_review_dispatcher(
        tmp_path, monkeypatch, runner=runner, gh=FakeGitHub(error=GitHubError("boom"))
    )
    try:
        claimed = claim(store, make_review_item(100), kind="review")
        await dispatcher.execute(claimed)

        # The open-PR lookup fails before the reviewer starts.
        assert runner.requests == []
        run = store.get_run(1)
        assert run is not None
        assert run.state == FAILED
        assert run.outcome == "pr_verification_error"
        assert store.crash_count(100) == 1
        assert status_href(fake, 100) == f"/api/v3/statuses/{STATUS_IN_TESTING}"
    finally:
        await client.aclose()
        store.close()


async def test_review_workspace_failure_stays_in_testing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = FakeRunner(make_result(tmp_path))
    dispatcher, fake, store, client, _ = await make_review_dispatcher(
        tmp_path, monkeypatch, runner=runner, gh=FakeGitHub(pr=PR)
    )

    async def failing_workspace(**kwargs: Any) -> Workspace:
        raise WorkspaceError("no remote branch origin/op-4-100 to review")

    monkeypatch.setattr(
        "op_agent_engine.dispatcher.prepare_workspace", failing_workspace
    )
    try:
        claimed = claim(store, make_review_item(100), kind="review")
        await dispatcher.execute(claimed)

        run = store.get_run(1)
        assert run is not None
        assert run.state == FAILED
        assert run.outcome == "workspace_error"
        assert store.crash_count(100) == 1
        assert runner.requests == []
        assert status_href(fake, 100) == f"/api/v3/statuses/{STATUS_IN_TESTING}"
    finally:
        await client.aclose()
        store.close()


async def test_review_dry_run_claim_is_not_executed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = FakeRunner(make_result(tmp_path))
    gh = FakeGitHub(pr=PR, merged=MERGED_PR)
    dispatcher, fake, store, client, _ = await make_review_dispatcher(
        tmp_path, monkeypatch, runner=runner, gh=gh
    )
    try:
        claimed = ClaimedRun(
            run_id=None,
            item=make_review_item(100),
            kind="review",
            attempt=1,
            branch="op-4-100",
            run_dir=None,
        )
        await dispatcher.execute(claimed)

        assert runner.requests == []
        assert gh.calls == []
        assert fake.patches == []
        assert fake.comments == []
        assert store.active_run_count() == 0
    finally:
        await client.aclose()
        store.close()


# -- rejection loop (M6) -------------------------------------------------------------


async def test_fix_pass_uses_latest_reviewer_and_later_human_comments(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Comment selection: latest reviewer feedback, later
    human comments, no other agent or engine comments, at most 5 human ones."""
    start = datetime(2026, 2, 10, 12, 0, tzinfo=UTC)
    human_comments = [
        activity_comment(
            100,
            f"human note {index}",
            author_id=99,
            created_at=start + timedelta(minutes=10 + index),
            activity_id=120 + index,
            author_title="Jane Human",
        )
        for index in range(7)
    ]
    activities = [
        status_change(
            100, "Rejected", activity_id=101, created_at=start - timedelta(days=2)
        ),
        activity_comment(
            100,
            "old human note before the feedback",
            author_id=99,
            created_at=start - timedelta(minutes=5),
            activity_id=102,
        ),
        activity_comment(
            100,
            "First review: fix A",
            created_at=start,
            activity_id=103,
        ),
        activity_comment(
            100,
            "agent chatter",
            author_id=11,
            created_at=start + timedelta(minutes=1),
            activity_id=104,
        ),
        activity_comment(
            100,
            "ENGINE: escalated",
            author_id=10,
            created_at=start + timedelta(minutes=2),
            activity_id=105,
        ),
        status_change(
            100, "Rejected", activity_id=106, created_at=start + timedelta(minutes=3)
        ),
        activity_comment(
            100,
            "Latest review: fix B",
            created_at=start + timedelta(minutes=5),
            activity_id=107,
        ),
        *human_comments,
    ]
    fake = make_fake(
        work_packages=[wp(100, status=STATUS_IN_PROGRESS, agent=11)],
        priorities=PRIORITIES,
        activities=activities,
    )
    runner = FakeRunner(make_result(tmp_path))
    dispatcher, _fake, store, client, _ = await make_dispatcher(
        tmp_path, monkeypatch, runner=runner, gh=FakeGitHub(pr=PR), fake=fake
    )
    try:
        claimed = claim(store, make_fix_item(100), kind="fix")
        await dispatcher.execute(claimed)

        run = store.get_run(1)
        assert run is not None
        assert run.state == SUCCEEDED
        assert run.attempt == 2

        prompt = (store.runs_dir / "1" / "prompt.txt").read_text()
        assert "attempt=2 of 3" in prompt
        assert "Latest review: fix B" in prompt
        assert "Jane Human (human)" in prompt
        # Only the newest five human comments after the feedback are included.
        assert "human note 0" not in prompt
        assert "human note 1" not in prompt
        for index in range(2, 7):
            assert f"human note {index}" in prompt
        # Comments before the feedback, from agents, or from the engine are
        # excluded.
        assert "old human note before the feedback" not in prompt
        assert "First review: fix A" not in prompt
        assert "agent chatter" not in prompt
        assert "ENGINE: escalated" not in prompt
    finally:
        await client.aclose()
        store.close()


async def test_fix_pass_without_reviewer_feedback_fails_before_the_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = make_fake(
        work_packages=[wp(100, status=STATUS_IN_PROGRESS, agent=11)],
        priorities=PRIORITIES,
        activities=[status_change(100, "Rejected")],
    )
    runner = FakeRunner(make_result(tmp_path))
    dispatcher, _fake, store, client, _ = await make_dispatcher(
        tmp_path, monkeypatch, runner=runner, gh=FakeGitHub(pr=PR), fake=fake
    )
    try:
        claimed = claim(store, make_fix_item(100), kind="fix")
        await dispatcher.execute(claimed)

        # The agent is never started; the failure counts as a crash retry.
        assert runner.requests == []
        run = store.get_run(1)
        assert run is not None
        assert run.state == FAILED
        assert run.outcome == "fix_feedback_missing"
        assert store.crash_count(100) == 1
        assert status_href(fake, 100) == f"/api/v3/statuses/{STATUS_IN_PROGRESS}"
    finally:
        await client.aclose()
        store.close()


async def test_fix_pass_without_a_pr_counts_as_crash_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = make_fake(
        work_packages=[wp(100, status=STATUS_IN_PROGRESS, agent=11)],
        priorities=PRIORITIES,
        activities=[activity_comment(100, "Please fix the failing test.")],
    )
    runner = FakeRunner(make_result(tmp_path, stdout="thinking about it\n"))
    dispatcher, _fake, store, client, _ = await make_dispatcher(
        tmp_path, monkeypatch, runner=runner, gh=FakeGitHub(pr=None), fake=fake
    )
    try:
        claimed = claim(store, make_fix_item(100), kind="fix")
        await dispatcher.execute(claimed)

        run = store.get_run(1)
        assert run is not None
        assert run.state == FAILED
        assert run.outcome == "no_pr"
        assert store.crash_count(100) == 1
        assert status_href(fake, 100) == f"/api/v3/statuses/{STATUS_IN_PROGRESS}"
    finally:
        await client.aclose()
        store.close()


def rejection_history(count: int) -> list[dict[str, Any]]:
    start = datetime(2026, 2, 1, 12, 0, tzinfo=UTC)
    return [
        status_change(
            100,
            "Rejected",
            activity_id=200 + index,
            created_at=start + timedelta(days=index),
        )
        for index in range(count)
    ]


async def test_third_rejection_is_allowed_with_default_cap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = make_fake(
        work_packages=[wp(100, status=STATUS_IN_TESTING, agent=22)],
        priorities=PRIORITIES,
        activities=rejection_history(2),
    )

    def post_reviewer_comment() -> None:
        fake.activities.append(reviewer_comment(100, "Still failing: fix C"))

    runner = FakeRunner(
        make_result(tmp_path, result_line="RESULT: REJECTED fix C"),
        on_run=post_reviewer_comment,
    )
    dispatcher, fake, store, client, _ = await make_review_dispatcher(
        tmp_path, monkeypatch, runner=runner, gh=FakeGitHub(pr=PR), fake=fake
    )
    try:
        claimed = claim(store, make_review_item(100), kind="review")
        await dispatcher.execute(claimed)

        # Rejection 3 of 3: still applied.
        run = store.get_run(1)
        assert run is not None
        assert run.state == SUCCEEDED
        assert run.outcome == "rejected"
        assert status_href(fake, 100) == f"/api/v3/statuses/{STATUS_REJECTED}"
        # The reviewer wrote the comment; the engine adds none.
        assert posted_comments(fake, 100) == []
    finally:
        await client.aclose()
        store.close()


async def test_fourth_rejection_escalates_to_on_hold(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = make_fake(
        work_packages=[wp(100, status=STATUS_IN_TESTING, agent=22)],
        priorities=PRIORITIES,
        activities=rejection_history(3),
    )

    def post_reviewer_comment() -> None:
        fake.activities.append(reviewer_comment(100, "Still failing: fix D"))

    runner = FakeRunner(
        make_result(tmp_path, result_line="RESULT: REJECTED fix D"),
        on_run=post_reviewer_comment,
    )
    dispatcher, fake, store, client, _ = await make_review_dispatcher(
        tmp_path, monkeypatch, runner=runner, gh=FakeGitHub(pr=PR), fake=fake
    )
    try:
        claimed = claim(store, make_review_item(100), kind="review")
        await dispatcher.execute(claimed)

        # Rejection 4 exceeds MAX_REJECTIONS=3: On Hold, not Rejected.
        run = store.get_run(1)
        assert run is not None
        assert run.state == SUCCEEDED
        assert run.outcome == "rejection_cap_reached"
        assert status_href(fake, 100) == f"/api/v3/statuses/{STATUS_ON_HOLD}"
        comments = posted_comments(fake, 100)
        assert len(comments) == 1
        assert comments[0].startswith("ENGINE: rejection cap reached")
        assert "rejection 4" in comments[0]
        assert "MAX_REJECTIONS=3" in comments[0]

        meta = json.loads((store.runs_dir / "1" / "meta.json").read_text())
        assert meta["outcome"] == "rejection_cap_reached"
        assert meta["transition"] == "on_hold"
    finally:
        await client.aclose()
        store.close()


async def test_engine_comments_use_the_activities_endpoint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verified live shape: POST /activities with ``{"comment": {"raw": ...}}``."""
    runner = FakeRunner(
        make_result(tmp_path, result_line="RESULT: BLOCKED missing credentials")
    )
    dispatcher, fake, store, client, _ = await make_dispatcher(
        tmp_path, monkeypatch, runner=runner, gh=FakeGitHub(pr=PR)
    )
    try:
        claimed = claim(store, make_item(100))
        await dispatcher.execute(claimed)

        comment_requests = [
            request
            for request in fake.requests
            if request.method == "POST"
            and request.url.path == "/api/v3/work_packages/100/activities"
        ]
        assert len(comment_requests) == 1
        body = json.loads(comment_requests[0].content.decode())
        assert body == {"comment": {"raw": "ENGINE: blocked\n\nmissing credentials"}}
        # The fake mirrors posted comments into the activity history under the
        # posting (engine) user.
        posted = [
            activity
            for activity in fake.activities
            if activity.get("comment", {}).get("raw", "").startswith("ENGINE:")
        ]
        assert posted[-1]["_links"]["user"]["href"] == "/api/v3/users/10"
    finally:
        await client.aclose()
        store.close()


async def test_timeout_after_the_pr_was_opened_moves_to_ready_for_review(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A hung run whose PR is open has delivered the work; it is not retried."""
    runner = FakeRunner(make_result(tmp_path, exit_code=-15, timed_out=True))
    dispatcher, fake, store, client, _ = await make_dispatcher(
        tmp_path, monkeypatch, runner=runner, gh=FakeGitHub(pr=PR)
    )
    try:
        claimed = claim(store, make_item(100))
        await dispatcher.execute(claimed)

        run = store.get_run(1)
        assert run is not None
        assert run.state == SUCCEEDED
        assert run.outcome == "ready_for_review"
        assert store.crash_count(100) == 0
        assert status_href(fake, 100) == f"/api/v3/statuses/{STATUS_READY_FOR_REVIEW}"
    finally:
        await client.aclose()
        store.close()


# -- failed-run worktree post-mortem ------------------------------------------
#
# These tests use a real clone plus a linked worktree (real git) so the
# capture and the keep/remove decision are exercised end to end.


def _git(args: list[str], cwd: Path | None = None) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=str(cwd) if cwd is not None else None,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout


def make_real_workspace(tmp_path: Path) -> Workspace:
    """A real clone plus a linked worktree at the paths make_dispatcher uses."""
    agent_dir = tmp_path / "ws" / "impl"
    clone = agent_dir / "repositories" / "demo"
    shutil.rmtree(clone, ignore_errors=True)
    clone.mkdir(parents=True)
    _git(["init", "--quiet", str(clone)])
    _git(["-C", str(clone), "config", "user.name", "t"])
    _git(["-C", str(clone), "config", "user.email", "t@example.com"])
    (clone / "README.md").write_text("hello\n", encoding="utf-8")
    _git(["-C", str(clone), "add", "README.md"])
    _git(["-C", str(clone), "commit", "--no-gpg-sign", "--quiet", "-m", "initial"])
    worktree = agent_dir / "worktrees" / "op-4-100"
    worktree.parent.mkdir(parents=True, exist_ok=True)
    _git(
        [
            "-C",
            str(clone),
            "worktree",
            "add",
            "--quiet",
            "-B",
            "op-4-100",
            str(worktree),
        ]
    )
    return Workspace(
        repo_dir=worktree,
        branch="op-4-100",
        standard_path=agent_dir / "AGENTS.md",
        default_branch="main",
        base_repo_dir=clone,
    )


def use_workspace(monkeypatch: pytest.MonkeyPatch, workspace: Workspace) -> None:
    async def fake_prepare_workspace(**kwargs: Any) -> Workspace:
        return workspace

    monkeypatch.setattr(
        "op_agent_engine.dispatcher.prepare_workspace", fake_prepare_workspace
    )


async def test_failed_run_captures_worktree_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    holder: dict[str, Workspace] = {}

    def edit() -> None:
        workspace = holder["ws"]
        (workspace.repo_dir / "README.md").write_text(
            "hello\nedited\n", encoding="utf-8"
        )
        (workspace.repo_dir / "notes.md").write_text("agent notes\n", encoding="utf-8")

    runner = FakeRunner(
        make_result(tmp_path, stdout="I tried things\n"), on_run=edit
    )
    dispatcher, _fake, store, client, _ = await make_dispatcher(
        tmp_path, monkeypatch, runner=runner, gh=FakeGitHub(pr=None)
    )
    try:
        workspace = make_real_workspace(tmp_path)
        holder["ws"] = workspace
        use_workspace(monkeypatch, workspace)

        await dispatcher.execute(claim(store, make_item(100)))

        run = store.get_run(1)
        assert run is not None
        assert run.state == FAILED
        assert run.outcome == "no_pr"

        # The artefact is written before anything is removed.
        artefact = store.runs_dir / "1" / "worktree"
        status = (artefact / "status.txt").read_text()
        assert "README.md" in status
        assert "notes.md" in status
        assert "edited" in (artefact / "diff.patch").read_text()
        assert (artefact / "untracked" / "notes.md").read_text() == "agent notes\n"
        assert (artefact / "MANIFEST.txt").is_file()

        meta = json.loads((store.runs_dir / "1" / "meta.json").read_text())
        assert meta["worktree_artefact"] == str(artefact)
        assert meta["worktree_kept"] is True
        assert meta["worktree"]["artefact"] == str(artefact)
        assert meta["worktree"]["kept"] is True
        assert meta["worktree"]["copied_files"] == 1
        assert meta["worktree"]["errors"] == []

        # The worktree itself is kept for post-mortem.
        assert workspace.repo_dir.is_dir()
        assert (workspace.repo_dir / "notes.md").is_file()
    finally:
        await client.aclose()
        store.close()


async def test_successful_run_removes_the_worktree_and_captures_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    holder: dict[str, Workspace] = {}

    def edit() -> None:
        workspace = holder["ws"]
        (workspace.repo_dir / "notes.md").write_text("agent notes\n", encoding="utf-8")

    runner = FakeRunner(make_result(tmp_path), on_run=edit)
    dispatcher, _fake, store, client, _ = await make_dispatcher(
        tmp_path, monkeypatch, runner=runner, gh=FakeGitHub(pr=PR)
    )
    try:
        workspace = make_real_workspace(tmp_path)
        holder["ws"] = workspace
        use_workspace(monkeypatch, workspace)

        await dispatcher.execute(claim(store, make_item(100)))

        run = store.get_run(1)
        assert run is not None
        assert run.state == SUCCEEDED
        # A successful run still cleans up: no worktree, no artefact.
        assert not workspace.repo_dir.exists()
        assert not (store.runs_dir / "1" / "worktree").exists()
        meta = json.loads((store.runs_dir / "1" / "meta.json").read_text())
        assert meta["worktree_artefact"] is None
        assert meta["worktree_kept"] is False
    finally:
        await client.aclose()
        store.close()


async def test_timeout_run_captures_worktree_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    holder: dict[str, Workspace] = {}

    def edit() -> None:
        (holder["ws"].repo_dir / "notes.md").write_text(
            "agent notes\n", encoding="utf-8"
        )

    runner = FakeRunner(
        make_result(tmp_path, exit_code=-15, timed_out=True), on_run=edit
    )
    dispatcher, _fake, store, client, _ = await make_dispatcher(
        tmp_path, monkeypatch, runner=runner, gh=FakeGitHub(pr=None)
    )
    try:
        workspace = make_real_workspace(tmp_path)
        holder["ws"] = workspace
        use_workspace(monkeypatch, workspace)

        await dispatcher.execute(claim(store, make_item(100)))

        run = store.get_run(1)
        assert run is not None
        assert run.state == TIMEOUT
        assert (store.runs_dir / "1" / "worktree" / "untracked" / "notes.md").is_file()
        assert workspace.repo_dir.is_dir()
    finally:
        await client.aclose()
        store.close()


async def test_escalation_comment_names_the_worktree_artefact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    holder: dict[str, Workspace] = {}

    def edit() -> None:
        (holder["ws"].repo_dir / "notes.md").write_text(
            "agent notes\n", encoding="utf-8"
        )

    runner = FakeRunner(make_result(tmp_path), on_run=edit)
    dispatcher, fake, store, client, _ = await make_dispatcher(
        tmp_path, monkeypatch, runner=runner, gh=FakeGitHub(pr=None),
        max_crash_retries=0,
    )
    try:
        workspace = make_real_workspace(tmp_path)
        holder["ws"] = workspace
        use_workspace(monkeypatch, workspace)

        await dispatcher.execute(claim(store, make_item(100)))

        assert status_href(fake, 100) == f"/api/v3/statuses/{STATUS_ON_HOLD}"
        comments = posted_comments(fake, 100)
        assert len(comments) == 1
        assert comments[0].startswith("ENGINE: escalated")
        assert str(store.runs_dir / "1" / "worktree") in comments[0]
    finally:
        await client.aclose()
        store.close()


async def test_pr_verification_error_captures_worktree_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    holder: dict[str, Workspace] = {}

    def edit() -> None:
        (holder["ws"].repo_dir / "notes.md").write_text(
            "agent notes\n", encoding="utf-8"
        )

    runner = FakeRunner(make_result(tmp_path), on_run=edit)
    gh = FakeGitHub(error=GitHubError("gh timed out"))
    dispatcher, _fake, store, client, _ = await make_dispatcher(
        tmp_path, monkeypatch, runner=runner, gh=gh
    )
    try:
        workspace = make_real_workspace(tmp_path)
        holder["ws"] = workspace
        use_workspace(monkeypatch, workspace)

        await dispatcher.execute(claim(store, make_item(100)))

        run = store.get_run(1)
        assert run is not None
        assert run.outcome == "pr_verification_error"
        meta = json.loads((store.runs_dir / "1" / "meta.json").read_text())
        assert meta["worktree_artefact"] == str(store.runs_dir / "1" / "worktree")
        assert (store.runs_dir / "1" / "worktree" / "status.txt").is_file()
    finally:
        await client.aclose()
        store.close()


async def test_keep_failed_worktrees_false_removes_the_worktree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    holder: dict[str, Workspace] = {}

    def edit() -> None:
        (holder["ws"].repo_dir / "notes.md").write_text(
            "agent notes\n", encoding="utf-8"
        )

    runner = FakeRunner(make_result(tmp_path), on_run=edit)
    dispatcher, _fake, store, client, settings = await make_dispatcher(
        tmp_path, monkeypatch, runner=runner, gh=FakeGitHub(pr=None)
    )
    settings.keep_failed_worktrees = False
    try:
        workspace = make_real_workspace(tmp_path)
        holder["ws"] = workspace
        use_workspace(monkeypatch, workspace)

        await dispatcher.execute(claim(store, make_item(100)))

        # The artefact is still captured; the worktree is removed.
        assert (store.runs_dir / "1" / "worktree" / "status.txt").is_file()
        assert not workspace.repo_dir.exists()
        meta = json.loads((store.runs_dir / "1" / "meta.json").read_text())
        assert meta["worktree_kept"] is False
        assert meta["worktree_artefact"] is not None
    finally:
        await client.aclose()
        store.close()


async def test_failed_worktree_size_cap_removes_the_worktree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    holder: dict[str, Workspace] = {}

    def edit() -> None:
        (holder["ws"].repo_dir / "notes.md").write_text(
            "agent notes\n", encoding="utf-8"
        )

    runner = FakeRunner(make_result(tmp_path), on_run=edit)
    dispatcher, _fake, store, client, settings = await make_dispatcher(
        tmp_path, monkeypatch, runner=runner, gh=FakeGitHub(pr=None)
    )
    settings.failed_worktree_max_mb = 0
    try:
        workspace = make_real_workspace(tmp_path)
        holder["ws"] = workspace
        use_workspace(monkeypatch, workspace)

        await dispatcher.execute(claim(store, make_item(100)))

        assert not workspace.repo_dir.exists()
        assert (store.runs_dir / "1" / "worktree" / "status.txt").is_file()
        meta = json.loads((store.runs_dir / "1" / "meta.json").read_text())
        assert meta["worktree_kept"] is False
    finally:
        await client.aclose()
        store.close()


# -- resume with an open PR (WP 144) -------------------------------------------


def seed_run(
    store: Store, *, kind: str, outcome: str, agent: str = "IMPL"
) -> int:
    """Insert a finished run for work package 100 (project 4)."""
    run_id = store.begin_run(
        work_package_id=100,
        project_id=4,
        agent=agent,
        kind=kind,
        attempt=1,
        branch="op-4-100",
        run_dir_template=str(store.runs_dir / "{id}"),
    )
    store.finish_run(run_id, state=SUCCEEDED, outcome=outcome)
    return run_id


async def test_resume_with_open_pr_runs_the_agent_when_a_rejection_is_outstanding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A killed fix run leaves an open PR that predates the rejection."""
    runner = FakeRunner(make_result(tmp_path))
    dispatcher, fake, store, client, _ = await make_dispatcher(
        tmp_path, monkeypatch, runner=runner, gh=FakeGitHub(pr=PR)
    )
    try:
        # Code first, rejection after: the rejection is unanswered.
        seed_run(store, kind="implement", outcome="ready_for_review")
        seed_run(store, kind="review", outcome="rejected", agent="REVIEWER")

        claimed = claim(store, make_item(100), kind="resume")
        await dispatcher.execute(claimed)

        run = store.get_run(claimed.run_id)
        assert run is not None
        assert run.state == SUCCEEDED
        assert run.outcome == "ready_for_review"
        # The agent ran instead of the PR short-circuiting the resume.
        assert len(runner.requests) == 1
        assert posted_comments(fake, 100) == []
    finally:
        await client.aclose()
        store.close()


async def test_resume_with_open_pr_is_skipped_when_the_rejection_was_addressed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = FakeRunner(make_result(tmp_path))
    dispatcher, fake, store, client, _ = await make_dispatcher(
        tmp_path, monkeypatch, runner=runner, gh=FakeGitHub(pr=PR)
    )
    try:
        # Rejection first, then a fix run that answered it.
        seed_run(store, kind="review", outcome="rejected", agent="REVIEWER")
        seed_run(store, kind="fix", outcome="ready_for_review")

        claimed = claim(store, make_item(100), kind="resume")
        await dispatcher.execute(claimed)

        run = store.get_run(claimed.run_id)
        assert run is not None
        assert run.state == SUCCEEDED
        assert run.outcome == "ready_for_review_existing_pr"
        assert runner.requests == []
        assert any(
            "resume skipped" in comment for comment in posted_comments(fake, 100)
        )
        assert status_href(fake, 100) == f"/api/v3/statuses/{STATUS_READY_FOR_REVIEW}"
    finally:
        await client.aclose()
        store.close()


async def test_resume_with_open_pr_and_no_review_is_skipped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = FakeRunner(make_result(tmp_path))
    dispatcher, _fake, store, client, _ = await make_dispatcher(
        tmp_path, monkeypatch, runner=runner, gh=FakeGitHub(pr=PR)
    )
    try:
        seed_run(store, kind="implement", outcome="ready_for_review")

        claimed = claim(store, make_item(100), kind="resume")
        await dispatcher.execute(claimed)

        run = store.get_run(claimed.run_id)
        assert run is not None
        assert run.outcome == "ready_for_review_existing_pr"
        assert runner.requests == []
    finally:
        await client.aclose()
        store.close()


# -- crash counter reset on un-hold (WP 367) -----------------------------------


async def test_unheld_ticket_resets_the_crash_counter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A human moving the ticket off On Hold restores the retry budget."""
    runner = FakeRunner(make_result(tmp_path, stdout="still working\n"))
    fake = make_fake(
        work_packages=[wp(100, status=STATUS_IN_PROGRESS, agent=11)],
        priorities=PRIORITIES,
        activities=[
            status_change(100, "On Hold", previous="In Progress", activity_id=900),
            status_change(100, "New", previous="On Hold", activity_id=901),
        ],
    )
    dispatcher, fake, store, client, _ = await make_dispatcher(
        tmp_path, monkeypatch, runner=runner, gh=FakeGitHub(pr=None), fake=fake
    )
    try:
        for _ in range(3):
            store.increment_crash_count(100)

        claimed = claim(store, make_item(100))
        await dispatcher.execute(claimed)

        run = store.get_run(claimed.run_id)
        assert run is not None
        assert run.outcome == "no_pr"
        # 3 -> 0 by the reset, then 1 for this failure: still inside the cap.
        assert store.crash_count(100) == 1
        assert status_href(fake, 100) == f"/api/v3/statuses/{STATUS_IN_PROGRESS}"
        assert posted_comments(fake, 100) == []
    finally:
        await client.aclose()
        store.close()


async def test_a_ticket_still_on_hold_keeps_its_crash_counter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The engine's own escalation must not hand the budget back."""
    runner = FakeRunner(make_result(tmp_path, stdout="still working\n"))
    fake = make_fake(
        work_packages=[wp(100, status=STATUS_IN_PROGRESS, agent=11)],
        priorities=PRIORITIES,
        activities=[
            status_change(100, "On Hold", previous="In Progress", activity_id=900),
        ],
    )
    dispatcher, fake, store, client, _ = await make_dispatcher(
        tmp_path, monkeypatch, runner=runner, gh=FakeGitHub(pr=None), fake=fake
    )
    try:
        for _ in range(3):
            store.increment_crash_count(100)

        claimed = claim(store, make_item(100))
        await dispatcher.execute(claimed)

        assert store.crash_count(100) == 4
        assert status_href(fake, 100) == f"/api/v3/statuses/{STATUS_ON_HOLD}"
        assert posted_comments(fake, 100)[0].startswith("ENGINE: escalated")
    finally:
        await client.aclose()
        store.close()


async def test_silent_run_is_reported_as_no_result_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A run that exits 0 and writes nothing is not an ordinary "no PR"."""
    runner = FakeRunner(make_result(tmp_path))  # exit 0, empty logs, no PR
    dispatcher, fake, store, client, _ = await make_dispatcher(
        tmp_path, monkeypatch, runner=runner, gh=FakeGitHub(pr=None)
    )
    try:
        store.increment_crash_count(100)
        store.increment_crash_count(100)
        claimed = claim(store, make_item(100))
        await dispatcher.execute(claimed)

        run = store.get_run(claimed.run_id)
        assert run is not None
        assert run.state == FAILED
        assert run.outcome == "no_result_line"
        assert store.crash_count(100) == 3
        assert status_href(fake, 100) == f"/api/v3/statuses/{STATUS_ON_HOLD}"
        comment = posted_comments(fake, 100)[0]
        assert comment.startswith("ENGINE: escalated")
        assert "(no_result_line)" in comment
        assert "wrote nothing at all to stdout or stderr" in comment
        assert "(run log is empty)" in comment
    finally:
        await client.aclose()
        store.close()
