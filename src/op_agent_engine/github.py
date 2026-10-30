"""GitHub lookups through the ``gh`` CLI (PLAN section 16: external checks).

The engine never talks to the GitHub API directly: it uses the host's
existing ``gh`` authentication (or ``GH_TOKEN`` when set).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
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
            "number,url,headRefName,isDraft",
        ]
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
                f"gh pr list failed ({process.returncode}): {detail}"
            )
        try:
            entries: list[dict[str, Any]] = json.loads(
                stdout.decode("utf-8") or "[]"
            )
        except json.JSONDecodeError as error:
            raise GitHubError(f"gh returned invalid JSON: {error}") from error
        if not entries:
            return None
        entry = entries[0]
        return PullRequest(
            number=int(entry["number"]),
            url=str(entry["url"]),
            head_branch=str(entry.get("headRefName", branch)),
            state=state,
            is_draft=bool(entry.get("isDraft", False)),
        )
