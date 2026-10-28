"""Outcome verification for implementer runs (PLAN sections 4 and 10).

The rules for implement and fix runs (fix is M5, review is M6):

* ``RESULT: BLOCKED <reason>`` -> blocked: the work package moves to On
  Hold and the reason is posted as an engine comment.
* Otherwise an open PR for the branch (external check via GitHub) ->
  success: the work package moves to Ready for Review.
* No PR (or non-zero exit, or timeout) -> failure; the crash retry counter
  increments and a resume run may be dispatched on a later poll (M7). When
  the cap (``MAX_CRASH_RETRIES``) is reached the work package moves to On
  Hold with an engine comment (escalation).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from op_agent_engine.store import FAILED, SUCCEEDED, TIMEOUT

#: Matches a ``RESULT:`` line anywhere in the payload (the line itself is
#: already extracted from the log tail by the runner).
RESULT_RE = re.compile(r"^\s*RESULT:\s*(?P<payload>.+)$")

#: ``BLOCKED <one-line reason>`` (case-insensitive keyword).
BLOCKED_RE = re.compile(r"^BLOCKED\s+(?P<reason>.+)$", re.IGNORECASE)

#: How many tail lines of the run log go into an escalation comment.
ESCALATION_TAIL_LINES = 40


@dataclass(frozen=True)
class OutcomeDecision:
    """What the engine must do after one run.

    * ``run_state``/``outcome``: recorded in the runs table.
    * ``transition``: OpenProject status key to move the work package to
      (``None`` = leave it, e.g. a retryable failure that stays In Progress).
    * ``comment``: engine comment to post (always starts with ``ENGINE:``).
    * ``crash_retry``: the failure counts against ``MAX_CRASH_RETRIES``; when
      the cap is exceeded the dispatcher escalates (On Hold + comment).
    """

    run_state: str
    outcome: str
    transition: str | None = None
    comment: str | None = None
    crash_retry: bool = False


def parse_result_line(result_line: str | None) -> str | None:
    """The payload after ``RESULT:`` (e.g. ``BLOCKED need X``), or None."""
    if result_line is None:
        return None
    match = RESULT_RE.match(result_line)
    return match.group("payload").strip() if match else None


def blocked_reason(result_line: str | None) -> str | None:
    """The reason when the agent reported ``RESULT: BLOCKED <reason>``."""
    payload = parse_result_line(result_line)
    if payload is None:
        return None
    match = BLOCKED_RE.match(payload)
    return match.group("reason").strip() if match else None


def decide_implementer_outcome(
    *,
    exit_code: int,
    timed_out: bool,
    result_line: str | None,
    pr_found: bool,
) -> OutcomeDecision:
    """Apply the PLAN section 4 rules for implement/fix runs."""
    if timed_out:
        return OutcomeDecision(run_state=TIMEOUT, outcome="timeout", crash_retry=True)
    if exit_code != 0:
        return OutcomeDecision(
            run_state=FAILED, outcome="nonzero_exit", crash_retry=True
        )
    reason = blocked_reason(result_line)
    if reason is not None:
        return OutcomeDecision(
            run_state=SUCCEEDED,
            outcome="blocked",
            transition="on_hold",
            comment=blocked_comment(reason),
        )
    if pr_found:
        return OutcomeDecision(
            run_state=SUCCEEDED,
            outcome="ready_for_review",
            transition="ready_for_review",
        )
    return OutcomeDecision(run_state=FAILED, outcome="no_pr", crash_retry=True)


def blocked_comment(reason: str) -> str:
    """Engine comment posted when the agent reports a genuine blocker."""
    return f"ENGINE: blocked\n\n{reason}"


def escalation_comment(
    *,
    outcome: str,
    exit_code: int | None = None,
    tail_lines: list[str] | None = None,
    detail: str | None = None,
) -> str:
    """Engine comment posted when the crash retry cap is reached."""
    lines = [
        "ENGINE: escalated",
        "",
        f"The agent run failed ({outcome}) and the crash retry cap is reached.",
        "Putting the ticket on hold for a human.",
    ]
    if detail:
        lines.extend(["", f"Failure detail: {detail}"])
    if exit_code is not None:
        lines.extend(["", f"Exit code: {exit_code}"])
    if tail_lines is not None:
        lines.extend(["", f"Last {len(tail_lines)} lines of the run log:"])
        lines.extend(tail_lines if tail_lines else ["(run log is empty)"])
    return "\n".join(lines)


def read_tail(path: Path | None, max_lines: int = ESCALATION_TAIL_LINES) -> list[str]:
    """The last *max_lines* lines of a log file (empty list if missing)."""
    if path is None or not path.is_file():
        return []
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    return lines[-max_lines:]
