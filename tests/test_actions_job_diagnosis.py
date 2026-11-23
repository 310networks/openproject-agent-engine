"""Actions-only readers can diagnose one failed job without settling the workflow."""

from unittest.mock import AsyncMock

import pytest

from op_agent_engine.github import GitHub, GitHubError

REPO = "example/widgets"
RUN = dict(
    id=17,
    run_attempt=2,
    workflow_id=9,
    event="pull_request",
    head_sha="abc",
    name="Tests",
    status="in_progress",
)
JOB = dict(
    run_id=17,
    run_attempt=2,
    name="Render",
    head_sha="abc",
    status="completed",
    conclusion="failure",
    html_url="https://example.com/job/20",
)


@pytest.mark.parametrize(
    ("case", "expected"),
    [
        ("current", True),
        ("unconfigured", False),
        ("foreign-job", False),
        ("wrong-attempt", False),
        ("stale-run", False),
        ("duplicate-run", False),
        ("unconfigured-job", False),
        ("skipped", False),
        ("partial-page", False),
        ("access-denied", False),
        ("foreign-run", False),
    ],
)
async def test_pending_job_lookup(
    case: str, expected: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    profile = {} if case == "unconfigured" else {REPO: ("Render",)}
    gh = GitHub(actionable_checks=profile)
    run = dict(RUN)
    job = dict(JOB)
    if case == "foreign-job":
        job["head_sha"] = "def"
    if case == "foreign-run":
        run["head_sha"] = "def"
    if case == "wrong-attempt":
        job["run_attempt"] = 1
    if case == "unconfigured-job":
        job["name"] = "Optional"
    if case == "skipped":
        job["conclusion"] = "skipped"
    rows = [run, dict(run, id=16)] if case == "duplicate-run" else [run]
    calls = []

    def response(args: list[str], *, what: str) -> object:
        path = args[-1]
        calls.append(path)
        if "/check-runs?" in path:
            raise GitHubError("forbidden")
        if path.endswith("/actions/runs?head_sha=abc&per_page=100"):
            return {"workflow_runs": rows, "total_count": len(rows)}
        if path.endswith("/17/attempts/2/jobs?per_page=100"):
            if case == "access-denied":
                raise GitHubError("forbidden")
            return {"jobs": [job], "total_count": 2 if case == "partial-page" else 1}
        if path.endswith("/runs/17"):
            return dict(run, run_attempt=3) if case == "stale-run" else run
        raise AssertionError(what)

    monkeypatch.setattr(gh, "_run_json", AsyncMock(side_effect=response))
    summary = await gh.commit_checks(REPO, "abc")
    assert summary.is_pending
    assert summary.actionable_failures(("Render",)) == (("Render",) if expected else ())
    if case == "unconfigured":
        assert not any("/jobs?" in path for path in calls)
