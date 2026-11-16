"""Execution of claimed runs.

The dispatcher takes one claimed run to completion:

1. Prepare the workspace (clone/fetch/checkout).
2. Render the prompt into the per-run directory.
3. Run the agent through the runner abstraction.
4. Verify the outcome against external state and apply the OpenProject status
   transition plus engine comment.
5. Record the run in the store and write ``meta.json``.

Implement runs (M4) verify an open PR for the branch. The PR is
 authoritative, exactly like a merged PR for a review: a run that was killed
 or timed out after opening the PR is not retried, and a resume run whose PR
 is already open does not start the agent at all. Review runs (M5)
first require an open PR (otherwise the work package goes to On Hold with an
engine comment and the reviewer is not started), then verify that the PR was
merged (approved) or that the reviewer reported ``RESULT: REJECTED`` together
with a comment the reviewer user posted during the run (rejected).

Fix runs (M6) are implementer runs for a Rejected work package. Their prompt
carries the latest reviewer feedback and later human comments (selected from
the work package history), and the fix pass number equals the number of
status changes to Rejected. When a review would produce a rejection beyond
``MAX_REJECTIONS`` the work package moves to On Hold instead and the engine
posts a comment about the cap.

Crash handling: a failed run increments the work package's crash counter.
When the counter exceeds ``MAX_CRASH_RETRIES`` the work package moves to On
Hold with an engine comment (escalation). Otherwise it stays In Progress
(implement/fix) or In Testing (review) and a resume/review run may be
dispatched on a later poll (M7).

Resume runs (M7) are implementer runs for a work package left In Progress by
an interrupted or retryable failed run; their prompt is ``resume.txt.j2`` and
they use the same external verification as implement and fix runs.

Checkpoint runs (M8) are the deliberate stops of a long task:
``RESULT: WAITING_CI``, a clean run that pushed commits, or a clean run that
pushed nothing while a draft PR's checks are still running. They keep the
ticket In Progress, do not touch the crash counter, keep their worktree for
the next run, post a short engine comment, and hold the next resume until the
head commit's CI checks settle (see :mod:`op_agent_engine.checkpoints`).
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from op_agent_engine.checkpoints import resume_ready_at
from op_agent_engine.config import (
    EngineSettings,
    ProjectConfig,
    WorkflowConfig,
)
from op_agent_engine.github import ChecksSummary, GitHubError, PullRequest, head_advanced
from op_agent_engine.hosting import build_hosting_client
from op_agent_engine.logging import get_logger, redact_mapping
from op_agent_engine.openproject.activities import (
    author_id as activity_author_id,
)
from op_agent_engine.openproject.activities import (
    comment_text as activity_comment_text,
)
from op_agent_engine.openproject.activities import (
    count_status_changes,
    unheld,
)
from op_agent_engine.openproject.activities import (
    created_at as activity_created_at,
)
from op_agent_engine.openproject.client import (
    OpenProjectClient,
    OpenProjectError,
    OpenProjectHTTPError,
    OpenProjectNotFoundError,
)
from op_agent_engine.openproject.resolve import OpenProjectResolution
from op_agent_engine.openproject.workpackage import WorkPackage, parse_work_package
from op_agent_engine.outcomes import (
    NO_RESULT_LINE,
    NO_RESULT_LINE_NOTE,
    OutcomeDecision,
    already_open_pr_comment,
    bound_comment,
    checkpoint_comment,
    checkpoint_wait_cap_comment,
    decide_implementer_outcome,
    decide_reviewer_outcome,
    escalation_comment,
    is_rejection,
    no_pr_comment,
    read_tail,
    rejection_cap_comment,
    rejection_is_outstanding,
    waiting_ci_detail,
    waiting_ci_hold_comment,
)
from op_agent_engine.prompts import FixContext, PromptRenderer, select_fix_context, with_standard
from op_agent_engine.runners.base import OnSpawned, RunRequest, RunResult
from op_agent_engine.runners.generic import GenericRunner
from op_agent_engine.scheduler import ClaimedRun
from op_agent_engine.store import (
    CHECKPOINT_OUTCOMES,
    FAILED,
    SUCCEEDED,
    ResumeHold,
    Store,
)
from op_agent_engine.workspace import (
    Workspace,
    WorkspaceError,
    WorktreeArchive,
    archive_failed_worktree,
    branch_snapshot,
    capture_worktree_state,
    cleanup_workspace,
    prepare_workspace,
)

logger = get_logger(__name__)

#: Run kinds the dispatcher executes.
EXECUTED_KINDS = frozenset({"implement", "fix", "resume", "review"})

#: Longest excerpt of the previous run's ticket report in a resume prompt.
MAX_CONTINUATION_REPORT_CHARS = 4000

#: The status a run was claimed in; transitions must start from there.
EXPECTED_STATUS_KEY_BY_KIND = {
    "implement": "in_progress",
    "fix": "in_progress",
    "resume": "in_progress",
    "review": "in_testing",
}


class TransitionAborted(Exception):
    """The work package left the expected status; the transition was not applied."""


class GitHubLike(Protocol):
    async def find_pr(
        self, repo: str, branch: str, *, state: str = "open"
    ) -> PullRequest | None: ...

    async def commit_checks(self, repo: str, sha: str) -> ChecksSummary: ...


class RunnerLike(Protocol):
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
    ) -> RunRequest: ...

    async def run(
        self,
        request: RunRequest,
        *,
        on_spawned: OnSpawned | None = None,
    ) -> RunResult: ...


class Dispatcher:
    """Executes claimed runs end to end (M4: implement, M5: review)."""

    def __init__(
        self,
        *,
        config: WorkflowConfig,
        resolution: OpenProjectResolution,
        store: Store,
        client: OpenProjectClient,
        settings: EngineSettings,
        gh: GitHubLike | None = None,
        runner: RunnerLike | None = None,
        prompts: PromptRenderer | None = None,
    ) -> None:
        self._config = config
        self._resolution = resolution
        self._store = store
        self._client = client
        self._settings = settings
        self._gh: GitHubLike = (
            gh if gh is not None else build_hosting_client(settings)
        )
        self._runner = runner if runner is not None else GenericRunner(config)
        self._prompts = (
            prompts
            if prompts is not None
            else PromptRenderer(self._resolve_template_dir())
        )
        # Two concurrent runs for one agent share a clone. Serialize workspace
        # preparation so fetch/worktree operations cannot race; the agents
        # themselves then run in separate worktrees.
        self._workspace_locks: dict[str, asyncio.Lock] = {}
        self._active_workspaces: dict[int, Workspace] = {}
        self._active_requests: dict[int, RunRequest] = {}
        self._active_claims: dict[int, tuple[ClaimedRun, datetime]] = {}

    # -- main entry point ---------------------------------------------------

    async def execute(self, claimed: ClaimedRun) -> None:
        """Execute one claimed run; record the outcome in the store.

        Engagement transition/comment failures finalize the run immediately.
        Earlier infrastructure failures may still require startup recovery.
        """
        item = claimed.item
        wp = item.work_package
        agent_key = item.agent_key
        log_extra = {
            "run_id": claimed.run_id,
            "work_package_id": wp.id,
            "agent": agent_key,
            "kind": claimed.kind,
        }

        if claimed.kind not in EXECUTED_KINDS:
            logger.warning(
                "run kind %r is not supported by this engine version; "
                "leaving the run active",
                claimed.kind,
                extra=log_extra,
            )
            return

        if claimed.run_id is None:
            logger.info(
                "[dry-run] would spawn %s run for %s (branch %s)",
                claimed.kind,
                wp.display_id,
                claimed.branch,
                extra={**log_extra, "dry_run": True},
            )
            return

        run_id = claimed.run_id
        run_dir = claimed.run_dir or (self._store.runs_dir / str(run_id))
        run_dir.mkdir(parents=True, exist_ok=True)
        started_at = datetime.now(UTC)
        logger.info("starting %s run", claimed.kind, extra=log_extra)

        self._active_claims[run_id] = (claimed, started_at)
        try:
            if claimed.kind == "review":
                await self._execute_review(claimed, run_id, run_dir, started_at)
            else:
                await self._execute_implement(claimed, run_id, run_dir, started_at)
        except asyncio.CancelledError:
            if self._settings.drain_seconds:
                await self._checkpoint_interruption(claimed, run_dir, started_at)
            raise
        finally:
            self._active_workspaces.pop(run_id, None)
            self._active_requests.pop(run_id, None)
            self._active_claims.pop(run_id, None)

    async def checkpoint_before_stop(self, run_id: int) -> None:
        """Best-effort, non-destructive evidence before sending TERM.

        The agent may still be writing. Cancellation captures a final snapshot
        after process termination; this preliminary one survives a hard kill.
        """
        active = self._active_claims.get(run_id)
        if active is None:
            return
        claimed, started_at = active
        run_dir = claimed.run_dir or self._store.runs_dir / str(run_id)
        self._write_meta(
            claimed, self._active_requests.get(run_id), None,
            state="interrupted", outcome="drained", transition=None, pr=None,
            started_at=started_at,
        )
        workspace = self._active_workspaces.get(run_id)
        if workspace is not None:
            try:
                async with asyncio.timeout(10):
                    capture = await capture_worktree_state(
                        workspace, run_dir / "worktree-preliminary"
                    )
                    current = self._store.get_run(run_id)
                    if current is not None and current.state == "running":
                        self._write_meta(
                            claimed, self._active_requests.get(run_id), None,
                            state="interrupted", outcome="drained", transition=None, pr=None,
                            started_at=started_at,
                            archive=WorktreeArchive(
                                artefact=capture.path, kept=True, errors=capture.errors,
                            ),
                        )
            except Exception:
                logger.exception("pre-stop checkpoint failed for run %s", run_id)

    async def _checkpoint_interruption(
        self, claimed: ClaimedRun, run_dir: Path, started_at: datetime
    ) -> None:
        """Snapshot after the runner stops writing; never spend a crash retry.

        Each external operation has a deadline. Evidence failures must not leave
        an active row, and a failed comment is not retried (no duplicate posts).
        """
        assert claimed.run_id is not None
        run_id = claimed.run_id
        archive = None
        pr = None
        errors: list[str] = []
        for name in ("stdout.log", "stderr.log"):
            try:
                with (run_dir / name).open("rb") as log:
                    log.seek(0, 2)
                    log.seek(max(0, log.tell() - 65536))
                    (run_dir / f"{name}.tail").write_bytes(log.read(65536))
            except FileNotFoundError:
                pass
            except OSError as error:
                errors.append(f"log tail: {error}")
        workspace = self._active_workspaces.get(run_id)
        if workspace is not None:
            try:
                async with asyncio.timeout(30):
                    archive = await self._archive_workspace(workspace, run_dir, failed=True)
            except Exception as error:
                errors.append(f"archive: {error}")
        try:
            async with asyncio.timeout(5):
                pr = await self._gh.find_pr(
                    self._repo_github(claimed.item.work_package.project_id), claimed.branch
                )
        except Exception as error:
            errors.append(f"PR lookup: {error}")
        self._store.mark_interrupted(run_id, outcome="drained")
        self._write_meta(
            claimed, self._active_requests.get(run_id), None,
            state="interrupted", outcome="drained", transition=None, pr=pr,
            started_at=started_at, archive=archive,
        )
        meta_path = run_dir / "meta.json"
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        head = meta["branch_head"]
        preliminary = run_dir / "worktree-preliminary"
        if preliminary.is_dir():
            # Keep pre-TERM evidence separate even if final capture failed.
            # Never advertise it as the authoritative post-termination state.
            meta["preliminary_worktree_artefact"] = str(preliminary)
        meta["interruption_errors"] = errors
        meta_path.write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")
        comment = (
            f"ENGINE: run {run_id} interrupted by engine restart at {meta['finished_at']}; "
            f"branch head {head}; artefact at {run_dir}; the next run resumes."
        )
        comment = bound_comment(
            comment, max_bytes=self._settings.comment_max_bytes,
            line_max_bytes=self._settings.comment_line_max_bytes,
        )
        try:
            async with asyncio.timeout(5):
                await self._post_comment(claimed.item.work_package.id, comment)
        except Exception as error:
            errors.append(f"comment: {error}")
            meta_path.write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")

    # -- implement runs (M4) ------------------------------------------------

    async def _execute_implement(
        self,
        claimed: ClaimedRun,
        run_id: int,
        run_dir: Path,
        started_at: datetime,
    ) -> None:
        wp = claimed.item.work_package
        # The open PR, when the pre-run lookup found one. Reused for the resume
        # briefing so one API call covers both purposes.
        open_pr: PullRequest | None = None

        # A resume run whose branch already has an open PR is usually finished
        # work: the previous run was killed or timed out after opening it.
        # Starting the agent again would repeat the work (and can hang again),
        # so the PR is authoritative here just like a merged PR for a review. A
        # draft PR is not authoritative and the agent resumes normally.
        #
        # The exception is an outstanding rejection: when a fix run was killed
        # before it pushed anything, the open PR predates the reviewer's latest
        # verdict and treating it as finished work sends an unchanged branch
        # back to review (WP 144: rejection at 07:40, interrupted fix run at
        # 07:40, resume at 07:43 declared ready for review, same findings
        # rejected again at 07:48). Then the agent must run.
        if claimed.kind == "resume":
            last_review = self._store.last_review_run(wp.id)
            last_code = self._store.last_code_run(wp.id)
            interrupted = self._store.last_interrupted_run(wp.id)
            if (
                interrupted is not None and (last_code is None or interrupted.id > last_code.id)
            ) or rejection_is_outstanding(last_review, last_code):
                # The rejection explains the interrupted fix run: run the
                # agent, do not short-circuit to ready for review.
                logger.info(
                    "resuming %s despite an open PR: interruption evidence or "
                    "outstanding rejection; the last review was "
                    "rejected at %s and the last code run finished at %s",
                    wp.display_id,
                    last_review.finished_at if last_review else None,
                    last_code.finished_at if last_code else None,
                    extra={
                        "run_id": run_id,
                        "work_package_id": wp.id,
                        "agent": claimed.item.agent_key,
                        "kind": claimed.kind,
                        "branch": claimed.branch,
                    },
                )
            else:
                try:
                    open_pr = await self._gh.find_pr(
                        self._repo_github(wp.project_id), claimed.branch
                    )
                except GitHubError as error:
                    # Verification is best-effort here; running the resume is
                    # still correct when GitHub cannot be reached.
                    logger.warning(
                        "could not check for an open PR before resuming %s; "
                        "starting the agent anyway: %s",
                        wp.display_id,
                        error,
                        extra={
                            "run_id": run_id,
                            "work_package_id": wp.id,
                            "agent": claimed.item.agent_key,
                            "kind": claimed.kind,
                            "branch": claimed.branch,
                        },
                    )
                else:
                    if open_pr is not None and not open_pr.is_draft:
                        await self._resume_already_has_pr(
                            claimed, run_id, open_pr, started_at
                        )
                        return

        await self._reset_crash_count_if_unheld(wp)

        # The scheduler only claims a held ticket once its deadline passed, so
        # the hold has done its job: clear it before the run so a failure is
        # not parked behind a stale deadline.
        resume_hold = self._store.resume_hold(wp.id)
        if resume_hold is not None:
            self._store.clear_resume_hold(wp.id)

        workspace = await self._prepare_workspace(
            claimed,
            run_id,
            # A checkpoint keeps its worktree so the next run reuses the warm
            # build caches instead of re-importing the whole project.
            keep_existing_worktree=self._settings.keep_checkpoint_worktrees,
        )
        if workspace is None:
            return

        # A resume prompt carries a brief of the previous run: what it did,
        # what it reported, and whether it ended at a checkpoint. The agent
        # then continues instead of re-deriving the ticket from scratch.
        continuation: dict[str, Any] | None = None
        if claimed.kind == "resume":
            continuation = await self._continuation_context(
                claimed, open_pr=open_pr, resume_hold=resume_hold
            )

        # A fix pass needs the reviewer feedback and the rejection number;
        # both come from the work package history.
        fix_context: FixContext | None = None
        attempt = 1
        if claimed.kind == "fix":
            try:
                activities = await self._fetch_activities(wp.id)
            except OpenProjectError as error:
                await self._fail_before_run(
                    run_id,
                    claimed,
                    detail=f"cannot read work package activities: {error}",
                    outcome="fix_context_error",
                    workspace=workspace,
                )
                return
            attempt = max(
                1, count_status_changes(activities, self._rejected_status_name())
            )
            self._store.set_attempt(run_id, attempt)
            fix_context = select_fix_context(
                activities,
                reviewer_id=self._reviewer_user_id(),
                agent_user_ids=self._agent_user_ids(),
                engine_user_id=self._resolution.engine_user.id,
                author_names=self._author_names(),
            )
            if fix_context.feedback is None:
                await self._fail_before_run(
                    run_id,
                    claimed,
                    detail=(
                        "the work package history has no reviewer feedback "
                        "comment for the fix pass"
                    ),
                    outcome="fix_feedback_missing",
                    workspace=workspace,
                )
                return

        request = self._build_request(
            claimed,
            workspace,
            run_id,
            run_dir,
            self._render_prompt(
                claimed,
                workspace,
                fix_context=fix_context,
                attempt=attempt,
                continuation=continuation,
            ),
        )
        try:
            before_head, _ = await branch_snapshot(workspace)
        except WorkspaceError as error:
            await self._fail_before_run(
                run_id, claimed, detail=f"cannot capture initial head: {error}",
                workspace=workspace,
            )
            return
        try:
            before_pr = await self._gh.find_pr(
                self._repo_github(wp.project_id), workspace.branch
            )
        except GitHubError as error:
            # A missing baseline cannot prove PR progress, but the local
            # branch comparison remains available. Verification after the
            # run is still required, just as before.
            logger.warning("could not capture initial PR head: %s", error)
            before_pr = None
        result = await self._run(request, run_id)

        # External check: open PR for the branch?
        try:
            pr = await self._gh.find_pr(
                self._repo_github(wp.project_id), workspace.branch
            )
            after_head, clean = await branch_snapshot(workspace)
        except (GitHubError, WorkspaceError) as error:
            archive = await self._archive_workspace(
                workspace, run_dir, failed=True
            )
            await self._fail_after_run(
                run_id, claimed, request, result,
                detail=f"PR verification failed: {error}",
                archive=archive,
            )
            return

        await self._finish_run(
            run_id,
            claimed,
            request,
            result,
            pr,
            started_at,
            workspace=workspace,
            run_dir=run_dir,
            heads={
                "branch_before": before_head,
                "branch_after": after_head,
                "pr_before": before_pr.head_oid if before_pr else None,
                "pr_after": pr.head_oid if pr else None,
                "worktree_clean": clean,
            },
        )

    # -- review runs (M5) ---------------------------------------------------

    async def _execute_review(
        self,
        claimed: ClaimedRun,
        run_id: int,
        run_dir: Path,
        started_at: datetime,
    ) -> None:
        wp = claimed.item.work_package
        repo = self._repo_github(wp.project_id)

        # A review needs a PR to look at. Without one the
        # work package goes to On Hold and the reviewer is not started.
        try:
            pr = await self._gh.find_pr(repo, claimed.branch)
        except GitHubError as error:
            await self._fail_after_run(
                run_id,
                claimed,
                None,
                None,
                detail=f"PR lookup failed: {error}",
            )
            return
        if pr is None:
            # Recovery can find a review that merged just before the engine
            # crashed: the PR is no longer open, but the merge is
            # authoritative and must close the work package without running
            # the reviewer again.
            try:
                merged = await self._gh.find_pr(
                    repo, claimed.branch, state="merged"
                )
            except GitHubError as error:
                await self._fail_after_run(
                    run_id,
                    claimed,
                    None,
                    None,
                    detail=f"merged PR lookup failed: {error}",
                )
                return
            if merged is not None:
                await self._review_already_merged(
                    claimed, run_id, merged, started_at
                )
                return
            await self._review_without_pr(claimed, run_id, started_at)
            return

        workspace = await self._prepare_workspace(claimed, run_id)
        if workspace is None:
            return

        request = self._build_request(
            claimed,
            workspace,
            run_id,
            run_dir,
            self._render_prompt(claimed, workspace, pr=pr),
        )
        result = await self._run(request, run_id)

        # Approved? A merged PR is authoritative.
        try:
            merged = await self._gh.find_pr(repo, workspace.branch, state="merged")
        except GitHubError as error:
            archive = await self._archive_workspace(
                workspace, run_dir, failed=True
            )
            await self._fail_after_run(
                run_id, claimed, request, result,
                detail=f"merged PR verification failed: {error}",
                archive=archive,
            )
            return

        # Rejected? The rejection is only accepted when the reviewer posted a
        # comment during the run (otherwise it is not verifiable).
        rejection_comment_found = False
        if merged is None and is_rejection(result.result_line):
            try:
                rejection_comment_found = await self._reviewer_commented_since(
                    wp.id, claimed.item.agent_key, started_at
                )
            except OpenProjectError as error:
                archive = await self._archive_workspace(
                    workspace, run_dir, failed=True
                )
                self._write_meta(
                    claimed, request, result, state="running", outcome="error",
                    transition=None, pr=None, started_at=started_at,
                    archive=archive,
                )
                logger.error(
                    "could not read the reviewer comments for work package %s; "
                    "run %d left active: %s",
                    wp.display_id,
                    run_id,
                    error,
                    extra={"run_id": run_id, "work_package_id": wp.id},
                )
                raise

        await self._finish_review_run(
            run_id,
            claimed,
            request,
            result,
            merged,
            rejection_comment_found=rejection_comment_found,
            started_at=started_at,
            workspace=workspace,
            run_dir=run_dir,
            reviewed_pr=pr,
        )

    # -- outcome handling ----------------------------------------------------

    async def _finish_run(
        self,
        run_id: int,
        claimed: ClaimedRun,
        request: RunRequest,
        result: RunResult,
        pr: PullRequest | None,
        started_at: datetime,
        *,
        workspace: Workspace | None = None,
        run_dir: Path | None = None,
        heads: dict[str, Any] | None = None,
    ) -> None:
        heads = heads or {}
        moved = head_advanced(heads.get("branch_before"), heads.get("branch_after"))
        pr_moved = pr is not None and head_advanced(
            heads.get("pr_before"), heads.get("pr_after")
        )
        decision = decide_implementer_outcome(
            exit_code=result.exit_code,
            timed_out=result.timed_out,
            result_line=result.result_line,
            pr_found=pr is not None and not pr.is_draft,
            silent=result.is_silent(),
            progress=bool(heads.get("worktree_clean")) and (moved or pr_moved),
            draft_pr_open=pr is not None and pr.is_draft,
        )
        decision, checkpoint_kept = await self._apply_checkpoint_policy(
            claimed, decision, pr=pr, heads=heads, result=result
        )
        archive = await self._finish_workspace(
            claimed,
            workspace,
            run_dir,
            decision=decision,
            keep=checkpoint_kept,
        )
        await self._apply_decision(
            run_id, claimed, request, result, decision, pr=pr,
            started_at=started_at, archive=archive, heads=heads,
            checkpoint_kept=checkpoint_kept,
        )

    async def _apply_checkpoint_policy(
        self,
        claimed: ClaimedRun,
        decision: OutcomeDecision,
        *,
        pr: PullRequest | None,
        heads: dict[str, Any],
        result: RunResult,
        keep_worktree: bool = True,
    ) -> tuple[OutcomeDecision, bool]:
        """Turn a checkpoint into a held/escalated decision and its comment.

        Returns the decision to record plus whether the worktree must be kept
        for the next run. The checkpoint never consumes the crash retry
        budget: the agent stopped deliberately, so the run itself is healthy.
        A clean run that pushed nothing is only a checkpoint while its CI is
        still running; otherwise it stays an ordinary ``no_pr`` failure.
        """
        wp = claimed.item.work_package
        if decision.outcome not in CHECKPOINT_OUTCOMES:
            if decision.run_state == SUCCEEDED:
                # A delivered run makes any leftover hold meaningless.
                self._store.clear_resume_hold(wp.id)
            return decision, False

        head_sha = heads.get("branch_after") or heads.get("pr_after")
        checks = await self._commit_checks(wp, head_sha)
        if (
            decision.outcome == "checkpoint_no_progress"
            and (checks is None or not checks.is_pending)
        ):
            # Nothing is running, so the agent did not wait for anything:
            # scoring it as a failure keeps the old, honest escalation.
            logger.info(
                "a draft PR with no new commits is not a checkpoint for %s: %s",
                wp.display_id,
                checks.describe() if checks is not None else "checks unknown",
                extra={
                    "work_package_id": wp.id,
                    "checks": checks.describe() if checks is not None else None,
                },
            )
            return (
                OutcomeDecision(run_state=FAILED, outcome="no_pr", crash_retry=True),
                False,
            )

        streak = self._store.checkpoint_streak(wp.id) + 1
        checks_detail = checks.describe() if checks is not None else None
        if streak > self._settings.max_checkpoint_waits:
            self._store.clear_resume_hold(wp.id)
            logger.warning(
                "checkpoint wait cap reached for %s (%d > %d); escalating",
                wp.display_id,
                streak,
                self._settings.max_checkpoint_waits,
                extra={"work_package_id": wp.id, "checkpoint_streak": streak},
            )
            return (
                replace(
                    decision,
                    outcome="checkpoint_wait_cap_reached",
                    transition="on_hold",
                    comment=checkpoint_wait_cap_comment(
                        waits=streak, head_sha=head_sha, checks_detail=checks_detail
                    ),
                ),
                False,
            )

        if self._store.crash_count(wp.id):
            # A healthy checkpoint is evidence the ticket is not crash-looping.
            self._store.reset_crash_count(wp.id)

        detail = waiting_ci_detail(result.result_line)
        # An explicit WAITING_CI is the agent telling us it is waiting, so it
        # is held even when the checks cannot be read: the gate then retries on
        # its interval instead of starting an agent run every poll. Without a
        # head there is nothing to re-check, so that case resumes at once.
        waiting = checks.is_pending if checks is not None else (
            decision.outcome == "waiting_ci" and head_sha is not None
        )
        if waiting:
            checks_detail = (
                checks.describe() if checks is not None
                else "CI state unavailable; the engine re-checks it"
            )
            ready_at = resume_ready_at(
                now=datetime.now(UTC),
                wait_seconds=self._settings.checkpoint_wait_seconds,
            )
            self._store.set_resume_hold(
                wp.id,
                repo=self._repo_github(wp.project_id),
                head_sha=head_sha,
                ready_at=ready_at,
                reason=checks_detail,
                attempts=streak,
            )
            comment = waiting_ci_hold_comment(
                head_sha=head_sha,
                checks_detail=checks_detail,
                ready_at=datetime.fromisoformat(ready_at),
                detail=detail,
            )
        else:
            self._store.clear_resume_hold(wp.id)
            comment = checkpoint_comment(
                outcome=decision.outcome,
                head_sha=head_sha,
                pr_url=pr.url if pr is not None else None,
                detail=detail,
            )
        return replace(decision, comment=comment), keep_worktree

    async def _commit_checks(
        self, wp: WorkPackage, head_sha: str | None
    ) -> ChecksSummary | None:
        """The CI state of the head commit, or None when it cannot be read."""
        if not head_sha:
            return None
        try:
            async with asyncio.timeout(60):
                return await self._gh.commit_checks(
                    self._repo_github(wp.project_id), head_sha
                )
        except (GitHubError, TimeoutError) as error:
            logger.warning(
                "could not read the CI state of %s for %s: %s",
                head_sha[:12],
                wp.display_id,
                error,
                extra={"work_package_id": wp.id, "head_sha": head_sha},
            )
            return None

    async def _finish_review_run(
        self,
        run_id: int,
        claimed: ClaimedRun,
        request: RunRequest,
        result: RunResult,
        merged: PullRequest | None,
        *,
        rejection_comment_found: bool,
        started_at: datetime,
        workspace: Workspace | None = None,
        run_dir: Path | None = None,
        reviewed_pr: PullRequest | None = None,
    ) -> None:
        decision = decide_reviewer_outcome(
            exit_code=result.exit_code,
            timed_out=result.timed_out,
            result_line=result.result_line,
            pr_merged=merged is not None,
            rejection_comment_found=rejection_comment_found,
        )
        # A reviewer waiting for the PR's checks is a checkpoint too: hold the
        # next review until they settle instead of re-running it every poll.
        # The review worktree is read-only, so it is not kept.
        decision, checkpoint_kept = await self._apply_checkpoint_policy(
            claimed,
            decision,
            pr=reviewed_pr,
            heads={"pr_after": reviewed_pr.head_oid if reviewed_pr else None},
            result=result,
            keep_worktree=False,
        )
        archive = await self._finish_workspace(
            claimed,
            workspace,
            run_dir,
            decision=decision,
            keep=checkpoint_kept,
        )
        if decision.transition == "rejected":
            decision = await self._apply_rejection_cap(
                claimed, run_id, decision, started_at=started_at, archive=archive
            )
        await self._apply_decision(
            run_id,
            claimed,
            request,
            result,
            decision,
            pr=merged,
            started_at=started_at,
            archive=archive,
        )

    async def _apply_rejection_cap(
        self,
        claimed: ClaimedRun,
        run_id: int,
        decision: OutcomeDecision,
        *,
        started_at: datetime,
        archive: WorktreeArchive | None = None,
    ) -> OutcomeDecision:
        """Escalate to On Hold when the next rejection exceeds the cap.

        The rejection count is derived from the work package history (status
        changes to Rejected). The current rejection has not been applied yet,
        so ``previous + 1`` is the number this review would produce. With the
        default ``MAX_REJECTIONS=3`` rejection 3 is allowed and rejection 4
        moves the work package to On Hold.
        """
        wp = claimed.item.work_package
        try:
            activities = await self._fetch_activities(wp.id)
        except OpenProjectError as error:
            self._write_meta(
                claimed, None, None, state="running", outcome="error",
                transition=None, pr=None, started_at=started_at,
                archive=archive,
            )
            logger.error(
                "could not read the work package history for rejection "
                "counting (%s); run %d left active: %s",
                wp.display_id,
                run_id,
                error,
                extra={"run_id": run_id, "work_package_id": wp.id},
            )
            raise

        previous = count_status_changes(activities, self._rejected_status_name())
        rejection_number = previous + 1
        if rejection_number <= self._settings.max_rejections:
            logger.info(
                "rejection %d/%d allowed for work package %s",
                rejection_number,
                self._settings.max_rejections,
                wp.display_id,
                extra={
                    "run_id": run_id,
                    "work_package_id": wp.id,
                    "rejection_number": rejection_number,
                },
            )
            return decision

        logger.warning(
            "rejection %d exceeds MAX_REJECTIONS=%d for work package %s; "
            "escalating to On Hold",
            rejection_number,
            self._settings.max_rejections,
            wp.display_id,
            extra={
                "run_id": run_id,
                "work_package_id": wp.id,
                "rejection_number": rejection_number,
            },
        )
        return OutcomeDecision(
            run_state=decision.run_state,
            outcome="rejection_cap_reached",
            transition="on_hold",
            comment=rejection_cap_comment(
                rejection_number=rejection_number,
                max_rejections=self._settings.max_rejections,
            ),
        )

    async def _apply_decision(
        self,
        run_id: int,
        claimed: ClaimedRun,
        request: RunRequest,
        result: RunResult,
        decision: OutcomeDecision,
        *,
        pr: PullRequest | None,
        started_at: datetime,
        archive: WorktreeArchive | None = None,
        heads: dict[str, Any] | None = None,
        checkpoint_kept: bool = False,
    ) -> None:
        wp = claimed.item.work_package
        expected_status_key = EXPECTED_STATUS_KEY_BY_KIND[claimed.kind]
        state, outcome, transition, comment = (
            decision.run_state,
            decision.outcome,
            decision.transition,
            decision.comment,
        )
        log_extra = {
            "run_id": run_id,
            "work_package_id": wp.id,
            "agent": claimed.item.agent_key,
            "kind": claimed.kind,
            "pr": pr.url if pr is not None else None,
        }

        if decision.crash_retry:
            crash_count = self._store.increment_crash_count(wp.id)
            if crash_count > self._settings.max_crash_retries:
                tail = (
                    read_tail(result.stdout_path)
                    or read_tail(result.stderr_path)
                )
                comment = escalation_comment(
                    max_bytes=self._settings.comment_max_bytes,
                    line_max_bytes=self._settings.comment_line_max_bytes,
                    outcome=outcome,
                    exit_code=result.exit_code,
                    tail_lines=tail,
                    artefact_path=(
                        str(archive.artefact)
                        if archive is not None and archive.artefact is not None
                        else None
                    ),
                    note=NO_RESULT_LINE_NOTE if outcome == NO_RESULT_LINE else None,
                )
                transition = "on_hold"
                logger.warning(
                    "crash retry cap reached for work package %s (%d > %d); "
                    "escalating to On Hold",
                    wp.display_id,
                    crash_count,
                    self._settings.max_crash_retries,
                    extra={**log_extra, "crash_count": crash_count},
                )
            else:
                logger.warning(
                    "run failed (%s); crash retry %d/%d allowed; work package "
                    "stays %s and is resumed by a later poll",
                    outcome,
                    crash_count,
                    self._settings.max_crash_retries,
                    expected_status_key,
                    extra={**log_extra, "crash_count": crash_count},
                )

        engagement_error: str | None = None
        recorded_decision = {
            "run_state": state, "outcome": outcome,
            "transition": transition, "comment": comment,
            "crash_retry": decision.crash_retry,
        }
        try:
            if transition is not None:
                await self._transition(
                    wp.id, transition, expected_status_key=expected_status_key
                )
            if comment is not None:
                await self._post_comment(wp.id, comment)
        except TransitionAborted as error:
            state, outcome = FAILED, f"transition_aborted: {error}"
            transition = None
            logger.error(
                "transition aborted for run %d: %s",
                run_id,
                error,
                extra=log_extra,
            )
        except OpenProjectError as error:
            # The process has exited. Release its slot even if the transition
            # succeeded but its explanatory comment did not. Unchanged active
            # tickets are eligible next poll; On Hold still needs a human.
            engagement_error = str(error)
            state, outcome = FAILED, "engagement_error"
            logger.error(
                "OpenProject error after run %d finished; run finalized: %s",
                run_id,
                error,
                extra=log_extra,
            )

        self._store.finish_run(
            run_id,
            state=state,
            outcome=outcome,
            exit_code=result.exit_code,
            result_line=result.result_line,
        )
        self._write_meta(
            claimed, request, result, state=state, outcome=outcome,
            transition=transition, pr=pr, started_at=started_at,
            archive=archive, heads=heads, decision=recorded_decision,
            engagement_error=engagement_error, checkpoint_kept=checkpoint_kept,
        )
        logger.info(
            "run finished: state=%s outcome=%s transition=%s",
            state,
            outcome,
            transition,
            extra={**log_extra, "state": state, "outcome": outcome},
        )

    async def _resume_already_has_pr(
        self,
        claimed: ClaimedRun,
        run_id: int,
        pr: PullRequest,
        started_at: datetime,
    ) -> None:
        """Finish a resume run without an agent because its PR is already open."""
        wp = claimed.item.work_package
        log_extra = {
            "run_id": run_id,
            "work_package_id": wp.id,
            "agent": claimed.item.agent_key,
            "kind": claimed.kind,
            "pr": pr.url,
        }
        transition: str | None = "ready_for_review"
        try:
            await self._transition(
                wp.id, "ready_for_review", expected_status_key="in_progress"
            )
            await self._post_comment(
                wp.id, already_open_pr_comment(claimed.branch, pr.url)
            )
        except TransitionAborted as error:
            state, outcome, transition = (
                FAILED,
                f"transition_aborted: {error}",
                None,
            )
            logger.error(
                "transition aborted for run %d: %s", run_id, error, extra=log_extra
            )
        except OpenProjectError as error:
            self._store.finish_run(run_id, state=FAILED, outcome="engagement_error")
            self._write_meta(
                claimed, None, None, state=FAILED, outcome="engagement_error",
                transition=transition, pr=pr, started_at=started_at,
                engagement_error=str(error),
                decision={
                    "run_state": SUCCEEDED, "outcome": "ready_for_review_existing_pr",
                    "transition": transition,
                },
            )
            logger.error("engagement failed for run %d: %s", run_id, error, extra=log_extra)
            return
        else:
            state, outcome = SUCCEEDED, "ready_for_review_existing_pr"
            logger.info(
                "branch %s already has an open PR; work package %s moved to "
                "Ready for Review without a resume run",
                claimed.branch,
                wp.display_id,
                extra=log_extra,
            )

        self._store.finish_run(run_id, state=state, outcome=outcome)
        self._write_meta(
            claimed, None, None, state=state, outcome=outcome,
            transition=transition, pr=pr, started_at=started_at,
        )

    async def _review_already_merged(
        self,
        claimed: ClaimedRun,
        run_id: int,
        merged: PullRequest,
        started_at: datetime,
    ) -> None:
        """Close a review whose PR was already merged before it started."""
        wp = claimed.item.work_package
        log_extra = {
            "run_id": run_id,
            "work_package_id": wp.id,
            "agent": claimed.item.agent_key,
            "kind": claimed.kind,
            "pr": merged.url,
        }
        transition: str | None = "closed"
        try:
            await self._transition(
                wp.id, "closed", expected_status_key="in_testing"
            )
        except TransitionAborted as error:
            state, outcome, transition = (
                FAILED,
                f"transition_aborted: {error}",
                None,
            )
            logger.error(
                "transition aborted for run %d: %s", run_id, error, extra=log_extra
            )
        except OpenProjectError as error:
            self._write_meta(
                claimed, None, None, state="running", outcome="error",
                transition=transition, pr=merged, started_at=started_at,
            )
            logger.error(
                "OpenProject error closing an already merged PR for run %d; "
                "run left active: %s",
                run_id,
                error,
                extra=log_extra,
            )
            raise
        else:
            state, outcome = SUCCEEDED, "approved"
            logger.info(
                "PR for branch %s was already merged; work package %s closed "
                "without a new review run",
                claimed.branch,
                wp.display_id,
                extra=log_extra,
            )

        self._store.finish_run(run_id, state=state, outcome=outcome)
        self._write_meta(
            claimed, None, None, state=state, outcome=outcome,
            transition=transition, pr=merged, started_at=started_at,
        )

    async def _review_without_pr(
        self, claimed: ClaimedRun, run_id: int, started_at: datetime
    ) -> None:
        """``Ready for Review`` without a PR: On Hold plus an engine comment.

        The reviewer is not started; the work package was
        already claimed, so it is In Testing when the comment is posted.
        """
        wp = claimed.item.work_package
        log_extra = {
            "run_id": run_id,
            "work_package_id": wp.id,
            "agent": claimed.item.agent_key,
            "kind": claimed.kind,
            "transition": "on_hold",
        }
        transition: str | None = "on_hold"
        try:
            await self._transition(
                wp.id, "on_hold", expected_status_key="in_testing"
            )
            await self._post_comment(wp.id, no_pr_comment(claimed.branch))
        except TransitionAborted as error:
            transition = None
            logger.error(
                "transition aborted for run %d: %s", run_id, error, extra=log_extra
            )
        except OpenProjectError as error:
            self._store.finish_run(run_id, state=FAILED, outcome="engagement_error")
            self._write_meta(
                claimed, None, None, state=FAILED, outcome="engagement_error",
                transition=transition, pr=None, started_at=started_at,
                engagement_error=str(error),
                decision={
                    "run_state": FAILED, "outcome": "no_pr_for_branch",
                    "transition": transition,
                },
            )
            logger.error("engagement failed for run %d: %s", run_id, error, extra=log_extra)
            return

        outcome = (
            "no_pr_for_branch" if transition is not None else "transition_aborted"
        )
        self._store.finish_run(run_id, state=FAILED, outcome=outcome)
        self._write_meta(
            claimed, None, None, state=FAILED, outcome=outcome,
            transition=transition, pr=None, started_at=started_at,
        )
        logger.warning(
            "no PR for branch %s; work package %s moved to On Hold without a "
            "review run",
            claimed.branch,
            wp.display_id,
            extra=log_extra,
        )

    async def _fail_before_run(
        self,
        run_id: int,
        claimed: ClaimedRun,
        detail: str,
        *,
        outcome: str = "workspace_error",
        workspace: Workspace | None = None,
    ) -> None:
        """A workspace or prompt-context failure before the process started.

        A workspace that was already prepared but never handed to an agent is
        removed: it is pristine, so there is nothing to preserve.
        """
        if workspace is not None:
            await cleanup_workspace(workspace)
        wp = claimed.item.work_package
        engagement_error = await self._crash_escalate(
            wp.id,
            outcome=outcome,
            detail=detail,
            exit_code=None,
            tail_lines=None,
            run_id=run_id,
            expected_status_key=EXPECTED_STATUS_KEY_BY_KIND[claimed.kind],
        )
        self._store.finish_run(run_id, state=FAILED, outcome=outcome)
        self._write_meta(
            claimed, None, None, state=FAILED, outcome=outcome,
            transition=None, pr=None, started_at=datetime.now(UTC),
            engagement_error=engagement_error,
            decision={"run_state": FAILED, "outcome": outcome, "detail": detail},
        )

    async def _fail_after_run(
        self,
        run_id: int,
        claimed: ClaimedRun,
        request: RunRequest | None,
        result: RunResult | None,
        *,
        detail: str,
        archive: WorktreeArchive | None = None,
    ) -> None:
        """A failure after the process ran (e.g. PR verification error)."""
        wp = claimed.item.work_package
        tail: list[str] | None = None
        if result is not None:
            tail = read_tail(result.stdout_path) or read_tail(result.stderr_path)
        engagement_error = await self._crash_escalate(
            wp.id,
            outcome="pr_verification_error",
            detail=detail,
            exit_code=result.exit_code if result is not None else None,
            tail_lines=tail,
            run_id=run_id,
            expected_status_key=EXPECTED_STATUS_KEY_BY_KIND[claimed.kind],
            artefact_path=(
                str(archive.artefact)
                if archive is not None and archive.artefact is not None
                else None
            ),
        )
        self._store.finish_run(
            run_id,
            state=FAILED,
            outcome="pr_verification_error",
            exit_code=result.exit_code if result is not None else None,
            result_line=result.result_line if result is not None else None,
        )
        self._write_meta(
            claimed, request, result, state=FAILED,
            outcome="pr_verification_error", transition=None, pr=None,
            started_at=datetime.now(UTC), archive=archive,
            engagement_error=engagement_error,
            decision={
                "run_state": FAILED, "outcome": "pr_verification_error", "detail": detail,
            },
        )

    async def _crash_escalate(
        self,
        work_package_id: int,
        *,
        outcome: str,
        detail: str,
        exit_code: int | None,
        tail_lines: list[str] | None,
        run_id: int,
        expected_status_key: str,
        artefact_path: str | None = None,
    ) -> str | None:
        """Escalate at the cap; return API failure detail for run forensics."""
        crash_count = self._store.increment_crash_count(work_package_id)
        if crash_count <= self._settings.max_crash_retries:
            logger.warning(
                "run %d failed before/without a usable result (%s); crash "
                "retry %d/%d allowed",
                run_id,
                outcome,
                crash_count,
                self._settings.max_crash_retries,
                extra={
                    "run_id": run_id,
                    "work_package_id": work_package_id,
                    "crash_count": crash_count,
                },
            )
            return None
        comment = escalation_comment(
            max_bytes=self._settings.comment_max_bytes,
            line_max_bytes=self._settings.comment_line_max_bytes,
            outcome=outcome,
            exit_code=exit_code,
            tail_lines=tail_lines,
            detail=detail,
            artefact_path=artefact_path,
        )
        try:
            await self._transition(
                work_package_id, "on_hold", expected_status_key=expected_status_key
            )
            await self._post_comment(work_package_id, comment)
        except OpenProjectError as error:
            logger.error("engagement failed for run %d: %s", run_id, error)
            return str(error)
        return None

    # -- OpenProject actions --------------------------------------------------

    async def _transition(
        self,
        work_package_id: int,
        status_key: str,
        *,
        expected_status_key: str,
    ) -> None:
        """Move the work package to *status_key* (status only, no assignee).

        Re-reads the work package first: if its status is no longer
        ``expected_status_key`` a human (or another actor) changed it and the
        transition is aborted. On HTTP 409 the patch is retried once with the
        fresh lockVersion (same protocol as the claim).
        """
        expected_id = self._resolution.statuses[expected_status_key].id
        target_id = self._resolution.statuses[status_key].id
        path = f"/api/v3/work_packages/{work_package_id}"

        fresh = await self._fetch_work_package(work_package_id)
        if fresh is None:
            raise TransitionAborted(f"work package {work_package_id} no longer exists")
        if fresh.status_id != expected_id:
            raise TransitionAborted(
                f"work package {work_package_id} status changed from "
                f"'{expected_status_key}' ({expected_id}) to '{fresh.status_name}' "
                f"({fresh.status_id}); not overriding"
            )
        if fresh.lock_version is None:
            raise TransitionAborted(
                f"work package {work_package_id} payload has no lockVersion"
            )

        for attempt in (1, 2):
            body = {
                "lockVersion": fresh.lock_version,
                "_links": {"status": {"href": f"/api/v3/statuses/{target_id}"}},
            }
            try:
                await self._client.patch_json(path, json_body=body)
                logger.info(
                    "work package %s -> %s",
                    work_package_id,
                    status_key,
                    extra={
                        "work_package_id": work_package_id,
                        "status": status_key,
                    },
                )
                return
            except OpenProjectHTTPError as error:
                if error.status_code != 409 or attempt == 2:
                    raise
                logger.warning(
                    "lock conflict transitioning work package %s; retrying once",
                    work_package_id,
                    extra={"work_package_id": work_package_id},
                )
                fresh = await self._fetch_work_package(work_package_id)
                if fresh is None:
                    raise TransitionAborted(
                        f"work package {work_package_id} no longer exists after "
                        "a conflict"
                    ) from None
                if fresh.status_id != expected_id:
                    raise TransitionAborted(
                        f"work package {work_package_id} status changed after a "
                        "conflict; not overriding"
                    ) from None
                if fresh.lock_version is None:
                    raise TransitionAborted(
                        f"work package {work_package_id} payload has no lockVersion"
                    ) from None

    async def _post_comment(self, work_package_id: int, comment: str) -> None:
        comment = bound_comment(
            comment,
            max_bytes=self._settings.comment_max_bytes,
            line_max_bytes=self._settings.comment_line_max_bytes,
        )
        # Verified live: comments are created through the activities
        # collection with a nested ``raw`` comment.
        # ``/work_packages/<id>/comments`` does not exist (HTTP 404).
        await self._client.post_json(
            f"/api/v3/work_packages/{work_package_id}/activities",
            json_body={"comment": {"raw": comment}},
        )
        logger.info(
            "engine comment posted for work package %s",
            work_package_id,
            extra={
                "work_package_id": work_package_id,
                "comment_event": comment.split("\n", 1)[0],
            },
        )

    async def _fetch_work_package(self, work_package_id: int) -> WorkPackage | None:
        try:
            payload = await self._client.get_json(
                f"/api/v3/work_packages/{work_package_id}"
            )
        except OpenProjectNotFoundError:
            return None
        if not isinstance(payload, dict):
            return None
        return parse_work_package(
            payload,
            self._resolution.agent_field.field_id,
            self._option_id_to_value(),
        )

    def _option_id_to_value(self) -> dict[int, str]:
        return {
            option_id: value
            for value, option_id in self._resolution.agent_field.options.items()
        }

    # -- review verification (M5) ---------------------------------------------

    async def _reviewer_commented_since(
        self,
        work_package_id: int,
        agent_key: str,
        since: datetime,
    ) -> bool:
        """Whether the reviewer user commented on the work package after *since*.

        OpenProject activities carry the comment text, the author link and the
        creation time (verified shape): ``comment.raw``, ``_links.user`` and
        ``createdAt``.
        """
        reviewer_id = self._reviewer_user_id(agent_key)
        async for activity in self._client.iter_collection(
            f"/api/v3/work_packages/{work_package_id}/activities"
        ):
            if activity_comment_text(activity) is None:
                continue
            if activity_author_id(activity) != reviewer_id:
                continue
            created_at = activity_created_at(activity)
            if created_at is not None and created_at >= since:
                logger.info(
                    "reviewer comment found for work package %s",
                    work_package_id,
                    extra={
                        "work_package_id": work_package_id,
                        "activity_id": activity.get("id"),
                    },
                )
                return True
        return False

    def _reviewer_user_id(self, agent_key: str | None = None) -> int:
        """The reviewer user id (default: the configured reviewer agent)."""
        agent = (
            self._config.agents[agent_key]
            if agent_key is not None
            else self._config.reviewer
        )
        return self._resolution.users[agent.op_login].id

    async def _fetch_activities(
        self, work_package_id: int
    ) -> list[dict[str, Any]]:
        """The full activity history of a work package (oldest first)."""
        return await self._client.get_collection(
            f"/api/v3/work_packages/{work_package_id}/activities"
        )

    def _on_hold_status_name(self) -> str:
        return self._resolution.statuses["on_hold"].name

    async def _reset_crash_count_if_unheld(self, wp: WorkPackage) -> None:
        """Restore the crash and checkpoint budgets when a human un-held it.

        The crash counter only ever incremented, so an escalation used up the
        whole budget for good: WP 144 had to be requeued with a hand-written
        ``DELETE``. The engine itself only ever moves tickets *to* On Hold, so
        the newest status change leaving On Hold is a human saying the cause
        was addressed. A checkpoint streak is reset the same way: the human has
        read the cap comment, so the ticket starts a fresh wait budget.

        Only tickets that still carry a counter or a streak cost an API call.
        """
        previous = self._store.crash_count(wp.id)
        streak = self._store.checkpoint_streak(wp.id)
        if previous == 0 and streak == 0:
            return
        try:
            activities = await self._fetch_activities(wp.id)
        except OpenProjectError as error:
            logger.warning(
                "could not read the history of %s to decide whether to reset "
                "the crash counter (left at %d): %s",
                wp.display_id,
                previous,
                error,
                extra={"work_package_id": wp.id},
            )
            return
        if not unheld(activities, self._on_hold_status_name()):
            return
        if previous:
            self._store.reset_crash_count(wp.id)
        baseline = self._store.reset_checkpoint_streak(wp.id) if streak else 0
        logger.info(
            "reset the budgets for %s (crash counter was %d, checkpoint streak "
            "was %d, baseline run %d): the ticket left On Hold",
            wp.display_id,
            previous,
            streak,
            baseline,
            extra={
                "work_package_id": wp.id,
                "previous_crash_count": previous,
                "previous_checkpoint_streak": streak,
                "checkpoint_baseline": baseline,
            },
        )

    def _rejected_status_name(self) -> str:
        return self._resolution.statuses["rejected"].name

    def _agent_user_ids(self) -> frozenset[int]:
        ids: set[int] = set()
        for agent in self._config.agents.values():
            user = self._resolution.users.get(agent.op_login)
            if user is not None:
                ids.add(user.id)
        return frozenset(ids)

    def _author_names(self) -> dict[int, str]:
        names = {user.id: user.name for user in self._resolution.users.values()}
        names[self._resolution.engine_user.id] = self._resolution.engine_user.name
        return names

    # -- workspace, prompt and runner -------------------------------------------

    async def _prepare_workspace(
        self,
        claimed: ClaimedRun,
        run_id: int,
        *,
        keep_existing_worktree: bool = False,
    ) -> Workspace | None:
        """Prepare the workspace; record the failure and return None if it fails.

        ``keep_existing_worktree`` reuses a clean worktree that is already on
        the branch head instead of recreating it, which keeps build caches
        warm across checkpoint runs. A dirty or out-of-date worktree is
        handled exactly as before.
        """
        wp = claimed.item.work_package
        agent = self._config.agents[claimed.item.agent_key]
        lock = self._workspace_locks.setdefault(agent.workspace, asyncio.Lock())
        try:
            async with lock:
                workspace = await prepare_workspace(
                    root=self._workspace_root(),
                    agent_workspace=agent.workspace,
                    github_repo=self._repo_github(wp.project_id),
                    clone_dir=self._repo_dir(wp.project_id),
                    default_branch=self._repo_default_branch(wp.project_id),
                    project_id=wp.project_id,
                    work_package_id=wp.id,
                    role=agent.kind,
                    gh_token=self._gh_token(claimed.item.agent_key),
                    git_host=self._settings.git_host,
                    max_kept_worktree_bytes=self._max_kept_worktree_bytes(),
                    keep_existing_worktree=keep_existing_worktree,
                )
                self._active_workspaces[run_id] = workspace
                return workspace
        except WorkspaceError as error:
            await self._fail_before_run(
                run_id, claimed, f"workspace preparation failed: {error}"
            )
            return None

    async def _archive_workspace(
        self,
        workspace: Workspace,
        run_dir: Path,
        *,
        failed: bool,
    ) -> WorktreeArchive | None:
        """Release a run's worktree, preserving the state of a failed one.

        A successful run's worktree is removed as before. A failed run's state
        is written to ``runs/<id>/worktree`` first (status, diffs, unpushed
        commits and untracked files) and the worktree itself is kept for the
        retention window, unless ``KEEP_FAILED_WORKTREES`` is false or it is
        larger than ``FAILED_WORKTREE_MAX_MB``.
        """
        if not failed:
            await cleanup_workspace(workspace)
            return None
        return await archive_failed_worktree(
            workspace,
            run_dir=run_dir,
            keep=self._settings.keep_failed_worktrees,
            max_keep_bytes=self._max_kept_worktree_bytes(),
        )

    async def _finish_workspace(
        self,
        claimed: ClaimedRun,
        workspace: Workspace | None,
        run_dir: Path | None,
        *,
        decision: OutcomeDecision,
        keep: bool = False,
    ) -> WorktreeArchive | None:
        """Capture a failure, keep a checkpoint, or remove a delivered worktree.

        ``keep`` marks a checkpoint whose worktree the next run reuses, so the
        warm build caches survive the hand-over. Nothing is captured in that
        case: the worktree is clean and already committed to the branch.
        """
        if workspace is None:
            return None
        if keep and decision.run_state == SUCCEEDED:
            logger.info(
                "keeping the checkpoint worktree %s for the next run",
                workspace.repo_dir,
                extra={"repo_dir": str(workspace.repo_dir)},
            )
            return None
        return await self._archive_workspace(
            workspace,
            run_dir or self._store.runs_dir / str(claimed.run_id or 0),
            failed=decision.run_state != SUCCEEDED,
        )

    def _max_kept_worktree_bytes(self) -> int:
        return self._settings.failed_worktree_max_mb * 1024 * 1024

    def _build_request(
        self,
        claimed: ClaimedRun,
        workspace: Workspace,
        run_id: int,
        run_dir: Path,
        prompt: str,
    ) -> RunRequest:
        """Write the prompt into the run directory and build the runner request."""
        agent = self._config.agents[claimed.item.agent_key]
        prompt_file = run_dir / "prompt.txt"
        prompt_file.write_text(prompt, encoding="utf-8")
        request = self._runner.build_request(
            claimed.item.agent_key,
            run_id=run_id,
            work_package_id=claimed.item.work_package.id,
            run_dir=run_dir,
            repo_dir=workspace.repo_dir,
            workspace_dir=self._workspace_root() / agent.workspace,
            branch=workspace.branch,
            prompt_file=prompt_file,
            timeout_seconds=agent.timeout_seconds
            or self._settings.run_timeout_seconds,
        )

        self._active_requests[run_id] = request
        return request

    async def _run(self, request: RunRequest, run_id: int) -> RunResult:
        def on_spawned(pid: int, pgid: int, proc_start: str) -> None:
            self._store.set_process(run_id, pid=pid, pgid=pgid, proc_start=proc_start)

        return await self._runner.run(request, on_spawned=on_spawned)

    # -- prompt ---------------------------------------------------------------

    def _render_prompt(
        self,
        claimed: ClaimedRun,
        workspace: Workspace,
        *,
        pr: PullRequest | None = None,
        fix_context: FixContext | None = None,
        attempt: int = 1,
        continuation: dict[str, Any] | None = None,
    ) -> str:
        wp = claimed.item.work_package
        agent = self._config.agents[claimed.item.agent_key]
        kind = (
            claimed.kind
            if claimed.kind in ("implement", "fix", "resume", "review")
            else "implement"
        )
        base_url = (self._settings.op_base_url or "").rstrip("/")
        context: dict[str, Any] = {
            "agent": {"name": agent.name},
            "standard_path": str(workspace.standard_path),
            "wp": {
                "id": wp.id,
                "subject": wp.subject,
                "type": wp.type_name,
                "url": f"{base_url}/projects/{wp.project_id}/work_packages/{wp.id}",
                "description": wp.description,
            },
            "project": {"identifier": self._project_identifier(wp.project_id)},
            "repo": {
                "github": self._repo_github(wp.project_id),
                "default_branch": (
                    workspace.default_branch
                    or self._repo_default_branch(wp.project_id)
                    or "unknown"
                ),
            },
            "repo_dir": str(workspace.repo_dir),
            "branch": workspace.branch,
            "pr": {
                "number": pr.number if pr is not None else None,
                "url": pr.url if pr is not None else None,
            },
            "review_number": 1,
        }
        context["interruption"] = None
        context["continuation"] = continuation
        if kind == "resume":
            previous = self._store.last_interrupted_run(wp.id)
            last_code = self._store.last_code_run(wp.id)
            if previous is not None and (last_code is None or previous.id > last_code.id):
                previous_dir = self._store.runs_dir / str(previous.id)
                try:
                    context["interruption"] = json.loads(
                        (previous_dir / "meta.json").read_text(encoding="utf-8")
                    ) | {"run_dir": str(previous_dir)}
                except (OSError, ValueError):
                    logger.warning("interruption metadata unavailable for run %s", previous.id)
        if kind == "fix":
            context.update(
                {
                    "attempt": attempt,
                    "max_rejections": self._settings.max_rejections,
                    "feedback": fix_context.feedback if fix_context is not None else None,
                    "later_human_comments": (
                        fix_context.later_human_comments
                        if fix_context is not None
                        else ()
                    ),
                }
            )
        prompt = self._prompts.render(kind, context)
        if self._config.runners[agent.runner].inject_standard:
            prompt = with_standard(
                prompt, workspace.standard_path, git_host=self._settings.git_host,
                repo=self._repo_github(wp.project_id),
            )
        return prompt

    # -- resume briefing -----------------------------------------------------

    async def _continuation_context(
        self,
        claimed: ClaimedRun,
        *,
        open_pr: PullRequest | None,
        resume_hold: ResumeHold | None = None,
    ) -> dict[str, Any] | None:
        """What the previous implementer run did, for the resume prompt.

        The run itself ran with ``--no-session``, so the prompt is the only
        place a fresh process can learn where the last one stopped. The brief
        is deliberately short: the result line, the observed head, the open
        PR, the hold reason and the agent's own ticket report, which is where
        the remaining work is written down.
        """
        wp = claimed.item.work_package
        previous = self._store.last_implementer_run(wp.id)
        if previous is None:
            return None
        previous_dir = self._store.runs_dir / str(previous.id)
        meta: dict[str, Any] = {}
        try:
            meta = json.loads((previous_dir / "meta.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            logger.warning(
                "continuation metadata unavailable for run %s", previous.id
            )
        heads = meta.get("heads") or {}

        report = await self._agent_report_text(wp.id, claimed.item.agent_key)
        return {
            "run_id": previous.id,
            "kind": previous.kind,
            "outcome": previous.outcome,
            "checkpoint": previous.outcome in CHECKPOINT_OUTCOMES,
            "finished_at": previous.finished_at,
            "result_line": previous.result_line,
            "branch_head": heads.get("branch_after"),
            "worktree_kept": bool(meta.get("worktree_checkpoint")),
            "pr_url": open_pr.url if open_pr is not None else meta.get("pr_url"),
            "pr_draft": open_pr.is_draft if open_pr is not None else None,
            "hold_reason": resume_hold.reason if resume_hold is not None else None,
            "report": report,
        }

    async def _agent_report_text(
        self, work_package_id: int, agent_key: str
    ) -> str | None:
        """The agent's most recent ticket comment, truncated for the prompt."""
        agent = self._config.agents.get(agent_key)
        if agent is None:
            return None
        user = self._resolution.users.get(agent.op_login)
        if user is None:
            return None
        try:
            activities = await self._fetch_activities(work_package_id)
        except OpenProjectError as error:
            logger.warning(
                "could not read the previous report of work package %s: %s",
                work_package_id,
                error,
                extra={"work_package_id": work_package_id},
            )
            return None
        latest: str | None = None
        for activity in activities:
            text = activity_comment_text(activity)
            if text is None or activity_author_id(activity) != user.id:
                continue
            latest = text
        if latest is None:
            return None
        latest = latest.strip()
        if len(latest) > MAX_CONTINUATION_REPORT_CHARS:
            latest = (
                latest[:MAX_CONTINUATION_REPORT_CHARS]
                + "\n… report truncated; read the ticket for the rest …"
            )
        return latest

    # -- meta.json -------------------------------------------

    def _write_meta(
        self,
        claimed: ClaimedRun,
        request: RunRequest | None,
        result: RunResult | None,
        *,
        state: str,
        outcome: str,
        transition: str | None,
        pr: PullRequest | None,
        started_at: datetime | None,
        archive: WorktreeArchive | None = None,
        heads: dict[str, Any] | None = None,
        decision: dict[str, Any] | None = None,
        engagement_error: str | None = None,
        checkpoint_kept: bool = False,
    ) -> None:
        run_dir = claimed.run_dir or (self._store.runs_dir / str(claimed.run_id or 0))
        run_dir.mkdir(parents=True, exist_ok=True)
        head = None
        head_dir = archive.artefact if archive is not None else run_dir / "worktree"
        try:
            lines = (head_dir / "head.txt").read_text().splitlines() if head_dir else []
            for line in lines:
                if line.startswith("HEAD: "):
                    head = line.removeprefix("HEAD: ")
        except OSError:
            pass
        meta: dict[str, Any] = {
            "branch_head": head,
            "run_dir": str(run_dir),
            "decision": decision,
            "engagement_error": engagement_error,
            "run_id": claimed.run_id,
            "work_package_id": claimed.item.work_package.id,
            "agent": claimed.item.agent_key,
            "kind": claimed.kind,
            "branch": claimed.branch,
            "project": self._project_identifier(claimed.item.work_package.project_id),
            "repo": self._repo_github(claimed.item.work_package.project_id),
            "started_at": started_at.isoformat() if started_at is not None else None,
            "finished_at": datetime.now(UTC).isoformat(),
            "state": state,
            "outcome": outcome,
            "transition": transition,
            "pr_url": pr.url if pr is not None else None,
            "argv": list(request.argv) if request is not None else None,
            "cwd": str(request.cwd) if request is not None else None,
            "env": redact_mapping(request.env) if request is not None else {},
            # Post-mortem artefact of a failed run: the worktree's status,
            # diffs, unpushed commits and untracked files.
            "worktree_artefact": (
                str(archive.artefact)
                if archive is not None and archive.artefact is not None
                else None
            ),
            "worktree_kept": (
                bool(archive.kept) if archive is not None else checkpoint_kept
            ),
            # A checkpoint worktree is reused by the next run, not only kept
            # for post-mortem inspection.
            "worktree_checkpoint": checkpoint_kept,
        }
        if heads is not None:
            meta["heads"] = heads
        if archive is not None:
            meta["worktree"] = archive.as_dict()
        if result is not None:
            meta["exit_code"] = result.exit_code
            meta["timed_out"] = result.timed_out
            meta["result_line"] = result.result_line
            meta["duration_seconds"] = round(result.duration_seconds, 2)
        (run_dir / "meta.json").write_text(
            json.dumps(meta, indent=2) + "\n", encoding="utf-8"
        )

    # -- config helpers ---------------------------------------------------------

    def _workspace_root(self) -> Path:
        root = self._settings.workspace_root
        if root is None:
            raise WorkspaceError(
                "WORKSPACE_ROOT is not set; the engine cannot prepare workspaces"
            )
        return root

    def _gh_token(self, agent_key: str | None = None) -> str | None:
        """The GitHub token used by an agent (or the engine when unset).

        A per-agent ``GH_TOKEN`` in the workflow config wins, so the reviewer
        can clone/fetch with its dedicated account while the engine keeps using
        the shared token for lookups. This also makes a configured token the
        clone URL's credential for that agent's workspace.
        """
        if agent_key is not None:
            agent = self._config.agents.get(agent_key)
            if agent is not None:
                token_var = (
                    "GH_TOKEN" if self._settings.git_host == "github.com" else "GITLAB_TOKEN"
                )
                configured_token = agent.env.get(token_var, "").strip()
                if configured_token:
                    return configured_token
        shared_token = (
            self._settings.gh_token if self._settings.git_host == "github.com"
            else self._settings.gitlab_token
        )
        return shared_token.get_secret_value() if shared_token is not None else None

    def _project_for(self, project_id: int) -> ProjectConfig:
        for project in self._config.projects:
            if project.enabled and self._project_id(project.identifier) == project_id:
                return project
        raise WorkspaceError(
            f"no enabled project with id {project_id} in workflow config"
        )

    def _project_id(self, identifier: str) -> int:
        return self._resolution.projects[identifier].id

    def _project_identifier(self, project_id: int) -> str:
        for identifier, resolved in self._resolution.projects.items():
            if resolved.id == project_id:
                return identifier
        return f"<unknown:{project_id}>"

    def _repo_github(self, project_id: int) -> str:
        repo = self._project_for(project_id).repo
        if repo is None:
            raise WorkspaceError(
                f"project {project_id} has no repository; GIT_REPO was not resolved"
            )
        return repo.github

    def _repo_dir(self, project_id: int) -> str:
        repo = self._project_for(project_id).repo
        if repo is None:
            raise WorkspaceError(
                f"project {project_id} has no repository; GIT_REPO was not resolved"
            )
        return repo.dir

    def _repo_default_branch(self, project_id: int) -> str | None:
        repo = self._project_for(project_id).repo
        return repo.default_branch if repo is not None else None

    def _resolve_template_dir(self) -> Path:
        from op_agent_engine.prompts import resolve_template_dir

        return resolve_template_dir(self._settings)
