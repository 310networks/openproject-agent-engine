"""Execution of claimed runs (PLAN section 4; milestones M4, M5 and M6).

The dispatcher takes one claimed run to completion:

1. Prepare the workspace (clone/fetch/checkout, PLAN section 11).
2. Render the prompt into the per-run directory (PLAN section 10).
3. Run the agent through the runner abstraction (PLAN section 6).
4. Verify the outcome against external state and apply the OpenProject status
   transition plus engine comment (PLAN section 4).
5. Record the run in the store and write ``meta.json`` (PLAN section 16).

Implement runs (M4) verify an open PR for the branch. Review runs (M5)
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

Resume dispatch is M7.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from op_agent_engine.config import (
    EngineSettings,
    ProjectConfig,
    WorkflowConfig,
)
from op_agent_engine.github import GitHub, GitHubError, PullRequest
from op_agent_engine.logging import get_logger, redact_mapping
from op_agent_engine.openproject.activities import (
    author_id as activity_author_id,
)
from op_agent_engine.openproject.activities import (
    comment_text as activity_comment_text,
)
from op_agent_engine.openproject.activities import (
    count_status_changes,
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
    OutcomeDecision,
    decide_implementer_outcome,
    decide_reviewer_outcome,
    escalation_comment,
    is_rejection,
    no_pr_comment,
    read_tail,
    rejection_cap_comment,
)
from op_agent_engine.prompts import FixContext, PromptRenderer, select_fix_context, with_standard
from op_agent_engine.runners.base import OnSpawned, RunRequest, RunResult
from op_agent_engine.runners.generic import GenericRunner
from op_agent_engine.scheduler import ClaimedRun
from op_agent_engine.store import FAILED, Store
from op_agent_engine.workspace import Workspace, WorkspaceError, prepare_workspace

logger = get_logger(__name__)

#: Run kinds the dispatcher executes. ``resume`` is M7.
EXECUTED_KINDS = frozenset({"implement", "fix", "review"})

#: The status a run was claimed in; transitions must start from there.
EXPECTED_STATUS_KEY_BY_KIND = {
    "implement": "in_progress",
    "fix": "in_progress",
    "review": "in_testing",
}


class TransitionAborted(Exception):
    """The work package left the expected status; the transition was not applied."""


class GitHubLike(Protocol):
    async def find_pr(
        self, repo: str, branch: str, *, state: str = "open"
    ) -> PullRequest | None: ...


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
        self._gh = gh if gh is not None else GitHub()
        self._runner = runner if runner is not None else GenericRunner(config)
        self._prompts = (
            prompts
            if prompts is not None
            else PromptRenderer(self._resolve_template_dir())
        )

    # -- main entry point ---------------------------------------------------

    async def execute(self, claimed: ClaimedRun) -> None:
        """Execute one claimed run; record the outcome in the store.

        Raises on infrastructure failures that should leave the run
        ``running`` (e.g. a transient OpenProject API error during the
        transition), so the next engine start recovers it.
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
            logger.info(
                "run kind %r is claimed but not executed yet (M7); "
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

        if claimed.kind == "review":
            await self._execute_review(claimed, run_id, run_dir, started_at)
        else:
            await self._execute_implement(claimed, run_id, run_dir, started_at)

    # -- implement runs (M4) ------------------------------------------------

    async def _execute_implement(
        self,
        claimed: ClaimedRun,
        run_id: int,
        run_dir: Path,
        started_at: datetime,
    ) -> None:
        wp = claimed.item.work_package
        workspace = await self._prepare_workspace(claimed, run_id)
        if workspace is None:
            return

        # A fix pass needs the reviewer feedback and the rejection number
        # (PLAN section 10); both come from the work package history.
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
                )
                return

        request = self._build_request(
            claimed,
            workspace,
            run_id,
            run_dir,
            self._render_prompt(
                claimed, workspace, fix_context=fix_context, attempt=attempt
            ),
        )
        result = await self._run(request, run_id)

        # External check: open PR for the branch?
        try:
            pr = await self._gh.find_pr(
                self._repo_github(wp.project_id), workspace.branch
            )
        except GitHubError as error:
            await self._fail_after_run(
                run_id, claimed, request, result,
                detail=f"PR verification failed: {error}",
            )
            return

        await self._finish_run(run_id, claimed, request, result, pr, started_at)

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

        # A review needs a PR to look at (PLAN section 4). Without one the
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

        # Approved? A merged PR is authoritative (PLAN section 4).
        try:
            merged = await self._gh.find_pr(repo, workspace.branch, state="merged")
        except GitHubError as error:
            await self._fail_after_run(
                run_id, claimed, request, result,
                detail=f"merged PR verification failed: {error}",
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
                self._write_meta(
                    claimed, request, result, state="running", outcome="error",
                    transition=None, pr=None, started_at=started_at,
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
    ) -> None:
        decision = decide_implementer_outcome(
            exit_code=result.exit_code,
            timed_out=result.timed_out,
            result_line=result.result_line,
            pr_found=pr is not None,
        )
        await self._apply_decision(
            run_id, claimed, request, result, decision, pr=pr, started_at=started_at
        )

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
    ) -> None:
        decision = decide_reviewer_outcome(
            exit_code=result.exit_code,
            timed_out=result.timed_out,
            result_line=result.result_line,
            pr_merged=merged is not None,
            rejection_comment_found=rejection_comment_found,
        )
        if decision.transition == "rejected":
            decision = await self._apply_rejection_cap(
                claimed, run_id, decision, started_at=started_at
            )
        await self._apply_decision(
            run_id,
            claimed,
            request,
            result,
            decision,
            pr=merged,
            started_at=started_at,
        )

    async def _apply_rejection_cap(
        self,
        claimed: ClaimedRun,
        run_id: int,
        decision: OutcomeDecision,
        *,
        started_at: datetime,
    ) -> OutcomeDecision:
        """Escalate to On Hold when the next rejection exceeds the cap.

        The rejection count is derived from the work package history (status
        changes to Rejected). The current rejection has not been applied yet,
        so ``previous + 1`` is the number this review would produce. With the
        default ``MAX_REJECTIONS=3`` rejection 3 is allowed and rejection 4
        moves the work package to On Hold (PLAN section 4).
        """
        wp = claimed.item.work_package
        try:
            activities = await self._fetch_activities(wp.id)
        except OpenProjectError as error:
            self._write_meta(
                claimed, None, None, state="running", outcome="error",
                transition=None, pr=None, started_at=started_at,
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
                    outcome=outcome,
                    exit_code=result.exit_code,
                    tail_lines=tail,
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
                    "stays %s for a later run (M7)",
                    outcome,
                    crash_count,
                    self._settings.max_crash_retries,
                    expected_status_key,
                    extra={**log_extra, "crash_count": crash_count},
                )

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
            # Transient API failure: leave the run 'running' so the next
            # engine start recovers it (interrupted -> M7 resume).
            self._write_meta(
                claimed, request, result, state="running", outcome="error",
                transition=transition, pr=pr, started_at=started_at,
            )
            logger.error(
                "OpenProject error after run %d finished; run left active: %s",
                run_id,
                error,
                extra=log_extra,
            )
            raise

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
        )
        logger.info(
            "run finished: state=%s outcome=%s transition=%s",
            state,
            outcome,
            transition,
            extra={**log_extra, "state": state, "outcome": outcome},
        )

    async def _review_without_pr(
        self, claimed: ClaimedRun, run_id: int, started_at: datetime
    ) -> None:
        """``Ready for Review`` without a PR: On Hold plus an engine comment.

        The reviewer is not started (PLAN section 4); the work package was
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
            self._write_meta(
                claimed, None, None, state="running", outcome="error",
                transition=transition, pr=None, started_at=started_at,
            )
            logger.error(
                "OpenProject error while handling a missing PR for run %d; "
                "run left active: %s",
                run_id,
                error,
                extra=log_extra,
            )
            raise

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
    ) -> None:
        """A workspace or prompt-context failure before the process started."""
        wp = claimed.item.work_package
        await self._crash_escalate(
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
        )

    async def _fail_after_run(
        self,
        run_id: int,
        claimed: ClaimedRun,
        request: RunRequest | None,
        result: RunResult | None,
        *,
        detail: str,
    ) -> None:
        """A failure after the process ran (e.g. PR verification error)."""
        wp = claimed.item.work_package
        tail: list[str] | None = None
        if result is not None:
            tail = read_tail(result.stdout_path) or read_tail(result.stderr_path)
        await self._crash_escalate(
            wp.id,
            outcome="pr_verification_error",
            detail=detail,
            exit_code=result.exit_code if result is not None else None,
            tail_lines=tail,
            run_id=run_id,
            expected_status_key=EXPECTED_STATUS_KEY_BY_KIND[claimed.kind],
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
            started_at=datetime.now(UTC),
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
    ) -> None:
        """Increment the crash counter; escalate to On Hold at the cap."""
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
            return
        comment = escalation_comment(
            outcome=outcome,
            exit_code=exit_code,
            tail_lines=tail_lines,
            detail=detail,
        )
        await self._transition(
            work_package_id, "on_hold", expected_status_key=expected_status_key
        )
        await self._post_comment(work_package_id, comment)

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
        fresh lockVersion (same protocol as the claim, PLAN section 9).
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
        # Verified live (docs/VERIFICATION.md section 11): comments are created
        # through the activities collection with a nested ``raw`` comment.
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
        creation time (verified shape, docs/VERIFICATION.md): ``comment.raw``,
        ``_links.user`` and ``createdAt``.
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
        self, claimed: ClaimedRun, run_id: int
    ) -> Workspace | None:
        """Prepare the workspace; record the failure and return None if it fails."""
        wp = claimed.item.work_package
        agent = self._config.agents[claimed.item.agent_key]
        try:
            return await prepare_workspace(
                root=self._workspace_root(),
                agent_workspace=agent.workspace,
                github_repo=self._repo_github(wp.project_id),
                clone_dir=self._repo_dir(wp.project_id),
                default_branch=self._repo_default_branch(wp.project_id),
                project_id=wp.project_id,
                work_package_id=wp.id,
                role=agent.kind,
                gh_token=self._gh_token(),
            )
        except WorkspaceError as error:
            await self._fail_before_run(
                run_id, claimed, f"workspace preparation failed: {error}"
            )
            return None

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
        return self._runner.build_request(
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
    ) -> str:
        wp = claimed.item.work_package
        agent = self._config.agents[claimed.item.agent_key]
        kind = claimed.kind if claimed.kind in ("implement", "fix", "review") else "implement"
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
                "default_branch": self._repo_default_branch(wp.project_id),
            },
            "repo_dir": str(workspace.repo_dir),
            "branch": workspace.branch,
            "pr": {
                "number": pr.number if pr is not None else None,
                "url": pr.url if pr is not None else None,
            },
            "review_number": 1,
        }
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
            prompt = with_standard(prompt, workspace.standard_path)
        return prompt

    # -- meta.json (PLAN section 16) -------------------------------------------

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
    ) -> None:
        run_dir = claimed.run_dir or (self._store.runs_dir / str(claimed.run_id or 0))
        run_dir.mkdir(parents=True, exist_ok=True)
        meta: dict[str, Any] = {
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
        }
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

    def _gh_token(self) -> str | None:
        token = self._settings.gh_token
        return token.get_secret_value() if token is not None else None

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
        return self._project_for(project_id).repo.github

    def _repo_dir(self, project_id: int) -> str:
        return self._project_for(project_id).repo.dir

    def _repo_default_branch(self, project_id: int) -> str:
        return self._project_for(project_id).repo.default_branch

    def _resolve_template_dir(self) -> Path:
        from op_agent_engine.prompts import resolve_template_dir

        return resolve_template_dir(self._settings)
