"""Tests for workspace preparation.

Uses local bare repositories over ``file://`` URLs so no network access is
needed.
"""

import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

from op_agent_engine.workspace import (
    WORKTREE_ARTEFACT_DIR_NAME,
    GitError,
    Workspace,
    WorkspaceError,
    archive_failed_worktree,
    capture_worktree_state,
    cleanup_stale_worktrees,
    cleanup_workspace,
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
        tmp_path / "ws" / "fe" / "worktrees" / "op-4-100"
    )
    assert workspace.base_repo_dir == (
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
    # The shared clone stays detached so every run branch is free to use.
    head = _git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=second.base_repo_dir)
    assert head.strip() == "HEAD"


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

async def test_runs_use_separate_worktrees(
    tmp_path: Path, git_repo: GitFixture
) -> None:
    root = _workspace_root(tmp_path)

    first = await _prepare(root, git_repo)
    second = await _prepare(root, git_repo, work_package_id=101)

    # Same agent, same clone, different checkouts.
    assert first.repo_dir != second.repo_dir
    assert first.branch != second.branch
    assert first.base_repo_dir == second.base_repo_dir
    assert (first.repo_dir / "README.md").is_file()
    assert (second.repo_dir / "README.md").is_file()
    assert _git(["-C", str(first.repo_dir), "branch", "--show-current"]).strip() == (
        first.branch
    )

    await cleanup_workspace(first)
    await cleanup_workspace(second)

    assert not first.repo_dir.exists()
    assert not second.repo_dir.exists()
    # The shared clone and the checkout of a later run survive.
    assert first.base_repo_dir is not None
    assert (first.base_repo_dir / ".git").exists()


async def test_default_branch_is_discovered_when_not_configured(
    tmp_path: Path, git_repo: GitFixture
) -> None:
    workspace = await _prepare(
        _workspace_root(tmp_path), git_repo, default_branch=None
    )

    assert workspace.default_branch == "main"
    head = _git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=workspace.repo_dir).strip()
    assert head == "op-4-100"

async def test_cleanup_stale_worktrees_removes_old_worktree(
    tmp_path: Path, git_repo: GitFixture
) -> None:
    import os
    from datetime import UTC, datetime, timedelta

    root = _workspace_root(tmp_path)
    workspace = await _prepare(root, git_repo, work_package_id=102)
    old = (datetime.now(UTC) - timedelta(days=40)).timestamp()
    for path in [workspace.repo_dir, *workspace.repo_dir.rglob("*")]:
        os.utime(path, (old, old))

    report = await cleanup_stale_worktrees(root, older_than_days=30)

    assert report.removed_count == 1
    assert report.removed[0].branch == "op-4-102"
    assert not workspace.repo_dir.exists()
    assert workspace.base_repo_dir is not None
    assert (workspace.base_repo_dir / ".git").exists()
    assert _git(
        ["-C", str(workspace.base_repo_dir), "branch", "--list", "op-4-102"]
    ).strip() == ""


async def test_cleanup_stale_worktrees_skips_active_branch(
    tmp_path: Path, git_repo: GitFixture
) -> None:
    import os
    from datetime import UTC, datetime, timedelta

    root = _workspace_root(tmp_path)
    workspace = await _prepare(root, git_repo, work_package_id=103)
    old = (datetime.now(UTC) - timedelta(days=40)).timestamp()
    for path in [workspace.repo_dir, *workspace.repo_dir.rglob("*")]:
        os.utime(path, (old, old))

    report = await cleanup_stale_worktrees(
        root, older_than_days=30, active_branches=frozenset({workspace.branch})
    )

    assert report.removed_count == 0
    assert report.kept == 1
    assert workspace.repo_dir.is_dir()


async def test_cleanup_stale_worktrees_dry_run_keeps_worktree(
    tmp_path: Path, git_repo: GitFixture
) -> None:
    import os
    from datetime import UTC, datetime, timedelta

    root = _workspace_root(tmp_path)
    workspace = await _prepare(root, git_repo, work_package_id=104)
    old = (datetime.now(UTC) - timedelta(days=40)).timestamp()
    for path in [workspace.repo_dir, *workspace.repo_dir.rglob("*")]:
        os.utime(path, (old, old))

    report = await cleanup_stale_worktrees(
        root, older_than_days=30, dry_run=True
    )

    assert report.removed_count == 1
    assert workspace.repo_dir.is_dir()
    assert workspace.base_repo_dir is not None
    assert _git(
        ["-C", str(workspace.base_repo_dir), "branch", "--list", "op-4-104"]
    ).strip() != ""
    assert "would remove" in "\n".join(report.summary())



# -- failed-run worktree capture ------------------------------------------------


async def test_capture_records_status_diffs_and_untracked(
    tmp_path: Path, git_repo: GitFixture
) -> None:
    workspace = await _prepare(_workspace_root(tmp_path), git_repo)
    repo = workspace.repo_dir
    (repo / "README.md").write_text("hello\nchanged by the agent\n", encoding="utf-8")
    (repo / "staged.txt").write_text("staged change\n", encoding="utf-8")
    _git(["-C", str(repo), "add", "staged.txt"])
    (repo / "notes").mkdir()
    (repo / "notes" / "todo.md").write_text("- investigate\n", encoding="utf-8")

    dest = tmp_path / "run" / WORKTREE_ARTEFACT_DIR_NAME
    capture = await capture_worktree_state(workspace, dest)

    assert capture.path == dest
    assert capture.errors == ()
    status = (dest / "status.txt").read_text()
    assert "README.md" in status
    assert "staged.txt" in status
    # Untracked files are included (git status would hide them under a dir).
    assert "notes/todo.md" in status
    # The unstaged patch carries the tracked modification only.
    assert "changed by the agent" in (dest / "diff.patch").read_text()
    assert "staged.txt" not in (dest / "diff.patch").read_text()
    assert "staged change" in (dest / "diff_cached.patch").read_text()
    # A patch cannot express an untracked file, so it is copied verbatim.
    assert (dest / "untracked" / "notes" / "todo.md").read_text() == "- investigate\n"
    assert capture.copied_files == 1
    manifest = (dest / "MANIFEST.txt").read_text()
    assert "status.txt" in manifest
    assert "untracked/notes/todo.md" in manifest
    assert "branch: op-4-100" in manifest


async def test_capture_skips_untracked_files_above_the_caps(
    tmp_path: Path, git_repo: GitFixture
) -> None:
    workspace = await _prepare(_workspace_root(tmp_path), git_repo)
    (workspace.repo_dir / "big.bin").write_bytes(b"x" * 4096)
    (workspace.repo_dir / "small.txt").write_text("small\n", encoding="utf-8")

    dest = tmp_path / "run" / WORKTREE_ARTEFACT_DIR_NAME
    capture = await capture_worktree_state(
        workspace, dest, max_file_bytes=1024, max_total_bytes=2048
    )

    assert (dest / "untracked" / "small.txt").is_file()
    assert not (dest / "untracked" / "big.bin").exists()
    assert capture.copied_files == 1
    assert any("big.bin" in item for item in capture.skipped_files)
    manifest = (dest / "MANIFEST.txt").read_text()
    assert "big.bin" in manifest
    assert "not copied" in manifest


async def test_capture_records_commits_that_were_never_pushed(
    tmp_path: Path, git_repo: GitFixture
) -> None:
    workspace = await _prepare(_workspace_root(tmp_path), git_repo)
    repo = workspace.repo_dir
    (repo / "feature.txt").write_text("feature\n", encoding="utf-8")
    _git(["-C", str(repo), "add", "feature.txt"])
    _git(
        [
            "-C",
            str(repo),
            "-c",
            "user.name=t",
            "-c",
            "user.email=t@example.com",
            "commit",
            "--no-gpg-sign",
            "--quiet",
            "-m",
            "local work only",
        ]
    )

    dest = tmp_path / "run" / WORKTREE_ARTEFACT_DIR_NAME
    await capture_worktree_state(workspace, dest)

    assert "local work only" in (dest / "commits.txt").read_text()
    assert "feature.txt" in (dest / "commits.patch").read_text()
    assert "op-4-100" in (dest / "head.txt").read_text()


async def test_capture_on_a_missing_worktree_is_recorded_not_raised(
    tmp_path: Path, git_repo: GitFixture
) -> None:
    workspace = await _prepare(_workspace_root(tmp_path), git_repo)
    await cleanup_workspace(workspace)
    assert not workspace.repo_dir.exists()

    dest = tmp_path / "run" / WORKTREE_ARTEFACT_DIR_NAME
    capture = await capture_worktree_state(workspace, dest)

    assert capture.path == dest
    assert capture.errors
    # The index is still written so a human sees what was captured.
    assert "capture errors" in (dest / "MANIFEST.txt").read_text()


async def test_archive_failed_worktree_keeps_the_worktree(
    tmp_path: Path, git_repo: GitFixture
) -> None:
    workspace = await _prepare(_workspace_root(tmp_path), git_repo)
    (workspace.repo_dir / "wip.txt").write_text("half done\n", encoding="utf-8")

    archive = await archive_failed_worktree(workspace, run_dir=tmp_path / "run")

    assert archive.kept is True
    assert archive.artefact == tmp_path / "run" / WORKTREE_ARTEFACT_DIR_NAME
    assert (archive.artefact / "status.txt").is_file()
    # The real checkout is still there for a human to inspect.
    assert workspace.repo_dir.is_dir()
    assert (workspace.repo_dir / "wip.txt").read_text() == "half done\n"


async def test_archive_failed_worktree_removes_when_disabled(
    tmp_path: Path, git_repo: GitFixture
) -> None:
    workspace = await _prepare(_workspace_root(tmp_path), git_repo)
    (workspace.repo_dir / "wip.txt").write_text("half done\n", encoding="utf-8")

    archive = await archive_failed_worktree(
        workspace, run_dir=tmp_path / "run", keep=False
    )

    assert archive.kept is False
    assert not workspace.repo_dir.exists()
    # The artefact is written before removal, so it survives.
    assert (tmp_path / "run" / WORKTREE_ARTEFACT_DIR_NAME / "status.txt").is_file()


async def test_archive_failed_worktree_respects_the_size_cap(
    tmp_path: Path, git_repo: GitFixture
) -> None:
    workspace = await _prepare(_workspace_root(tmp_path), git_repo)
    (workspace.repo_dir / "wip.txt").write_text("half done\n", encoding="utf-8")

    archive = await archive_failed_worktree(
        workspace, run_dir=tmp_path / "run", max_keep_bytes=1
    )

    assert archive.kept is False
    assert not workspace.repo_dir.exists()
    assert (tmp_path / "run" / WORKTREE_ARTEFACT_DIR_NAME / "status.txt").is_file()


async def test_prepare_workspace_preserves_a_dirty_worktree(
    tmp_path: Path, git_repo: GitFixture
) -> None:
    root = _workspace_root(tmp_path)
    first = await _prepare(root, git_repo)
    (first.repo_dir / "wip.txt").write_text("work in progress\n", encoding="utf-8")

    second = await _prepare(root, git_repo)

    # The new run gets a pristine worktree on the same branch.
    assert second.repo_dir == first.repo_dir
    assert not (second.repo_dir / "wip.txt").exists()
    assert _git(["branch", "--show-current"], cwd=second.repo_dir).strip() == (
        "op-4-100"
    )
    # The failed run's state was moved aside, not deleted.
    kept = sorted(
        path
        for path in second.repo_dir.parent.iterdir()
        if path.name.startswith("op-4-100.kept-")
    )
    assert len(kept) == 1
    assert (kept[0] / "wip.txt").read_text() == "work in progress\n"
    listing = _git(["-C", str(first.base_repo_dir), "worktree", "list"])
    assert str(kept[0]) in listing


async def test_prepare_workspace_removes_a_clean_worktree(
    tmp_path: Path, git_repo: GitFixture
) -> None:
    root = _workspace_root(tmp_path)
    first = await _prepare(root, git_repo)

    second = await _prepare(root, git_repo)

    assert second.repo_dir == first.repo_dir
    assert not [
        path
        for path in second.repo_dir.parent.iterdir()
        if ".kept-" in path.name
    ]


async def test_prepare_workspace_does_not_keep_an_oversized_worktree(
    tmp_path: Path, git_repo: GitFixture
) -> None:
    root = _workspace_root(tmp_path)
    first = await _prepare(root, git_repo)
    (first.repo_dir / "wip.txt").write_text("work in progress\n", encoding="utf-8")

    second = await _prepare(root, git_repo, max_kept_worktree_bytes=0)

    assert second.repo_dir == first.repo_dir
    assert not [
        path
        for path in second.repo_dir.parent.iterdir()
        if ".kept-" in path.name
    ]


async def test_cleanup_stale_worktrees_removes_a_preserved_worktree(
    tmp_path: Path, git_repo: GitFixture
) -> None:
    import os
    from datetime import UTC, datetime, timedelta

    root = _workspace_root(tmp_path)
    first = await _prepare(root, git_repo, work_package_id=105)
    (first.repo_dir / "wip.txt").write_text("work in progress\n", encoding="utf-8")
    second = await _prepare(root, git_repo, work_package_id=105)
    await cleanup_workspace(second)

    kept = [path for path in first.repo_dir.parent.iterdir() if ".kept-" in path.name]
    assert len(kept) == 1
    old = (datetime.now(UTC) - timedelta(days=40)).timestamp()
    for path in [kept[0], *kept[0].rglob("*")]:
        os.utime(path, (old, old))

    report = await cleanup_stale_worktrees(root, older_than_days=30)

    assert report.removed_count == 1
    assert not kept[0].exists()
    assert report.errors == ()
