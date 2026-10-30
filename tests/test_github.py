"""Tests for the GitHub lookup via the ``gh`` CLI (PLAN section 16)."""

import json
import stat
from pathlib import Path

import pytest

from op_agent_engine.github import GitHub, GitHubError, PullRequest


def _fake_gh(tmp_path: Path, *, body: str = "[]", exit_code: int = 0) -> Path:
    """Write a fake ``gh`` executable that prints *body* and exits *exit_code*."""
    script = tmp_path / "gh"
    script.write_text(
        "#!/bin/sh\n"
        f"cat <<'EOF'\n{body}\nEOF\n"
        f"exit {exit_code}\n",
        encoding="utf-8",
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return script


PR_BODY = json.dumps(
    [
        {
            "number": 7,
            "url": "https://github.com/acme/thing/pull/7",
            "headRefName": "op-4-100",
        }
    ]
)


async def test_find_pr_returns_first_entry(tmp_path: Path) -> None:
    gh = GitHub(executable=str(_fake_gh(tmp_path, body=PR_BODY)))
    pr = await gh.find_pr("acme/thing", "op-4-100")
    assert pr == PullRequest(
        number=7,
        url="https://github.com/acme/thing/pull/7",
        head_branch="op-4-100",
        state="open",
    )


async def test_find_pr_empty_list_is_none(tmp_path: Path) -> None:
    gh = GitHub(executable=str(_fake_gh(tmp_path, body="[]")))
    assert await gh.find_pr("acme/thing", "op-4-100") is None


async def test_find_pr_nonzero_exit_raises(tmp_path: Path) -> None:
    gh = GitHub(executable=str(_fake_gh(tmp_path, body="[]", exit_code=1)))
    with pytest.raises(GitHubError, match="gh pr list failed"):
        await gh.find_pr("acme/thing", "op-4-100")


async def test_find_pr_invalid_json_raises(tmp_path: Path) -> None:
    gh = GitHub(executable=str(_fake_gh(tmp_path, body="not json")))
    with pytest.raises(GitHubError, match="invalid JSON"):
        await gh.find_pr("acme/thing", "op-4-100")


async def test_find_pr_missing_binary_raises(tmp_path: Path) -> None:
    gh = GitHub(executable=str(tmp_path / "definitely-not-a-binary"))
    with pytest.raises(GitHubError, match="not found"):
        await gh.find_pr("acme/thing", "op-4-100")


async def test_find_pr_timeout_raises(tmp_path: Path) -> None:
    script = tmp_path / "gh"
    script.write_text("#!/bin/sh\nsleep 5\n", encoding="utf-8")
    script.chmod(script.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    gh = GitHub(executable=str(script), timeout=0.3)
    with pytest.raises(GitHubError, match="timed out"):
        await gh.find_pr("acme/thing", "op-4-100")


PR_BODY_DRAFT = json.dumps(
    [
        {
            "number": 8,
            "url": "https://github.com/acme/thing/pull/8",
            "headRefName": "op-4-101",
            "isDraft": True,
        }
    ]
)


async def test_find_pr_reports_draft_flag(tmp_path: Path) -> None:
    """A draft PR is not authoritative for an implementation result."""
    gh = GitHub(executable=str(_fake_gh(tmp_path, body=PR_BODY_DRAFT)))
    pr = await gh.find_pr("acme/thing", "op-4-101")
    assert pr is not None
    assert pr.is_draft is True


async def test_find_pr_without_is_draft_is_not_a_draft(tmp_path: Path) -> None:
    gh = GitHub(executable=str(_fake_gh(tmp_path, body=PR_BODY)))
    pr = await gh.find_pr("acme/thing", "op-4-100")
    assert pr is not None
    assert pr.is_draft is False
