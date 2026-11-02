"""Outcome verification for implementer, fix and reviewer runs.

Implement and fix runs (M6):

* ``RESULT: BLOCKED <reason>`` -> blocked: the work package moves to On
  Hold and the reason is posted as an engine comment.
* Otherwise an open pull request for the branch (external check via GitHub)
  -> success: the work package moves to Ready for Review. An open PR is
  authoritative, exactly like a merged PR for reviews: a run that was
  killed or timed out after it opened the PR has delivered the work, so it
  is not retried.
* No PR (or non-zero exit, or timeout without a PR) -> failure; the crash
  retry counter increments and a resume run is dispatched on a later poll
  (M7). When the cap (``MAX_CRASH_RETRIES``) is reached the work package
  moves to On Hold with an engine comment (escalation).

Review runs (M5):

* A merged PR is authoritative: approved, the work package moves to Closed.
* ``RESULT: REJECTED`` **and** a comment by the reviewer user posted since the
  run started -> rejected: the work package moves to Rejected (unless the
  rejection cap is exceeded, see M6 below). The reviewer wrote the comment,
  so the engine does not duplicate it.
* ``RESULT: REJECTED`` without such a comment -> failure (the rejection is not
  verifiable), so it counts as a crash retry.
* Anything else (non-zero exit, timeout, no merged PR) -> failure, crash
  retry; the work package stays In Testing for a later review run.

Rejection loop (M6): the rejection count is read from the work package
history (status changes to Rejected). When the next rejection would exceed
``MAX_REJECTIONS`` the work package moves to On Hold and the engine posts a
comment summarising the cap instead of applying ``Rejected``.
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

#: ``REJECTED [<reason>]`` (case-insensitive keyword).
REJECTED_RE = re.compile(r"^REJECTED\b\s*(?P<reason>.*)$", re.IGNORECASE)

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


def rejection_reason(result_line: str | None) -> str | None:
    """The (possibly empty) reason when the reviewer reported ``REJECTED``."""
    payload = parse_result_line(result_line)
    if payload is None:
        return None
    match = REJECTED_RE.match(payload)
    if match is None:
        return None
    return match.group("reason").strip()


def is_rejection(result_line: str | None) -> bool:
    """Whether the run reported ``RESULT: REJECTED`` (with or without reason)."""
    return rejection_reason(result_line) is not None


def decide_implementer_outcome(
    *,
    exit_code: int,
    timed_out: bool,
    result_line: str | None,
    pr_found: bool,
) -> OutcomeDecision:
    """Apply the rules for implement/fix runs.

    Order matters:

    1. An explicit ``RESULT: BLOCKED`` wins: the agent needs a human even if
       it happened to leave an open PR behind.
    2. An open PR is authoritative (like a merged PR for a review). A run
       that was killed or timed out after the PR was opened must not be
       retried: the deliverable exists, so the work package moves to Ready
       for Review.
    3. Only without a PR do a timeout or a non-zero exit count as a crash
       retry.
    """
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
    if timed_out:
        return OutcomeDecision(run_state=TIMEOUT, outcome="timeout", crash_retry=True)
    if exit_code != 0:
        return OutcomeDecision(
            run_state=FAILED, outcome="nonzero_exit", crash_retry=True
        )
    return OutcomeDecision(run_state=FAILED, outcome="no_pr", crash_retry=True)


def decide_reviewer_outcome(
    *,
    exit_code: int,
    timed_out: bool,
    result_line: str | None,
    pr_merged: bool,
    rejection_comment_found: bool,
) -> OutcomeDecision:
    """Apply the rules for review runs.

    External verification is authoritative: a merged PR means approval even
    when the agent exited non-zero (it may have been killed right after the
    merge). A rejection is only accepted when the reviewer posted a comment
    that explains it, otherwise the run is a failure.
    """
    if pr_merged:
        return OutcomeDecision(
            run_state=SUCCEEDED, outcome="approved", transition="closed"
        )
    reason = blocked_reason(result_line)
    if reason is not None:
        return OutcomeDecision(
            run_state=SUCCEEDED,
            outcome="blocked",
            transition="on_hold",
            comment=blocked_comment(reason),
        )
    if timed_out:
        return OutcomeDecision(run_state=TIMEOUT, outcome="timeout", crash_retry=True)
    if exit_code != 0:
        return OutcomeDecision(
            run_state=FAILED, outcome="nonzero_exit", crash_retry=True
        )
    if is_rejection(result_line):
        if rejection_comment_found:
            return OutcomeDecision(
                run_state=SUCCEEDED, outcome="rejected", transition="rejected"
            )
        return OutcomeDecision(
            run_state=FAILED,
            outcome="rejected_without_comment",
            crash_retry=True,
        )
    return OutcomeDecision(run_state=FAILED, outcome="not_merged", crash_retry=True)


def no_pr_comment(branch: str) -> str:
    """Engine comment for ``Ready for Review`` without a PR for the branch."""
    return (
        "ENGINE: no PR found\n\n"
        f"No pull request was found for branch `{branch}`, so the review cannot "
        "start. Putting the ticket on hold for a human."
    )


def already_open_pr_comment(branch: str, pr_url: str) -> str:
    """Engine comment for a resume run skipped because the PR is already open."""
    return (
        "ENGINE: resume skipped (PR already open)\n\n"
        f"Branch `{branch}` already has an open pull request: {pr_url}\n\n"
        "The previous run was interrupted after the pull request was opened, "
        "so the resume run was skipped instead of repeating the work. Moving "
        "the ticket to Ready for Review."
    )


def blocked_comment(reason: str) -> str:
    """Engine comment posted when the agent reports a genuine blocker."""
    return f"ENGINE: blocked\n\n{reason}"


def blocked_by_relations_comment(blocker_labels: list[str]) -> str:
    """Engine comment posted when OpenProject relations block a New ticket."""
    blockers = ", ".join(blocker_labels)
    return (
        "ENGINE: blocked by dependency\n\n"
        f"This ticket is blocked by unfinished work package(s): {blockers}.\n\n"
        "The engine moved it to Blocked. It will move back to In Progress and "
        "be assigned to the configured agent automatically once every blocker "
        "is Closed."
    )


def rejection_cap_comment(*, rejection_number: int, max_rejections: int) -> str:
    """Engine comment posted when a rejection would exceed the cap.

    With ``MAX_REJECTIONS=3`` the third rejection is still applied (the cap is
    exceeded, not reached); the fourth moves the work package to On Hold so a
    human can intervene instead of another automatic fix pass.
    """
    return (
        "ENGINE: rejection cap reached\n\n"
        f"This review would be rejection {rejection_number} of the ticket, "
        f"which exceeds MAX_REJECTIONS={max_rejections}. Putting the ticket on "
        "hold for a human instead of starting another fix pass."
    )


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
