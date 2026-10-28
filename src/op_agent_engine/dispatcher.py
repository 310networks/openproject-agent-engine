"""Execution of claimed runs (PLAN section 4; milestone M4).

The dispatcher takes one claimed implement run to completion:

1. Prepare the workspace (clone/fetch/checkout, PLAN section 11).
2. Render the prompt into the per-run directory (PLAN section 10).
3. Run the agent through the runner abstraction (PLAN section 6).
4. Verify the outcome against GitHub (open PR for the branch) and apply the
   OpenProject status transition plus engine comment (PLAN section 4).
5. Record the run in the store and write ``meta.json`` (PLAN section 16).

Crash handling: a failed run (non-zero exit, timeout, no PR) increments the
work package's crash counter. When the counter exceeds ``MAX_CRASH_RETRIES``
the work package moves to On Hold with an engine comment (escalation).
Otherwise it stays In Progress and a resume run may be dispatched on a later
poll (M7).

Fix and review runs are claimed (M3) but not executed yet (M5/M6); resume
dispatch is M7.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from op_agent_engine.config import (
    AgentConfig,
    EngineSettings,
    ProjectConfig,
    WorkflowConfig,
)
from op_agent_engine.github import GitHub, GitHubError, PullRequest
from op_agent_engine.logging import get_logger, redact_mapping
from op_agent_engine.openproject.client import (
    OpenProjectClient,
    OpenProjectError,
    OpenProjectHTTPError,
    OpenProjectNotFoundError,
)
from op_agent_engine.openproject.resolve import OpenProjectResolution
from op_agent_engine.openproject.workpackage import WorkPackage, parse_work_package
from op_agent_engine.outcomes import (
    decide_implementer_outcome,
    escalation_comment,
    read_tail,
)
from op_agent_engine.prompts import PromptRenderer, with_standard
from op_agent_engine.runners.base import OnSpawned, RunRequest, RunResult
from op_agent_engine.runners.generic import GenericRunner
from op_agent_engine.scheduler import ClaimedRun
from op_agent_engine.store import FAILED, Store
from op_agent_engine.workspace import Workspace, WorkspaceError, prepare_workspace

logger = get_logger(__name__)


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
    """Executes claimed runs end to end (M4: implement kind)."""

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

        if claimed.kind != "implement":
            logger.info(
                "run kind %r is claimed but not executed yet (M5/M6); "
                "leaving the run active",
                claimed.kind,
                extra=log_extra,
            )
            return

        if claimed.run_id is None:
            logger.info(
                "[dry-run] would spawn implement run for %s (branch %s)",
                wp.display_id,
                claimed.branch,
                extra={**log_extra, "dry_run": True},
            )
            return

        run_id = claimed.run_id
        run_dir = claimed.run_dir or (self._store.runs_dir / str(run_id))
        run_dir.mkdir(parents=True, exist_ok=True)
        started_at = datetime.now(UTC)
        logger.info("starting implement run", extra=log_extra)

        # 1. workspace
        try:
            workspace = await prepare_workspace(
                root=self._workspace_root(),
                agent_workspace=self._config.agents[agent_key].workspace,
                github_repo=self._repo_github(wp.project_id),
                clone_dir=self._repo_dir(wp.project_id),
                default_branch=self._repo_default_branch(wp.project_id),
                project_id=wp.project_id,
                work_package_id=wp.id,
                role=self._config.agents[agent_key].kind,
                gh_token=self._gh_token(),
            )
        except WorkspaceError as error:
            await self._fail_before_run(
                run_id, claimed, f"workspace preparation failed: {error}"
            )
            return

        # 2. prompt
        agent = self._config.agents[agent_key]
        prompt_file = run_dir / "prompt.txt"
        prompt_file.write_text(
            self._render_prompt(claimed, wp, agent, workspace), encoding="utf-8"
        )

        # 3. runner request
        timeout = agent.timeout_seconds or self._settings.run_timeout_seconds
        request = self._runner.build_request(
            agent_key,
            run_id=run_id,
            work_package_id=wp.id,
            run_dir=run_dir,
            repo_dir=workspace.repo_dir,
            workspace_dir=self._workspace_root() / agent.workspace,
            branch=workspace.branch,
            prompt_file=prompt_file,
            timeout_seconds=timeout,
        )

        # 4. run
        def on_spawned(pid: int, pgid: int, proc_start: str) -> None:
            self._store.set_process(run_id, pid=pid, pgid=pgid, proc_start=proc_start)

        result = await self._runner.run(request, on_spawned=on_spawned)

        # 5. external check: open PR for the branch?
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

        # 6. decision + OpenProject actions + store + meta
        await self._finish_run(
            run_id, claimed, request, result, pr, started_at
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
        wp = claimed.item.work_package
        decision = decide_implementer_outcome(
            exit_code=result.exit_code,
            timed_out=result.timed_out,
            result_line=result.result_line,
            pr_found=pr is not None,
        )
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
                    "stays In Progress for a resume run (M7)",
                    outcome,
                    crash_count,
                    self._settings.max_crash_retries,
                    extra={**log_extra, "crash_count": crash_count},
                )

        try:
            if transition is not None:
                await self._transition(
                    wp.id, transition, expected_status_key="in_progress"
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

    async def _fail_before_run(
        self, run_id: int, claimed: ClaimedRun, detail: str
    ) -> None:
        """A workspace (or similar) failure before the process started."""
        wp = claimed.item.work_package
        await self._crash_escalate(
            wp.id,
            outcome="workspace_error",
            detail=detail,
            exit_code=None,
            tail_lines=None,
            run_id=run_id,
        )
        self._store.finish_run(
            run_id, state=FAILED, outcome="workspace_error"
        )
        self._write_meta(
            claimed, None, None, state=FAILED, outcome="workspace_error",
            transition=None, pr=None, started_at=datetime.now(UTC),
        )

    async def _fail_after_run(
        self,
        run_id: int,
        claimed: ClaimedRun,
        request: RunRequest,
        result: RunResult,
        *,
        detail: str,
    ) -> None:
        """A failure after the process ran (e.g. PR verification error)."""
        wp = claimed.item.work_package
        tail = read_tail(result.stdout_path) or read_tail(result.stderr_path)
        await self._crash_escalate(
            wp.id,
            outcome="pr_verification_error",
            detail=detail,
            exit_code=result.exit_code,
            tail_lines=tail,
            run_id=run_id,
        )
        self._store.finish_run(
            run_id,
            state=FAILED,
            outcome="pr_verification_error",
            exit_code=result.exit_code,
            result_line=result.result_line,
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
            work_package_id, "on_hold", expected_status_key="in_progress"
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
        await self._client.post_json(
            f"/api/v3/work_packages/{work_package_id}/comments",
            json_body={"comment": comment},
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

    # -- prompt ---------------------------------------------------------------

    def _render_prompt(
        self,
        claimed: ClaimedRun,
        wp: WorkPackage,
        agent: AgentConfig,
        workspace: Workspace,
    ) -> str:
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
        }
        prompt = self._prompts.render("implement", context)
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
