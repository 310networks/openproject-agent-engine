"""Workspace preparation.

Each agent has a workspace under ``WORKSPACE_ROOT``:

    <WORKSPACE_ROOT>/<agent>/
        AGENTS.md              the engineering standard (required)
        repositories/<dir>/    the repository clone

Branch naming: ``op-<projectId>-<workPackageId>``.

The engine keeps one clone per agent. When ``MAX_RUNS_PER_AGENT`` allows more
than one run at a time, each run gets its own git worktree under
``worktrees/<branch>`` instead of sharing the clone's checkout; the scheduler
still allows only one run per work package.

Checkout rules:

* implementer: if ``origin/<branch>`` exists, check it out; otherwise create it
  from ``origin/<default_branch>``.
* reviewer: check out ``origin/<branch>`` read-only; if it does not exist the
  run cannot start (no PR to review).
"""

from __future__ import annotations

import asyncio
import contextlib
import shutil
from dataclasses import dataclass
from pathlib import Path

from op_agent_engine.logging import get_logger

logger = get_logger(__name__)

#: Timeout for a single git command (clone of a large repo included).
GIT_TIMEOUT_SECONDS = 300.0


class WorkspaceError(Exception):
    """Workspace preparation failed; the run cannot start."""


class GitError(WorkspaceError):
    """A git command exited non-zero (or timed out)."""

    def __init__(self, args: list[str], returncode: int, stderr: str) -> None:
        self.git_args = args
        self.returncode = returncode
        self.stderr = stderr
        detail = stderr.strip()[:500] or "(no stderr)"
        super().__init__(f"git {' '.join(args)} failed ({returncode}): {detail}")


@dataclass(frozen=True)
class Workspace:
    """A prepared workspace: repository checked out on the run branch."""

    repo_dir: Path
    branch: str
    standard_path: Path
    #: The remote's default branch (discovered when not configured).
    default_branch: str | None = None
    #: The agent's shared clone; set when ``repo_dir`` is a git worktree.
    base_repo_dir: Path | None = None


def clone_url_for(github_repo: str, gh_token: str | None = None) -> str:
    """HTTPS clone URL for *github_repo*.

    With ``gh_token`` the token is embedded (``x-access-token``); without it
    the host's existing ``gh``/git authentication is used.
    """
    if gh_token:
        return f"https://x-access-token:{gh_token}@github.com/{github_repo}.git"
    return f"https://github.com/{github_repo}.git"


async def run_git(
    args: list[str],
    *,
    cwd: Path | None = None,
) -> str:
    """Run one git command and return its stdout; raise :class:`GitError`."""
    process = await asyncio.create_subprocess_exec(
        "git",
        *args,
        cwd=str(cwd) if cwd is not None else None,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        _stdout, stderr = await asyncio.wait_for(
            process.communicate(), timeout=GIT_TIMEOUT_SECONDS
        )
    except TimeoutError as error:
        process.kill()
        with contextlib.suppress(BaseException):
            await process.wait()
        raise GitError(args, -1, "timed out") from error
    returncode = process.returncode
    if returncode is None or returncode != 0:
        raise GitError(
            args,
            returncode if returncode is not None else -1,
            stderr.decode("utf-8", errors="replace"),
        )
    return _stdout.decode("utf-8", errors="replace")


async def remote_default_branch(repo_dir: Path) -> str | None:
    """Return the remote default branch, preferring local tracking data.

    ``origin/HEAD`` is set by a normal clone; after a fetch of an older clone
    it can be stale, so ask the remote with ``ls-remote --symref`` when it is
    not available. Returns ``None`` when neither works.
    """
    prefix = "refs/remotes/origin/"
    try:
        ref = (
            await run_git(
                ["symbolic-ref", "--quiet", "refs/remotes/origin/HEAD"],
                cwd=repo_dir,
            )
        ).strip()
    except GitError:
        ref = ""
    if ref.startswith(prefix):
        branch = ref[len(prefix):].strip()
        if branch:
            return branch

    try:
        output = await run_git(
            ["ls-remote", "--symref", "origin", "HEAD"], cwd=repo_dir
        )
    except GitError:
        output = ""
    ref_prefix = "ref: refs/heads/"
    for line in output.splitlines():
        if not line.startswith(ref_prefix):
            continue
        branch = line[len(ref_prefix):].split("\t", 1)[0].split(" ", 1)[0].strip()
        if branch:
            return branch

    # A remote whose HEAD points at a missing branch (a common state after a
    # rename) exposes no symref. Fall back to the conventional names if they
    # actually exist locally.
    for candidate in ("main", "master"):
        if await _remote_branch_exists(repo_dir, candidate):
            return candidate
    return None


async def _remote_branch_exists(repo_dir: Path, branch: str) -> bool:
    try:
        await run_git(
            ["show-ref", "--verify", "--quiet", f"refs/remotes/origin/{branch}"],
            cwd=repo_dir,
        )
        return True
    except GitError:
        return False


async def prepare_workspace(
    *,
    root: Path,
    agent_workspace: str,
    github_repo: str,
    clone_dir: str,
    default_branch: str | None,
    project_id: int,
    work_package_id: int,
    role: str,
    gh_token: str | None = None,
    clone_url: str | None = None,
    use_worktree: bool = False,
) -> Workspace:
    """Clone/fetch the repository and check out the run branch.

    ``clone_url`` overrides the derived GitHub URL (tests use ``file://``
    URLs for local bare repositories).

    Raises :class:`WorkspaceError` (or :class:`GitError`) when the workspace
    cannot be prepared; the caller records the run as failed.
    """
    workspace_dir = root / agent_workspace
    standard_path = workspace_dir / "AGENTS.md"
    if not standard_path.is_file():
        raise WorkspaceError(
            f"no engineering standard at {standard_path}; the agent workspace "
            "must contain an AGENTS.md"
        )

    repo_dir = workspace_dir / "repositories" / clone_dir
    branch = f"op-{project_id}-{work_package_id}"

    if not (repo_dir / ".git").exists():
        repo_dir.parent.mkdir(parents=True, exist_ok=True)
        logger.info(
            "cloning %s into %s",
            github_repo,
            repo_dir,
            extra={"github_repo": github_repo, "repo_dir": str(repo_dir)},
        )
        await run_git(
            [
                "clone",
                "--quiet",
                clone_url or clone_url_for(github_repo, gh_token),
                str(repo_dir),
            ]
        )
    else:
        await run_git(["fetch", "--quiet", "origin"], cwd=repo_dir)

    remote_branch_exists = await _remote_branch_exists(repo_dir, branch)
    resolved_default_branch = default_branch or await remote_default_branch(repo_dir)

    if role == "implementer":
        if remote_branch_exists:
            base = f"origin/{branch}"
        else:
            if resolved_default_branch is None:
                raise WorkspaceError(
                    "could not determine the default branch for "
                    f"{github_repo}; set GITHUB_DEFAULT_BRANCH explicitly"
                )
            if not await _remote_branch_exists(repo_dir, resolved_default_branch):
                raise WorkspaceError(
                    f"origin/{resolved_default_branch} not found after fetch "
                    f"(repository {github_repo})"
                )
            base = f"origin/{resolved_default_branch}"
    elif role == "reviewer":
        if not remote_branch_exists:
            raise WorkspaceError(
                f"no remote branch origin/{branch} to review (repository "
                f"{github_repo})"
            )
        base = f"origin/{branch}"
    else:
        raise WorkspaceError(f"unknown agent kind {role!r}")

    if use_worktree:
        # A run may share the agent with another run. Give it its own working
        # tree so the two checkouts (and the agents' edits) cannot collide.
        # The shared clone stays detached so every run branch is free to use.
        worktree_dir = workspace_dir / "worktrees" / branch
        await run_git(["worktree", "prune"], cwd=repo_dir)
        if worktree_dir.exists():
            with contextlib.suppress(GitError):
                await run_git(
                    ["worktree", "remove", "--force", str(worktree_dir)],
                    cwd=repo_dir,
                )
            if worktree_dir.exists():
                shutil.rmtree(worktree_dir, ignore_errors=True)
            await run_git(["worktree", "prune"], cwd=repo_dir)
        if not await _head_is_detached(repo_dir):
            await run_git(
                ["checkout", "--quiet", "--detach", base],
                cwd=repo_dir,
            )
        worktree_dir.parent.mkdir(parents=True, exist_ok=True)
        await run_git(
            [
                "worktree",
                "add",
                "--force",
                "-B",
                branch,
                str(worktree_dir),
                base,
            ],
            cwd=repo_dir,
        )
        logger.info(
            "workspace ready: %s on branch %s (worktree of %s)",
            worktree_dir,
            branch,
            repo_dir,
            extra={"repo_dir": str(worktree_dir), "branch": branch},
        )
        return Workspace(
            repo_dir=worktree_dir,
            branch=branch,
            standard_path=standard_path,
            default_branch=resolved_default_branch,
            base_repo_dir=repo_dir,
        )

    await run_git(["checkout", "--quiet", "-B", branch, base], cwd=repo_dir)
    logger.info(
        "workspace ready: %s on branch %s",
        repo_dir,
        branch,
        extra={"repo_dir": str(repo_dir), "branch": branch},
    )
    return Workspace(
        repo_dir=repo_dir,
        branch=branch,
        standard_path=standard_path,
        default_branch=resolved_default_branch,
    )


async def _head_is_detached(repo_dir: Path) -> bool:
    try:
        await run_git(["symbolic-ref", "--quiet", "HEAD"], cwd=repo_dir)
    except GitError:
        return True
    return False


async def cleanup_workspace(workspace: Workspace) -> None:
    """Remove a per-run worktree; a shared clone is left alone."""
    if workspace.base_repo_dir is None:
        return
    with contextlib.suppress(GitError):
        await run_git(
            ["worktree", "remove", "--force", str(workspace.repo_dir)],
            cwd=workspace.base_repo_dir,
        )
    if workspace.repo_dir.exists():
        shutil.rmtree(workspace.repo_dir, ignore_errors=True)
    with contextlib.suppress(GitError):
        await run_git(["worktree", "prune"], cwd=workspace.base_repo_dir)
