"""Workspace preparation.

Each agent has a workspace under ``WORKSPACE_ROOT``:

    <WORKSPACE_ROOT>/<agent>/
        AGENTS.md              the engineering standard (required)
        repositories/<dir>/    the repository clone

Branch naming: ``op-<projectId>-<workPackageId>``.

The engine keeps one clone per agent. Every run gets its own git worktree
under ``worktrees/<branch>`` instead of sharing the clone's checkout. The
shared clone stays detached, so a run that times out or is killed leaves its
uncommitted work in its own worktree and cannot dirty the clone used by later
runs. The scheduler still allows only one run per work package and enforces
``MAX_RUNS_PER_AGENT``.

The worktree is removed after every run. ``cleanup_stale_worktrees`` removes
worktrees left behind by an engine crash or a failed removal.

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
from datetime import UTC, datetime, timedelta
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
        if gh_token is not None and clone_url is None:
            # Keep the stored remote credential in step with the configured
            # token. Reviewer and implementer workspaces are separate, so this
            # is how a role (and a rotated token) keeps its own identity.
            await run_git(
                [
                    "remote",
                    "set-url",
                    "origin",
                    clone_url_for(github_repo, gh_token),
                ],
                cwd=repo_dir,
            )
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

    # Every run gets its own working tree so checkouts (and the agents'
    # edits) cannot collide. The shared clone stays detached so every run
    # branch is free to use and a killed run cannot leave it dirty.
    worktree_dir = workspace_dir / "worktrees" / branch
    await _remove_worktree(repo_dir, worktree_dir)
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


async def _head_is_detached(repo_dir: Path) -> bool:
    try:
        await run_git(["symbolic-ref", "--quiet", "HEAD"], cwd=repo_dir)
    except GitError:
        return True
    return False


async def _remove_worktree(base_repo_dir: Path, worktree_dir: Path) -> None:
    """Remove *worktree_dir* and its git metadata, if present."""
    await run_git(["worktree", "prune"], cwd=base_repo_dir)
    if worktree_dir.exists():
        with contextlib.suppress(GitError):
            await run_git(
                ["worktree", "remove", "--force", str(worktree_dir)],
                cwd=base_repo_dir,
            )
        if worktree_dir.exists():
            shutil.rmtree(worktree_dir, ignore_errors=True)
        await run_git(["worktree", "prune"], cwd=base_repo_dir)


async def cleanup_workspace(workspace: Workspace) -> None:
    """Remove a per-run worktree; the shared clone is left alone.

    Cleanup is best-effort: a git failure must not turn a finished run into an
    engine exception. The stale-worktree sweep retries leftovers later.
    """
    if workspace.base_repo_dir is None:
        return
    with contextlib.suppress(GitError):
        await _remove_worktree(workspace.base_repo_dir, workspace.repo_dir)


# -- stale worktree retention -------------------------------------------------


@dataclass(frozen=True)
class RemovedWorktree:
    """One worktree selected (and unless dry-run, removed) by cleanup."""

    path: Path
    branch: str
    age_seconds: float
    size_bytes: int


@dataclass(frozen=True)
class WorktreeCleanupReport:
    """Result of one stale-worktree retention pass."""

    root: Path
    older_than_days: int
    dry_run: bool
    removed: tuple[RemovedWorktree, ...] = ()
    kept: int = 0
    errors: tuple[str, ...] = ()

    @property
    def removed_count(self) -> int:
        return len(self.removed)

    @property
    def bytes_freed(self) -> int:
        return sum(item.size_bytes for item in self.removed)

    def as_dict(self) -> dict[str, object]:
        return {
            "root": str(self.root),
            "older_than_days": self.older_than_days,
            "dry_run": self.dry_run,
            "removed_count": self.removed_count,
            "kept": self.kept,
            "bytes_freed": self.bytes_freed,
            "removed": [
                {
                    "path": str(item.path),
                    "branch": item.branch,
                    "age_hours": round(item.age_seconds / 3600, 1),
                    "size_bytes": item.size_bytes,
                }
                for item in self.removed
            ],
            "errors": list(self.errors),
        }

    def summary(self) -> list[str]:
        verb = "would remove" if self.dry_run else "removed"
        lines = [
            f"worktree root: {self.root}",
            f"older than: {self.older_than_days} day(s)",
            f"{verb}: {self.removed_count} worktree(s), {self.bytes_freed} bytes",
            f"kept: {self.kept}",
        ]
        for item in self.removed:
            lines.append(
                f"  - {item.branch}: {item.size_bytes} bytes, "
                f"{item.age_seconds / 3600:.1f}h old"
            )
        for error in self.errors:
            lines.append(f"  ! {error}")
        return lines


def _worktree_usage(path: Path) -> tuple[int, float]:
    """Return ``(bytes, newest_mtime)`` for a worktree directory."""
    total = 0
    newest = path.stat().st_mtime
    for child in path.rglob("*"):
        try:
            stat = child.stat()
        except OSError:
            continue
        newest = max(newest, stat.st_mtime)
        if child.is_file() and not child.is_symlink():
            total += stat.st_size
    return total, newest


def _base_repo_for_worktree(worktree_dir: Path) -> Path | None:
    """Resolve the shared clone from a linked worktree's ``.git`` file."""
    try:
        content = (worktree_dir / ".git").read_text(encoding="utf-8")
    except OSError:
        return None
    if not content.startswith("gitdir:"):
        return None
    git_dir = Path(content.split(":", 1)[1].strip())
    # <clone>/.git/worktrees/<branch>
    if (
        git_dir.is_absolute()
        and git_dir.parent.name == "worktrees"
        and git_dir.parent.parent.name == ".git"
    ):
        return git_dir.parent.parent.parent
    return None


async def cleanup_stale_worktrees(
    root: Path,
    *,
    older_than_days: int,
    dry_run: bool = False,
    now: datetime | None = None,
    active_branches: frozenset[str] = frozenset(),
) -> WorktreeCleanupReport:
    """Remove worktrees older than *older_than_days*.

    A worktree is normally removed immediately after its run. This sweep only
    handles leftovers from an engine crash, a failed removal, or a ticket that
    was closed while a run was still in flight. ``active_branches`` protects
    worktrees that the store still reports as running.
    """
    if older_than_days < 0:
        raise ValueError("older_than_days must not be negative")

    current = now or datetime.now(UTC)
    if current.tzinfo is None:
        current = current.replace(tzinfo=UTC)
    cutoff = current - timedelta(days=older_than_days)

    removed: list[RemovedWorktree] = []
    errors: list[str] = []
    kept = 0

    try:
        agent_dirs = sorted(root.iterdir(), key=lambda item: item.name)
    except FileNotFoundError:
        agent_dirs = []
    except OSError as error:
        return WorktreeCleanupReport(
            root=root,
            older_than_days=older_than_days,
            dry_run=dry_run,
            errors=(f"cannot read {root}: {error}",),
        )

    for agent_dir in agent_dirs:
        worktrees_dir = agent_dir / "worktrees"
        try:
            worktrees = sorted(worktrees_dir.iterdir(), key=lambda item: item.name)
        except FileNotFoundError:
            continue
        except OSError as error:
            errors.append(f"cannot read {worktrees_dir}: {error}")
            continue
        for worktree_dir in worktrees:
            branch = worktree_dir.name
            try:
                if not worktree_dir.is_dir() or worktree_dir.is_symlink():
                    continue
                size_bytes, newest_mtime = _worktree_usage(worktree_dir)
            except OSError as error:
                errors.append(f"cannot stat {worktree_dir}: {error}")
                continue
            if branch in active_branches:
                kept += 1
                continue
            modified = datetime.fromtimestamp(newest_mtime, tz=UTC)
            if modified >= cutoff:
                kept += 1
                continue
            age_seconds = max(0.0, (current - modified).total_seconds())
            if not dry_run:
                base_repo = _base_repo_for_worktree(worktree_dir)
                if base_repo is None:
                    errors.append(
                        f"cannot locate the shared clone for {worktree_dir}"
                    )
                    continue
                try:
                    await _remove_worktree(base_repo, worktree_dir)
                    # The worktree is gone; drop the local branch too. Its
                    # remote-tracking branch and any pushed commits remain.
                    with contextlib.suppress(GitError):
                        await run_git(["branch", "-D", branch], cwd=base_repo)
                except (GitError, OSError) as error:
                    errors.append(f"cannot remove {worktree_dir}: {error}")
                    continue
            removed.append(
                RemovedWorktree(
                    path=worktree_dir,
                    branch=branch,
                    age_seconds=age_seconds,
                    size_bytes=size_bytes,
                )
            )

    return WorktreeCleanupReport(
        root=root,
        older_than_days=older_than_days,
        dry_run=dry_run,
        removed=tuple(removed),
        kept=kept,
        errors=tuple(errors),
    )
