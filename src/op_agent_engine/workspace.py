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

A successful run's worktree is removed as soon as the run is verified. A
failed run's worktree is first recorded under its run directory
(``runs/<runId>/worktree``: ``git status``, the diffs, unpushed commits and
the untracked files) and then kept on disk until the retention window expires,
unless ``KEEP_FAILED_WORKTREES`` is false or the worktree is larger than
``FAILED_WORKTREE_MAX_MB``. ``cleanup_stale_worktrees`` removes worktrees left
behind by an engine crash, a failed removal or the retention window of such a
kept worktree.

A worktree that is still dirty when a new run for the same branch starts is
moved aside (``<branch>.kept-<utc>``) instead of being deleted, so the state a
failed run left behind survives the resume run that follows it.

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

#: Directory under a run directory that holds a failed run's worktree state.
WORKTREE_ARTEFACT_DIR_NAME = "worktree"

#: Largest untracked file copied into an artefact; bigger ones are only listed
#: in ``MANIFEST.txt`` (1 MiB).
CAPTURE_MAX_FILE_BYTES = 1024 * 1024

#: Total size of the untracked files copied into one artefact (20 MiB).
CAPTURE_MAX_TOTAL_BYTES = 20 * 1024 * 1024

#: Largest worktree kept on disk for a failed run (2 GiB). Anything bigger is
#: removed after its state has been captured, so installed toolchains cannot
#: fill the volume.
DEFAULT_FAILED_WORKTREE_MAX_BYTES = 2 * 1024 * 1024 * 1024


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
    try:
        process = await asyncio.create_subprocess_exec(
            "git",
            *args,
            cwd=str(cwd) if cwd is not None else None,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except OSError as error:
        # e.g. a missing working directory: report it like any git failure so
        # callers only have to handle GitError.
        raise GitError(args, -1, str(error)) from error
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
    max_kept_worktree_bytes: int = DEFAULT_FAILED_WORKTREE_MAX_BYTES,
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
    await _reclaim_worktree(
        repo_dir, worktree_dir, max_keep_bytes=max_kept_worktree_bytes
    )
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


async def _worktree_is_dirty(worktree_dir: Path) -> bool:
    """Whether *worktree_dir* has changes (or cannot be inspected).

    An unreadable worktree is treated as dirty: keeping it is harmless, while
    deleting state that cannot be inspected loses the evidence.
    """
    try:
        status = await run_git(
            ["status", "--porcelain=v1", "-uall"], cwd=worktree_dir
        )
    except GitError:
        return True
    return bool(status.strip())


def _preserved_path(worktree_dir: Path) -> Path:
    """A free sibling path that keeps a worktree's name recognisable."""
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    candidate = worktree_dir.parent / f"{worktree_dir.name}.kept-{stamp}"
    counter = 1
    while candidate.exists():
        candidate = worktree_dir.parent / f"{worktree_dir.name}.kept-{stamp}-{counter}"
        counter += 1
    return candidate


async def _reclaim_worktree(
    base_repo_dir: Path,
    worktree_dir: Path,
    *,
    max_keep_bytes: int,
) -> Path | None:
    """Clear *worktree_dir* before a new run, preserving a dirty one.

    A worktree that still has changes belongs to a run that failed without
    committing; it is moved aside (``<branch>.kept-<utc>``) so its state
    survives the resume run, and the retention sweep removes it later. A clean
    worktree, or one larger than *max_keep_bytes*, is removed as before.
    Returns the path the dirty worktree was preserved at, if any.
    """
    await run_git(["worktree", "prune"], cwd=base_repo_dir)
    if not worktree_dir.exists():
        return None

    preserved: Path | None = None
    if await _worktree_is_dirty(worktree_dir):
        size_bytes = worktree_size_bytes(worktree_dir)
        if max_keep_bytes > 0 and size_bytes <= max_keep_bytes:
            target = _preserved_path(worktree_dir)
            # Detach first: the branch is needed by the new run's worktree, and
            # git refuses to force it while it is checked out elsewhere.
            with contextlib.suppress(GitError):
                await run_git(
                    ["checkout", "--quiet", "--detach"], cwd=worktree_dir
                )
            try:
                await run_git(
                    ["worktree", "move", str(worktree_dir), str(target)],
                    cwd=base_repo_dir,
                )
            except GitError as error:
                logger.warning(
                    "could not preserve the dirty worktree %s: %s",
                    worktree_dir,
                    error,
                    extra={"repo_dir": str(worktree_dir)},
                )
            else:
                preserved = target
                logger.warning(
                    "the worktree %s still had changes; kept it as %s "
                    "(%d bytes) for post-mortem",
                    worktree_dir,
                    target,
                    size_bytes,
                    extra={"repo_dir": str(target), "size_bytes": size_bytes},
                )
        else:
            logger.warning(
                "the worktree %s still had changes but is %d bytes (cap %d); "
                "removing it",
                worktree_dir,
                size_bytes,
                max_keep_bytes,
                extra={"repo_dir": str(worktree_dir), "size_bytes": size_bytes},
            )

    if preserved is None or worktree_dir.exists():
        await _remove_worktree(base_repo_dir, worktree_dir)
    return preserved


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


# -- failed-run worktree capture ----------------------------------------------


def worktree_size_bytes(path: Path) -> int:
    """Bytes used by a worktree directory (0 when it cannot be measured)."""
    try:
        return _worktree_usage(path)[0]
    except OSError:
        return 0


@dataclass(frozen=True)
class WorktreeCapture:
    """The files written for a failed run's worktree state.

    ``path`` is ``None`` when the artefact directory itself could not be
    created; ``errors`` then explains why. Capture never raises: a failed
    capture must not turn a failed run into an engine exception.
    """

    path: Path | None
    size_bytes: int = 0
    copied_files: int = 0
    skipped_files: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()


@dataclass(frozen=True)
class WorktreeArchive:
    """What the engine recorded for a failed run and whether it kept the tree."""

    artefact: Path | None
    kept: bool
    size_bytes: int = 0
    copied_files: int = 0
    skipped_files: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, object]:
        return {
            "artefact": str(self.artefact) if self.artefact is not None else None,
            "kept": self.kept,
            "size_bytes": self.size_bytes,
            "copied_files": self.copied_files,
            "skipped_files": list(self.skipped_files),
            "errors": list(self.errors),
        }


async def _ref_exists(repo_dir: Path, ref: str) -> bool:
    try:
        await run_git(["rev-parse", "--verify", "--quiet", ref], cwd=repo_dir)
    except GitError:
        return False
    return True


async def _capture_base_ref(
    repo_dir: Path, branch: str, default_branch: str | None
) -> str | None:
    """The commit the branch started from (for the unpushed-commit dump)."""
    candidates = [f"origin/{branch}"]
    if default_branch is not None:
        candidates.append(f"origin/{default_branch}")
    for candidate in candidates:
        if await _ref_exists(repo_dir, candidate):
            return candidate
    return None


async def capture_worktree_state(
    workspace: Workspace,
    dest_dir: Path,
    *,
    max_file_bytes: int = CAPTURE_MAX_FILE_BYTES,
    max_total_bytes: int = CAPTURE_MAX_TOTAL_BYTES,
) -> WorktreeCapture:
    """Write the state of a failed run's worktree into *dest_dir*.

    The artefact is a plain directory a human can read with shell tools:

    ``status.txt``
        ``git status --porcelain=v1 -uall`` (untracked files included).
    ``diff.patch``
        ``git diff --binary``: unstaged changes, binary ones included.
    ``diff_cached.patch``
        ``git diff --binary --cached``: staged changes.
    ``commits.txt`` / ``commits.patch``
        Commits that exist only locally (never pushed), which no diff of the
        working tree would show.
    ``head.txt``
        The branch and the HEAD commit of the captured worktree.
    ``untracked/``
        Untracked files copied verbatim, because a patch cannot express them.
        Files above the size caps are listed in ``MANIFEST.txt`` instead.
    ``MANIFEST.txt``
        Index of everything above, with the files that were skipped and why.

    Capture is best effort: every git failure is recorded in ``errors`` and
    the remaining files are still written.
    """
    dest = Path(dest_dir)
    try:
        dest.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        return WorktreeCapture(
            path=None, errors=(f"cannot create {dest}: {error}",)
        )

    repo_dir = workspace.repo_dir
    errors: list[str] = []
    written: list[tuple[str, int]] = []
    skipped: list[str] = []

    def save(name: str, text: str) -> None:
        try:
            (dest / name).write_text(text, encoding="utf-8")
        except OSError as error:
            errors.append(f"cannot write {name}: {error}")
            return
        written.append((name, len(text.encode("utf-8"))))

    async def capture(name: str, args: list[str]) -> str | None:
        try:
            return await run_git(args, cwd=repo_dir)
        except (GitError, OSError) as error:
            errors.append(f"{name}: {error}")
            return None

    status = await capture("status", ["status", "--porcelain=v1", "-uall"])
    if status is not None:
        save("status.txt", status)

    unstaged = await capture("diff", ["diff", "--binary", "--no-color"])
    if unstaged is not None:
        save("diff.patch", unstaged)

    staged = await capture(
        "diff --cached", ["diff", "--binary", "--no-color", "--cached"]
    )
    if staged is not None:
        save("diff_cached.patch", staged)

    head = await capture("rev-parse HEAD", ["rev-parse", "HEAD"])
    base = await _capture_base_ref(repo_dir, workspace.branch, workspace.default_branch)
    head_lines = [
        f"branch: {workspace.branch}",
        f"worktree: {repo_dir}",
        f"HEAD: {head.strip() if head is not None else 'unknown'}",
        f"base: {base or 'unknown (no origin ref)'}",
    ]
    save("head.txt", "\n".join(head_lines) + "\n")

    if base is not None:
        local_commits = await capture(
            "log", ["log", "--oneline", "--no-decorate", f"{base}..HEAD"]
        )
        if local_commits is not None:
            save(
                "commits.txt",
                local_commits or f"(no commits on {workspace.branch} beyond {base})\n",
            )
        commit_diff = await capture(
            "commit diff",
            ["diff", "--binary", "--no-color", f"{base}..HEAD"],
        )
        if commit_diff:
            save("commits.patch", commit_diff)

    untracked_raw = await capture(
        "ls-files", ["ls-files", "--others", "--exclude-standard", "-z"]
    )
    copied_files = 0
    copied_bytes = 0
    for rel in (untracked_raw or "").split("\0"):
        if not rel:
            continue
        source = repo_dir / rel
        try:
            if source.is_symlink():
                skipped.append(f"{rel} (symlink)")
                continue
            size = source.stat().st_size
        except OSError as error:
            skipped.append(f"{rel} (unreadable: {error})")
            continue
        if size > max_file_bytes:
            skipped.append(
                f"{rel} ({size} bytes exceeds the {max_file_bytes} byte "
                "per-file cap)"
            )
            continue
        if copied_bytes + size > max_total_bytes:
            skipped.append(
                f"{rel} ({max_total_bytes} byte artefact cap reached)"
            )
            continue
        target = dest / "untracked" / rel
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
        except OSError as error:
            skipped.append(f"{rel} (copy failed: {error})")
            continue
        copied_files += 1
        copied_bytes += size
        written.append((f"untracked/{rel}", size))

    manifest = [
        "op-agent-engine: state of a failed run's worktree",
        "",
        *head_lines,
        "",
        "status.txt           git status --porcelain=v1 -uall",
        "diff.patch           git diff --binary (unstaged changes)",
        "diff_cached.patch    git diff --binary --cached (staged changes)",
        "commits.txt          local commits that were never pushed",
        "commits.patch        diff of those commits against the base",
        "untracked/           untracked files copied verbatim",
        "",
        f"files written: {len(written)}",
    ]
    manifest.extend(f"  {name}: {size} bytes" for name, size in written)
    if skipped:
        manifest.extend(["", f"not copied: {len(skipped)}"])
        manifest.extend(f"  {item}" for item in skipped)
    if errors:
        manifest.extend(["", f"capture errors: {len(errors)}"])
        manifest.extend(f"  {item}" for item in errors)
    manifest_text = "\n".join(manifest) + "\n"
    try:
        (dest / "MANIFEST.txt").write_text(manifest_text, encoding="utf-8")
    except OSError as error:
        errors.append(f"cannot write MANIFEST.txt: {error}")

    total_bytes = sum(size for _name, size in written) + len(
        manifest_text.encode("utf-8")
    )
    return WorktreeCapture(
        path=dest,
        size_bytes=total_bytes,
        copied_files=copied_files,
        skipped_files=tuple(skipped),
        errors=tuple(errors),
    )


async def archive_failed_worktree(
    workspace: Workspace,
    *,
    run_dir: Path,
    keep: bool = True,
    max_keep_bytes: int = DEFAULT_FAILED_WORKTREE_MAX_BYTES,
) -> WorktreeArchive:
    """Record a failed run's worktree state, then keep or remove the worktree.

    The artefact is always written (``<run_dir>/worktree``) before the worktree
    is touched. The worktree itself is then kept on disk so a human can inspect
    the real checkout, unless ``keep`` is false or it is larger than
    *max_keep_bytes*; ``cleanup_stale_worktrees`` removes kept worktrees once
    they are older than ``RUN_RETENTION_DAYS``.
    """
    capture = await capture_worktree_state(
        workspace, run_dir / WORKTREE_ARTEFACT_DIR_NAME
    )
    size_bytes = worktree_size_bytes(workspace.repo_dir)

    if workspace.base_repo_dir is None:
        # Not a linked worktree (tests, or a workspace prepared elsewhere):
        # there is no clone to unregister it from, so it stays where it is.
        return WorktreeArchive(
            artefact=capture.path,
            kept=False,
            size_bytes=size_bytes,
            copied_files=capture.copied_files,
            skipped_files=capture.skipped_files,
            errors=capture.errors,
        )

    log_extra = {"repo_dir": str(workspace.repo_dir), "size_bytes": size_bytes}
    keep_it = False
    if not keep:
        logger.info(
            "KEEP_FAILED_WORKTREES is false; removing the failed run's worktree "
            "%s after capturing it",
            workspace.repo_dir,
            extra=log_extra,
        )
    elif max_keep_bytes <= 0:
        logger.warning(
            "FAILED_WORKTREE_MAX_MB is 0; removing the failed run's worktree %s "
            "after capturing it",
            workspace.repo_dir,
            extra=log_extra,
        )
    elif size_bytes > max_keep_bytes:
        logger.warning(
            "the failed run's worktree %s is %d bytes, above the %d byte cap; "
            "removing it (the artefact under the run directory is kept)",
            workspace.repo_dir,
            size_bytes,
            max_keep_bytes,
            extra=log_extra,
        )
    else:
        keep_it = True
        logger.warning(
            "kept the failed run's worktree %s (%d bytes) for post-mortem; "
            "cleanup removes it after the retention window",
            workspace.repo_dir,
            size_bytes,
            extra=log_extra,
        )

    if not keep_it:
        await cleanup_workspace(workspace)

    return WorktreeArchive(
        artefact=capture.path,
        kept=keep_it,
        size_bytes=size_bytes,
        copied_files=capture.copied_files,
        skipped_files=capture.skipped_files,
        errors=capture.errors,
    )


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

    A successful run removes its worktree immediately. This sweep handles the
    worktrees a failed run keeps for post-mortem (they expire here with the
    same ``RUN_RETENTION_DAYS`` window as its run directory), the worktrees
    preserved by :func:`_reclaim_worktree` when a dirty branch is reused, and
    the leftovers of an engine crash or a failed removal. ``active_branches``
    protects worktrees that the store still reports as running.
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
