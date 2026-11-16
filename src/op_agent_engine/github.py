"""GitHub lookups through the ``gh`` CLI.

The engine never talks to the GitHub API directly: it uses the host's
existing ``gh`` authentication (or ``GH_TOKEN`` when set).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from op_agent_engine.logging import get_logger

logger = get_logger(__name__)

#: Timeout for a single gh invocation.
GH_TIMEOUT_SECONDS = 30.0


class GitHubError(Exception):
    """A ``gh`` invocation failed (missing binary, auth, network, ...)."""


@dataclass(frozen=True)
class PullRequest:
    """A GitHub pull request (as far as the engine needs it)."""

    number: int
    url: str
    head_branch: str
    state: str  # "open" | "closed" | "merged"
    #: A draft PR is not finished work: the engine does not treat it as an
    #: authoritative implementation result.
    is_draft: bool = False
    head_oid: str | None = None


def head_advanced(before: str | None, after: str | None) -> bool:
    """Only observed, different heads prove progress (missing is not a head)."""
    return bool(before) and bool(after) and before != after


#: Check-run conclusions that mean the commit will not go green.
FAILED_CONCLUSIONS = frozenset(
    {
        "failure",
        "timed_out",
        "cancelled",
        "action_required",
        "startup_failure",
        "stale",
    }
)


@dataclass(frozen=True)
class ChecksSummary:
    """The state of the CI checks attached to one commit.

    ``state`` is one of ``pending`` (at least one check has not finished),
    ``failure`` (nothing pending, at least one failed), ``success`` (nothing
    pending, nothing failed) or ``none`` (the commit carries no checks at all,
    e.g. the repository has no CI). The counts make the engine comment
    specific without exposing provider payloads.
    """

    state: str
    total: int = 0
    pending: int = 0
    failed: int = 0

    @property
    def is_pending(self) -> bool:
        return self.state == "pending"

    def describe(self) -> str:
        """A short human summary for an engine comment."""
        if self.state == "none":
            return "no checks reported"
        if self.state == "pending":
            return f"{self.pending} of {self.total} check(s) still running"
        if self.state == "failure":
            return f"{self.failed} of {self.total} check(s) failed"
        return f"all {self.total} check(s) finished"


def summarise_checks(
    check_runs: Mapping[str, Any], combined_status: Mapping[str, Any] | None = None
) -> ChecksSummary:
    """Reduce GitHub check-runs and a combined commit status to a summary."""
    total = pending = failed = 0
    runs = check_runs.get("check_runs")
    for run in runs if isinstance(runs, list) else []:
        if not isinstance(run, Mapping):
            continue
        total += 1
        if run.get("status") != "completed":
            pending += 1
        elif str(run.get("conclusion") or "") in FAILED_CONCLUSIONS:
            failed += 1
    statuses = (combined_status or {}).get("statuses")
    for status in statuses if isinstance(statuses, list) else []:
        if not isinstance(status, Mapping):
            continue
        total += 1
        state = str(status.get("state") or "")
        if state == "pending":
            pending += 1
        elif state in {"failure", "error"}:
            failed += 1
    if pending:
        state = "pending"
    elif failed:
        state = "failure"
    elif total:
        state = "success"
    else:
        state = "none"
    return ChecksSummary(state=state, total=total, pending=pending, failed=failed)


class GitHub:
    """PR lookups via ``gh pr list`` with JSON output."""

    def __init__(
        self,
        *,
        executable: str = "gh",
        env: dict[str, str] | None = None,
        timeout: float = GH_TIMEOUT_SECONDS,
    ) -> None:
        self._executable = executable
        self._env = env
        self._timeout = timeout

    async def _run_json(self, args: list[str], *, what: str) -> Any:
        """Run ``gh`` and parse its JSON output, normalising every failure."""
        try:
            process = await asyncio.create_subprocess_exec(
                *args,
                env=self._env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(
                process.communicate(), timeout=self._timeout
            )
        except FileNotFoundError as error:
            raise GitHubError(f"gh executable not found: {self._executable}") from error
        except TimeoutError as error:
            with contextlib.suppress(ProcessLookupError):
                process.kill()
            raise GitHubError(
                f"gh timed out after {self._timeout:.0f}s"
            ) from error
        if process.returncode != 0:
            detail = stderr.decode("utf-8", errors="replace").strip()[:500]
            raise GitHubError(
                f"gh {what} failed ({process.returncode}): {detail}"
            )
        try:
            return json.loads(stdout.decode("utf-8") or "null")
        except json.JSONDecodeError as error:
            raise GitHubError(f"gh returned invalid JSON: {error}") from error

    async def commit_checks(self, repo: str, sha: str) -> ChecksSummary:
        """The CI state of commit *sha* (check runs plus combined status).

        The engine polls this itself before spending another agent run, so a
        ticket that is only waiting on CI does not cost model time. Any
        provider failure is raised as :class:`GitHubError`; the caller then
        resumes normally instead of blocking on an unknown state.
        """
        check_runs = await self._run_json(
            [
                self._executable,
                "api",
                f"repos/{repo}/commits/{sha}/check-runs?per_page=100",
            ],
            what="api check-runs",
        )
        if not isinstance(check_runs, Mapping):
            raise GitHubError("gh api check-runs returned an unexpected payload")
        combined: Mapping[str, Any] | None = None
        try:
            status = await self._run_json(
                [self._executable, "api", f"repos/{repo}/commits/{sha}/status"],
                what="api status",
            )
            if isinstance(status, Mapping):
                combined = status
        except GitHubError:
            # Legacy commit statuses are optional; check runs are the norm.
            combined = None
        return summarise_checks(check_runs, combined)

    async def find_pr(
        self, repo: str, branch: str, *, state: str = "open"
    ) -> PullRequest | None:
        """Return the first PR in *repo* with head *branch* and *state*, or None."""
        args = [
            self._executable,
            "pr",
            "list",
            "--repo",
            repo,
            "--head",
            branch,
            "--state",
            state,
            "--json",
            "number,url,headRefName,isDraft,headRefOid",
        ]
        payload = await self._run_json(args, what="pr list")
        entries = payload if isinstance(payload, list) else []
        if not entries:
            return None
        entry = entries[0]
        return PullRequest(
            number=int(entry["number"]),
            url=str(entry["url"]),
            head_branch=str(entry.get("headRefName", branch)),
            state=state,
            is_draft=bool(entry.get("isDraft", False)),
            head_oid=entry.get("headRefOid"),
        )
