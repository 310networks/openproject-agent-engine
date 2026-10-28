"""Tests for workspace preparation (PLAN section 11).

Uses local bare repositories over ``file://`` URLs so no network access is
needed.
"""

import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

from op_agent_engine.workspace import (
    GitError,
    Workspace,
    WorkspaceError,
    clone_url_for,
    prepare_workspace,
)

# -- local git helpers --------------------------------------------------------


def _git(args: list[str], cwd: Path | None = None) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=str(cwd) if cwd is not None else None,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout


@dataclass(frozen=True)
class GitFixture:
    """A bare origin with one commit on the default branch."""

    origin: Path
    default_branch: str

    @property
    def clone_url(self) -> str:
        return f"file://{self.origin}"

    def push_branch(self, branch: str, extra_file: str) -> None:
        """Push *branch* (with one extra commit) to the bare origin."""
        work = self.origin.parent / f"push-{branch}"
        _git(["clone", "--quiet", str(self.origin), str(work)])
        _git(
            [
                "-C",
                str(work),
                "checkout",
                "--quiet",
                "-b",
                branch,
                f"origin/{self.default_branch}",
            ]
        )
        (work / extra_file).write_text(extra_file, encoding="utf-8")
        _git(["-C", str(work), "add", extra_file])
        _git(
            [
                "-C",
                str(work),
                "-c",
                "user.name=t",
                "-c",
                "user.email=t@example.com",
                "commit",
                "--no-gpg-sign",
                "--quiet",
                "-m",
                f"extra {extra_file}",
            ]
        )
        _git(["-C", str(work), "push", "--quiet", "origin", branch])

    def push_default(self, extra_file: str) -> None:
        """Push one extra commit to the default branch."""
        self.push_branch(self.default_branch, extra_file)


@pytest.fixture()
def git_repo(tmp_path: Path) -> GitFixture:
    origin = tmp_path / "origin.git"
    _git(["init", "--bare", "--quiet", str(origin)])
    seed = tmp_path / "seed"
    _git(["clone", "--quiet", str(origin), str(seed)])
    (seed / "README.md").write_text("hello\n", encoding="utf-8")
    _git(["-C", str(seed), "add", "README.md"])
    _git(
        [
            "-C",
            str(seed),
            "-c",
            "user.name=t",
            "-c",
            "user.email=t@example.com",
            "commit",
            "--no-gpg-sign",
            "--quiet",
            "-m",
            "initial",
        ]
    )
    _git(["-C", str(seed), "push", "--quiet", "origin", "HEAD:refs/heads/main"])
    return GitFixture(origin=origin, default_branch="main")


def _workspace_root(tmp_path: Path, agent: str = "fe") -> Path:
    root = tmp_path / "ws"
    agent_dir = root / agent
    agent_dir.mkdir(parents=True)
    (agent_dir / "AGENTS.md").write_text("# Standard\n", encoding="utf-8")
    return root


async def _prepare(root: Path, git_repo: GitFixture, **overrides: object) -> Workspace:
    kwargs: dict[str, object] = {
        "root": root,
        "agent_workspace": "fe",
        "github_repo": "acme/thing",
        "clone_dir": "thing",
        "default_branch": git_repo.default_branch,
        "project_id": 4,
        "work_package_id": 100,
        "role": "implementer",
        "clone_url": git_repo.clone_url,
    }
    kwargs.update(overrides)
    return await prepare_workspace(**kwargs)  # type: ignore[arg-type]


# -- clone + checkout ----------------------------------------------------------


async def test_implementer_creates_branch_from_default(
    tmp_path: Path, git_repo: GitFixture
) -> None:
    workspace = await _prepare(_workspace_root(tmp_path), git_repo)
    assert workspace.branch == "op-4-100"
    assert workspace.repo_dir == (
        tmp_path / "ws" / "fe" / "repositories" / "thing"
    )
    assert workspace.standard_path.is_file()
    head = _git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=workspace.repo_dir).strip()
    assert head == "op-4-100"
    # The branch contains the initial commit.
    assert (workspace.repo_dir / "README.md").is_file()


async def test_implementer_reuses_existing_remote_branch(
    tmp_path: Path, git_repo: GitFixture
) -> None:
    git_repo.push_branch("op-4-100", "extra.txt")
    workspace = await _prepare(_workspace_root(tmp_path), git_repo)
    # The extra commit from the remote branch is present.
    assert (workspace.repo_dir / "extra.txt").is_file()
    head = _git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=workspace.repo_dir).strip()
    assert head == "op-4-100"


async def test_implementer_fetches_new_default_commits(
    tmp_path: Path, git_repo: GitFixture
) -> None:
    root = _workspace_root(tmp_path)
    first = await _prepare(root, git_repo)
    # A new commit lands on the default branch upstream.
    git_repo.push_default("newer.txt")
    second = await _prepare(root, git_repo)
    assert second.repo_dir == first.repo_dir
    # The existing clone was fetched; origin/main now has the new commit.
    log = _git(
        ["log", "--oneline", "origin/main", "--", "newer.txt"], cwd=second.repo_dir
    )
    assert "newer.txt" in log


async def test_reviewer_checks_out_existing_branch(
    tmp_path: Path, git_repo: GitFixture
) -> None:
    git_repo.push_branch("op-4-100", "extra.txt")
    workspace = await _prepare(
        _workspace_root(tmp_path), git_repo, role="reviewer"
    )
    assert workspace.branch == "op-4-100"
    assert (workspace.repo_dir / "extra.txt").is_file()


async def test_reviewer_fails_without_remote_branch(
    tmp_path: Path, git_repo: GitFixture
) -> None:
    with pytest.raises(WorkspaceError, match="no remote branch"):
        await _prepare(_workspace_root(tmp_path), git_repo, role="reviewer")


async def test_missing_standard_fails(tmp_path: Path, git_repo: GitFixture) -> None:
    root = tmp_path / "ws"
    (root / "fe").mkdir(parents=True)  # no AGENTS.md
    with pytest.raises(WorkspaceError, match="no engineering standard"):
        await _prepare(root, git_repo)


async def test_unknown_role_fails(tmp_path: Path, git_repo: GitFixture) -> None:
    with pytest.raises(WorkspaceError, match="unknown agent kind"):
        await _prepare(_workspace_root(tmp_path), git_repo, role="resumer")


async def test_clone_failure_raises_git_error(
    tmp_path: Path, git_repo: GitFixture
) -> None:
    with pytest.raises(GitError):
        await _prepare(
            _workspace_root(tmp_path),
            git_repo,
            clone_url="file:///nonexistent/repo.git",
        )


def test_clone_url_for() -> None:
    assert clone_url_for("acme/thing") == "https://github.com/acme/thing.git"
    assert clone_url_for("acme/thing", "tok123") == (
        "https://x-access-token:tok123@github.com/acme/thing.git"
    )
