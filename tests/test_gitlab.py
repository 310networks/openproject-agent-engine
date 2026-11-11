"""GitLab adapter, configuration and full dispatcher verification paths."""

from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx
import pytest
import respx
from pydantic import SecretStr, ValidationError

from op_agent_engine.config import EngineSettings, RepoConfig, WorkflowConfig
from op_agent_engine.github import GitHubError
from op_agent_engine.gitlab import GitLab
from op_agent_engine.logging import is_secret_name, redact_mapping
from op_agent_engine.openproject.resolve import _repository, expand_projects
from op_agent_engine.prompts import with_standard
from op_agent_engine.store import SUCCEEDED
from op_agent_engine.workspace import GitError, clone_url_for, redact_secrets
from tests.test_dispatcher import (
    STATUS_CLOSED,
    STATUS_IN_TESTING,
    STATUS_ON_HOLD,
    STATUS_READY_FOR_REVIEW,
    FakeRunner,
    activity_comment,
    claim,
    make_dispatcher,
    make_fix_item,
    make_item,
    make_result,
    make_review_item,
    status_href,
    wp,
)
from tests.test_resolve import REPO_FIELD, make_config, make_fake, project_with_repo, resolve

HOST = "gitlab.example.org:8443"
REPO = "group/subgroup/demo"
BRANCH = "op-4-100"
URL = f"https://{HOST}/api/v4/projects/{quote(REPO, safe='')}/merge_requests"
MR_URL = f"https://{HOST}/{REPO}/-/merge_requests/7"


def mr(**overrides: Any) -> dict[str, Any]:
    return {
        "iid": 7, "web_url": MR_URL, "source_branch": BRANCH, "state": "opened",
        "source_project_id": 42, "target_project_id": 42, "draft": False,
        **overrides,
    }


@pytest.mark.parametrize("host", ["gitlab.com", HOST])
@pytest.mark.parametrize("state", ["open", "closed", "merged"])
async def test_lookup_hosts_states(host: str, state: str) -> None:
    gitlab_state = "opened" if state == "open" else state
    with respx.mock as router:
        route = router.get(
            f"https://{host}/api/v4/projects/{quote(REPO, safe='')}/merge_requests",
            params__contains={"source_branch": BRANCH, "state": gitlab_state},
        ).respond(200, json=[mr(
            state=gitlab_state, web_url=f"https://{host}/{REPO}/-/merge_requests/7"
        )])
        result = await GitLab(host=host, token="secret").find_pr(REPO, BRANCH, state=state)
        assert result is not None
        assert result.state == state and result.number == 7 and not result.is_draft
        assert route.calls[0].request.headers["PRIVATE-TOKEN"] == "secret"
        assert b"group%2Fsubgroup%2Fdemo" in route.calls[0].request.url.raw_path


@pytest.mark.parametrize("draft", [{"draft": True}, {"work_in_progress": True}])
async def test_draft_and_legacy_wip(draft: dict[str, bool]) -> None:
    entry = mr()
    del entry["draft"]
    with respx.mock as router:
        router.get(URL).respond(200, json=[{**entry, **draft}])
        result = await GitLab(host=HOST, token="secret").find_pr(REPO, BRANCH)
        assert result is not None and result.is_draft


async def test_paginate_and_exclude_foreign_branch_fork_or_state() -> None:
    with respx.mock as router:
        route = router.get(URL)
        route.side_effect = [
            httpx.Response(200, json=[mr(source_project_id=99), mr(source_branch="other"),
                                     mr(state="closed")], headers={"X-Next-Page": "2"}),
            httpx.Response(200, json=[mr()]),
        ]
        result = await GitLab(host=HOST, token="secret").find_pr(REPO, BRANCH)
        assert result is not None
        assert route.calls[1].request.url.params["page"] == "2"


async def test_empty() -> None:
    with respx.mock as router:
        router.get(URL).respond(200, json=[])
        assert await GitLab(host=HOST, token="secret").find_pr(REPO, BRANCH) is None


@pytest.mark.parametrize("payload", [
    {}, [None], [mr(iid="7")], [mr(web_url="https://evil.example/7")],
    [mr(draft=None)], [mr(state="unexpected")], [mr(iid=-1)],
])
async def test_malformed_response_fails_closed(payload: Any) -> None:
    with respx.mock as router:
        router.get(URL).respond(200, json=payload)
        with pytest.raises(GitHubError, match="invalid merge request response"):
            await GitLab(host=HOST, token="secret").find_pr(REPO, BRANCH)


@pytest.mark.parametrize("status", [301, 401, 403, 404, 429, 500])
async def test_provider_failure_no_secrets_or_redirect(status: int) -> None:
    with respx.mock as router:
        route = router.get(URL).respond(
            status, text="secret", headers={"Location": "https://evil.example"}
        )
        with pytest.raises(GitHubError) as caught:
            await GitLab(host=HOST, token="secret").find_pr(REPO, BRANCH)
        assert "secret" not in str(caught.value)
        assert str(status) in str(caught.value)
        assert route.call_count == 1


async def test_network_timeout() -> None:
    with respx.mock as router:
        router.get(URL).mock(side_effect=httpx.ReadTimeout("secret"))
        with pytest.raises(GitHubError, match="ReadTimeout") as caught:
            await GitLab(host=HOST, token="secret").find_pr(REPO, BRANCH)
        assert "secret" not in str(caught.value)


async def test_invalid_json_pagination_and_missing_token() -> None:
    with respx.mock as router:
        route = router.get(URL).respond(200, text="invalid JSON")
        provider = GitLab(host=HOST, token="secret")
        with pytest.raises(GitHubError):
            await provider.find_pr(REPO, BRANCH)
        route.respond(200, json=[], headers={"x-next-page": "1"})
        with pytest.raises(GitHubError):
            await provider.find_pr(REPO, BRANCH)
    with pytest.raises(GitHubError, match="GITLAB_TOKEN"):
        await GitLab(host=HOST, token=None).find_pr(REPO, BRANCH)


@pytest.mark.parametrize("value", [
    REPO, f"https://{HOST}/{REPO}.git", f"ssh://git@{HOST}/{REPO}",
    f"git@{HOST}:{REPO}.git",
])
def test_repository_normalization(value: str) -> None:
    assert _repository(value, HOST) == REPO


@pytest.mark.parametrize("value", [
    "https://evil.example/a/b", "https://user:password@" + HOST + "/a/b",
    "../project", "a/../project", "a/./project", "a//b", "a/b?query", "a/b#fragment",
    "a/b%2fc", "a/b\\c", "-o option/project", "a/b\n",
])
def test_invalid_repositories(value: str) -> None:
    # Leading/trailing whitespace is intentionally stripped at the OP boundary.
    if value.endswith("\n"):
        with pytest.raises(ValidationError):
            RepoConfig(github=value, git_host=HOST, dir="repo")
    else:
        assert _repository(value, HOST) is None


@pytest.mark.parametrize("host", [
    "https://gitlab.com", "gitlab.com/path", "user:secret@gitlab.com", "gitlab.com?query",
    "gitlab.com#fragment", "git lab.com", "gitlab.com:abc", "gitlab.com:99999",
    "gitlab.com:", "gitlab.com;param", "gitlab.com'", "", "gitlab.com%2fpath",
])
def test_invalid_hosts(host: str) -> None:
    with pytest.raises(ValidationError):
        EngineSettings(git_host=host)


def test_tokens_configuration_and_clone_credentials() -> None:
    settings = EngineSettings(
        op_base_url="https://op.example", op_project="demo", git_host=HOST,
        gitlab_token=SecretStr("shared"), gitlab_token_reviewer=SecretStr("reviewer"),
    )
    config = WorkflowConfig.from_settings(settings, environ={})
    assert config.repo_discovery.git_host == HOST
    assert config.reviewer.env["GITLAB_TOKEN"] == "reviewer"
    assert "GH_TOKEN" not in config.reviewer.env
    assert config.reviewer.env["GIT_AUTHOR_NAME"] == "codeReviewer"
    assert all(a.env["GITLAB_TOKEN"] == "shared" for a in config.implementers)
    assert "gitlab identity: dedicated reviewer token" in config.summary()
    url = clone_url_for(REPO, "private:@/token", git_host=HOST)
    assert url == f"https://oauth2:private%3A%40%2Ftoken@{HOST}/{REPO}.git"
    assert "private" not in redact_secrets(url)
    assert "private" not in str(GitError(["clone", url], 1, "failed"))
    assert is_secret_name("GITLAB_TOKEN_REVIEWER")
    assert redact_mapping({"GITLAB_TOKEN": "secret"}) == {"GITLAB_TOKEN": "***"}
    assert redact_secrets("glpat-abcdefghijklmnopqrstuvwxyz") == "glpat-***"
    assert clone_url_for("a/b") == "https://github.com/a/b.git"
    settings.gitlab_token = None
    assert "GITLAB_TOKEN" in settings.missing_runtime_values()


async def test_repository_discovery_from_project_attribute() -> None:
    config = make_config(
        projects=[{"identifier": "demo"}], repo_discovery={"git_host": HOST}
    )
    fake = make_fake(projects=[project_with_repo("demo", repo=f"https://{HOST}/{REPO}.git")],
                     project_schema=REPO_FIELD)
    await resolve(fake, config)
    assert config.projects[0].repo == RepoConfig(github=REPO, git_host=HOST, dir="demo")
    # Expansion is idempotent on resolved configurations.
    from op_agent_engine.openproject.client import OpenProjectClient
    from tests.test_resolve import BASE_URL, TOKEN
    async with OpenProjectClient(BASE_URL, TOKEN, transport=fake.transport) as client:
        await expand_projects(client, config)


def test_prompt_provider_instructions_no_token(tmp_path: Path) -> None:
    standard = tmp_path / "AGENTS.md"
    standard.write_text("STANDARD")
    prompt = with_standard("TASK", standard, git_host=HOST, repo=REPO)
    assert "Hosting provider: GitLab" in prompt
    assert f"projects/{quote(REPO, safe='')}" in prompt
    assert "RESULT: PR_OPENED" in prompt
    assert "state=merged" in prompt
    assert "PRIVATE-TOKEN: $GITLAB_TOKEN" in prompt
    assert "Hosting provider: GitLab" not in with_standard("TASK", standard)


@pytest.mark.parametrize("scenario,expected", [
    ("implement", STATUS_READY_FOR_REVIEW), ("draft", STATUS_ON_HOLD),
    ("resume", STATUS_READY_FOR_REVIEW), ("fix", STATUS_READY_FOR_REVIEW),
    ("missing", STATUS_ON_HOLD), ("api_error", STATUS_ON_HOLD),
    ("review", STATUS_CLOSED), ("review_merge", STATUS_CLOSED),
    ("scheduled_merge", STATUS_ON_HOLD),
])
async def test_dispatcher_gitlab_workflow(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, scenario: str, expected: int
) -> None:
    review = scenario in {"review", "review_merge", "scheduled_merge"}
    result_line = f"RESULT: {'APPROVED' if review else 'PR_OPENED'} {MR_URL}"
    runner = FakeRunner(make_result(tmp_path, result_line=result_line))
    dispatcher, fake, store, client, _ = await make_dispatcher(
        tmp_path, monkeypatch, runner=runner, max_crash_retries=0,
        work_packages=([wp(100, status=STATUS_IN_TESTING, agent=22)]
                       if review else None),
        settings_overrides={"git_host": HOST, "gitlab_token": SecretStr("shared")},
        config_overrides={
            "projects": [{"identifier": "demo", "repo": {
                "github": REPO, "git_host": HOST, "dir": "demo",
            }}],
            "repo_discovery": {"git_host": HOST},
        },
    )
    if scenario == "fix":
        fake.activities.append(activity_comment(100, "Fix the failing check"))
    try:
        with respx.mock as router:
            route = router.get(URL)
            if scenario in {"review_merge", "scheduled_merge"}:
                # Preflight is open; only a completed, not scheduled, merge succeeds.
                route.side_effect = [
                    httpx.Response(200, json=[mr()]),
                    httpx.Response(200, json=[mr(state="merged")]
                                   if scenario == "review_merge" else []),
                ]
            elif scenario == "api_error":
                route.respond(403)
            else:
                entries = [] if scenario == "missing" else [mr(
                    draft=scenario == "draft", state="merged" if scenario == "review" else "opened"
                )]
                route.respond(200, json=entries)
            item = (make_review_item(100) if review else
                    make_fix_item(100) if scenario == "fix" else make_item(100))
            kind = (
                "review" if review else scenario if scenario in {"fix", "resume"} else "implement"
            )
            await dispatcher.execute(claim(store, item, kind=kind))
        assert status_href(fake, 100) == f"/api/v3/statuses/{expected}"
        if scenario in {"implement", "review", "review_merge", "fix", "resume"}:
            assert store.get_run(1).state == SUCCEEDED
        if scenario == "resume":
            assert runner.requests == []  # Existing non-draft MR is authoritative.
        if scenario == "fix":
            assert len(runner.requests) == 1
            assert "Fix the failing check" in (store.runs_dir / "1" / "prompt.txt").read_text()
    finally:
        await client.aclose()
        store.close()
