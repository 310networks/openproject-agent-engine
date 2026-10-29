"""End-to-end fake-agent cycle (PLAN sections 19 and M9 acceptance).

One work package goes ``New`` -> ``Ready for Review`` -> ``Closed`` in two
polls, using the real ``GenericRunner`` to spawn ``tests/fakes/fake_agent.sh``.
OpenProject and GitHub are fakes; workspace preparation is stubbed (no git).

This is the automated counterpart of the manual acceptance checklist in
docs/ACCEPTANCE.md:
it proves that a configured runner, prompt rendering, result parsing, outcome
verification and every transition work together, and that no secret configured
for a run leaks into the run directory.
"""

from __future__ import annotations

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
from op_agent_engine.runners import GenericRunner
from op_agent_engine.store import SUCCEEDED, Store
from op_agent_engine.workspace import Workspace
from tests.fakes.openproject import FakeOpenProject
from tests.test_poller import PRIORITIES, wp
from tests.test_resolve import BASE_URL, TOKEN, make_config, make_fake

STATUS_NEW = 1
STATUS_READY_FOR_REVIEW = 3
STATUS_CLOSED = 7

WORK_PACKAGE_ID = 100
BRANCH = f"op-4-{WORK_PACKAGE_ID}"
FAKE_AGENT = Path(__file__).parent / "fakes" / "fake_agent.sh"

PR = PullRequest(
    number=7,
    url="https://github.com/owner/demo/pull/7",
    head_branch=BRANCH,
    state="open",
)
MERGED_PR = PullRequest(
    number=7,
    url="https://github.com/owner/demo/pull/7",
    head_branch=BRANCH,
    state="merged",
)

SECRET = "ghp_e2e_should_never_appear"


class FakeGitHub:
    def __init__(self) -> None:
        self.states: list[str] = []

    async def find_pr(
        self, repo: str, branch: str, *, state: str = "open"
    ) -> PullRequest | None:
        self.states.append(state)
        if state == "merged":
            return MERGED_PR
        return PR


def status_href(fake: FakeOpenProject, work_package_id: int) -> str:
    entry = next(w for w in fake.work_packages if w.get("id") == work_package_id)
    return entry["_links"]["status"]["href"]


def e2e_config() -> Any:
    return make_config(
        runners={
            "pi": {
                "argv": ["/bin/sh", str(FAKE_AGENT), "{prompt_file}"],
                "cwd": "{repo_dir}",
                # GH_TOKEN is a secret; it must be redacted from meta.json.
                "env": {"GH_TOKEN": SECRET, "FAKE_AGENT_SLEEP_SECONDS": "0"},
                "inject_standard": False,
            }
        }
    )


async def make_engine(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Engine, FakeOpenProject, OpenProjectClient, Path]:
    fake = make_fake(
        work_packages=[wp(WORK_PACKAGE_ID, status=STATUS_NEW, agent=11)],
        priorities=PRIORITIES,
    )
    config = e2e_config()
    settings = EngineSettings(
        op_base_url=BASE_URL,
        op_engine_token=SecretStr(TOKEN),
        data_dir=tmp_path / "data",
        workspace_root=tmp_path / "ws",
        poll_interval_seconds=1,
        max_concurrent_runs=1,
        run_timeout_seconds=120,
        stop_grace_seconds=5,
    )
    client = OpenProjectClient(BASE_URL, TOKEN, transport=fake.transport, max_attempts=1)

    repo_dir = tmp_path / "ws" / "impl" / "repositories" / "demo"
    repo_dir.mkdir(parents=True)
    standard = tmp_path / "ws" / "impl" / "AGENTS.md"
    standard.write_text("STANDARD CONTENT", encoding="utf-8")

    async def fake_prepare_workspace(**kwargs: Any) -> Workspace:
        return Workspace(repo_dir=repo_dir, branch=BRANCH, standard_path=standard)

    monkeypatch.setattr(
        "op_agent_engine.dispatcher.prepare_workspace", fake_prepare_workspace
    )

    prompts_dir = tmp_path / "prompts"
    prompts_dir.mkdir()
    (prompts_dir / "implement.txt.j2").write_text(
        "Implement {{ wp.subject }} branch={{ branch }} repo={{ repo.github }}",
        encoding="utf-8",
    )
    (prompts_dir / "review.txt.j2").write_text(
        "Review OP#{{ wp.id }} PR {{ pr.url }} branch={{ branch }}",
        encoding="utf-8",
    )
    (prompts_dir / "fix.txt.j2").write_text(
        "Fix {{ wp.subject }} attempt={{ attempt }} feedback={{ feedback.text }}",
        encoding="utf-8",
    )
    (prompts_dir / "resume.txt.j2").write_text(
        "Resume {{ wp.subject }} branch={{ branch }}", encoding="utf-8"
    )
    runner = GenericRunner(config)
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
            gh=FakeGitHub(),
            runner=runner,
            prompts=prompts,
        )

    engine = Engine(config, settings, client=client, dispatcher_factory=factory)
    return engine, fake, client, settings.data_dir / "engine.db"


async def test_new_to_closed_with_the_fake_agent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine, fake, client, store_path = await make_engine(tmp_path, monkeypatch)
    try:
        # Poll 1: New -> implement -> Ready for Review (open PR verified).
        assert await engine.run(max_polls=1) == 0
        assert status_href(fake, WORK_PACKAGE_ID) == (
            f"/api/v3/statuses/{STATUS_READY_FOR_REVIEW}"
        )

        # Poll 2: Ready for Review -> review -> merged PR -> Closed.
        assert await engine.run(max_polls=1) == 0
        assert status_href(fake, WORK_PACKAGE_ID) == (
            f"/api/v3/statuses/{STATUS_CLOSED}"
        )

        store = Store(store_path)
        try:
            runs = [
                run
                for run in store.finished_runs()
                if run.work_package_id == WORK_PACKAGE_ID
            ]
        finally:
            store.close()
        assert [(run.kind, run.state) for run in runs] == [
            ("review", SUCCEEDED),
            ("implement", SUCCEEDED),
        ]
        assert {run.outcome for run in runs} == {"approved", "ready_for_review"}

        # The fake runner's real stdout/stderr logs and the rendered prompts
        # exist for both runs, in run order.
        run_root = tmp_path / "data" / "runs"
        prompts = [
            (run_root / str(run.id) / "prompt.txt").read_text()
            for run in runs
        ]
        assert any("Implement wp 100" in prompt for prompt in prompts)
        assert any("Review OP#100" in prompt for prompt in prompts)
        for run in runs:
            stdout = (run_root / str(run.id) / "stdout.log").read_text()
            assert "fake agent: behavior=" in stdout

        # Secret hardening: the configured GH_TOKEN is redacted from meta.json
        # and appears nowhere in the run directories or the SQLite history.
        assert SECRET not in (run_root / str(runs[0].id) / "meta.json").read_text()
        for path in run_root.rglob("*"):
            if path.is_file():
                assert SECRET not in path.read_text(errors="ignore")
        assert SECRET not in store_path.read_bytes().decode("utf-8", errors="ignore")
    finally:
        await client.aclose()
