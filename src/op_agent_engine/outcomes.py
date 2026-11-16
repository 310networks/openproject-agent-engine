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
* ``RESULT: WAITING_CI`` -> a checkpoint, not a failure: the agent finished
  its work for now and the only remaining input is CI. The ticket stays In
  Progress, the crash counter is not touched, and the resume is held until
  the commit's checks settle (the engine polls them itself, so waiting costs
  no model time). A draft PR with no new commits is treated the same way
  while checks are still running; once they settle that run is an ordinary
  ``no_pr`` failure.

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
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from op_agent_engine.store import FAILED, SUCCEEDED, TIMEOUT, Run

#: Matches a ``RESULT:`` line anywhere in the payload (the line itself is
#: already extracted from the log tail by the runner).
RESULT_RE = re.compile(r"^\s*RESULT:\s*(?P<payload>.+)$")

#: ``BLOCKED <one-line reason>`` (case-insensitive keyword).
BLOCKED_RE = re.compile(r"^BLOCKED\s+(?P<reason>.+)$", re.IGNORECASE)

#: ``REJECTED [<reason>]`` (case-insensitive keyword).
REJECTED_RE = re.compile(r"^REJECTED\b\s*(?P<reason>.*)$", re.IGNORECASE)

#: ``WAITING_CI [<detail>]`` (case-insensitive keyword). The detail is usually
#: the workflow run or PR URL, so the engine comment can point at it.
WAITING_CI_RE = re.compile(r"^WAITING_CI\b\s*(?P<detail>.*)$", re.IGNORECASE)

#: How many tail lines of the run log go into an escalation comment.
ESCALATION_TAIL_LINES = 40
DEFAULT_COMMENT_MAX_BYTES = 64 * 1024
DEFAULT_COMMENT_LINE_MAX_BYTES = 2048


def bound_comment(
    text: str,
    *,
    max_bytes: int = DEFAULT_COMMENT_MAX_BYTES,
    line_max_bytes: int = DEFAULT_COMMENT_LINE_MAX_BYTES,
) -> str:
    """Bound UTF-8 text, preserving the head and tail with explicit markers.

    Limits apply to the raw comment, not its JSON envelope. The 64 KiB default
    leaves room even for JSON escaping under a proxy's usual 1 MiB body limit.
    """
    if max_bytes < 256 or line_max_bytes < 128:
        raise ValueError("comment limits must be at least 256/128 bytes")
    line_max_bytes = min(line_max_bytes, (max_bytes - 80) // 2 - 1)
    lines = []
    for line in text.split("\n"):
        encoded = line.encode("utf-8")
        if len(encoded) > line_max_bytes:
            marker = " … truncated 1 lines … (line shortened) "
            budget = (line_max_bytes - len(marker.encode("utf-8"))) // 2
            line = (
                encoded[:budget].decode("utf-8", errors="ignore")
                + marker
                + encoded[-budget:].decode("utf-8", errors="ignore")
            )
        lines.append(line)
    bounded = "\n".join(lines)
    if len(bounded.encode("utf-8")) <= max_bytes:
        return bounded

    # Reserve enough space for the omission marker and both newlines. Select
    # complete lines from each end so a JSON event never consumes the budget.
    budget = (max_bytes - 80) // 2
    head: list[str] = []
    tail: list[str] = []
    for source, selected in ((lines, head), (list(reversed(lines)), tail)):
        used = 0
        for line in source:
            size = len(line.encode("utf-8")) + 1
            if used + size > budget:
                break
            selected.append(line)
            used += size
    omitted = len(lines) - len(head) - len(tail)
    return "\n".join([*head, f"… truncated {omitted} lines …", *reversed(tail)])


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


def is_waiting_ci(result_line: str | None) -> bool:
    """Whether the run reported ``RESULT: WAITING_CI`` (detail optional)."""
    payload = parse_result_line(result_line)
    if payload is None:
        return False
    return WAITING_CI_RE.match(payload) is not None


def waiting_ci_detail(result_line: str | None) -> str | None:
    """The optional detail after ``WAITING_CI`` (e.g. a workflow run URL)."""
    payload = parse_result_line(result_line)
    if payload is None:
        return None
    match = WAITING_CI_RE.match(payload)
    if match is None:
        return None
    return match.group("detail").strip() or None


#: Outcome for a run that exited 0, printed nothing and left no PR.
NO_RESULT_LINE = "no_result_line"

#: Why a silent run is not an ordinary "no PR" failure, used in the escalation
#: comment. The agent CLI can exit 0 without writing a byte (pi print mode
#: writes only the final assistant message), so the log says nothing about
#: what the agent did or why it stopped.
NO_RESULT_LINE_NOTE = (
    "The run exited 0 but wrote nothing at all to stdout or stderr, so there "
    "is no RESULT line and no evidence of what the agent did. This is a known "
    "agent-CLI failure mode, not a missing pull request: the run budget was "
    "spent without producing a report."
)


def decide_implementer_outcome(
    *,
    exit_code: int,
    timed_out: bool,
    result_line: str | None,
    pr_found: bool,
    silent: bool = False,
    progress: bool = False,
    draft_pr_open: bool = False,
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
    4. A successful run with a RESULT line and verified clean head movement
       is progress, not finished delivery; it stays In Progress without a retry.
    5. ``RESULT: WAITING_CI`` is a checkpoint: the remaining input is CI, so
       the run must not spend the crash budget. The dispatcher decides
       whether to hold the resume (checks still running) or resume at once.
    6. A clean, successful run that pushed nothing while a draft PR is open is
       a checkpoint too -- but only while the commit's checks are running.
       The dispatcher downgrades it to ``no_pr`` once they settle, because
       then the agent is not waiting for anything.
    7. A run that exited 0 without writing anything and without a PR is
       ``no_result_line``, not ``no_pr``: the engine cannot tell whether the
       agent did nothing or the agent CLI swallowed its output.
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
    if silent:
        return OutcomeDecision(
            run_state=FAILED, outcome=NO_RESULT_LINE, crash_retry=True
        )
    if is_waiting_ci(result_line):
        return OutcomeDecision(run_state=SUCCEEDED, outcome="waiting_ci")
    if progress and result_line is not None:
        return OutcomeDecision(run_state=SUCCEEDED, outcome="progress_not_finished")
    if draft_pr_open and result_line is not None:
        return OutcomeDecision(
            run_state=SUCCEEDED, outcome="checkpoint_no_progress"
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
    if is_waiting_ci(result_line):
        # The reviewer is waiting for the PR's checks before it can decide.
        # Same checkpoint rules as an implementer: stays In Testing, no crash
        # retry, and the engine holds the next review until the checks settle.
        return OutcomeDecision(run_state=SUCCEEDED, outcome="waiting_ci")
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


def rejection_is_outstanding(last_review: Run | None, last_code: Run | None) -> bool:
    """True when the reviewer's rejection came after the newest code run.

    Used by the resume path: an open pull request only proves that *some*
    work reached review, not that the latest rejection was addressed. When
    the last review rejected after the last successful implement/fix run
    (the case after a fix run was killed), resuming must run the agent again
    instead of declaring the work package ready for review.

    Timestamps are compared, not read from comments: the engine knows both
    (``runs.finished_at`` for the review and for the code run).
    """
    if last_review is None or last_review.outcome != "rejected":
        return False
    if last_review.finished_at is None:
        return False
    if last_code is None or last_code.finished_at is None:
        return True
    return _parse_time(last_code.finished_at) <= _parse_time(last_review.finished_at)


def _parse_time(value: str) -> datetime:
    """Parse an ISO timestamp from the store, tolerating a trailing ``Z``."""
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


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


def checkpoint_comment(
    *,
    outcome: str,
    head_sha: str | None,
    pr_url: str | None,
    detail: str | None = None,
) -> str:
    """Engine comment for a checkpoint run that stays In Progress.

    Kept to a few lines: a long task produces several of these and they must
    not bury the agent's own ticket report.
    """
    lines = ["ENGINE: checkpoint", ""]
    if outcome == "waiting_ci":
        lines.append(
            "The agent finished what it can do for now and is waiting for CI."
        )
    elif outcome == "checkpoint_no_progress":
        lines.append(
            "The agent verified the current state and had nothing new to push "
            "while its CI checks are still running."
        )
    else:
        lines.append(
            "The agent reported progress and handed the branch back for the "
            "next run."
        )
    if pr_url:
        lines.append(f"Pull request: {pr_url}")
    if head_sha:
        lines.append(f"Head: {head_sha[:12]}")
    if detail:
        lines.append(f"Waiting on: {detail}")
    lines.extend(
        [
            "",
            "The ticket keeps its current status; the next run continues from "
            "this branch.",
        ]
    )
    return "\n".join(lines)


def waiting_ci_hold_comment(
    *,
    head_sha: str | None,
    checks_detail: str,
    ready_at: datetime | None,
    detail: str | None = None,
) -> str:
    """Engine comment for a checkpoint whose resume is held for CI.

    The engine polls the commit's checks itself; no agent run is spent while
    they run.
    """
    lines = [
        "ENGINE: waiting on CI",
        "",
        f"Checks for {head_sha[:12] if head_sha else 'the current head'}: "
        f"{checks_detail}.",
    ]
    if detail:
        lines.append(f"Waiting on: {detail}")
    if ready_at is not None:
        lines.append(
            "The next run is held until they finish (or until "
            f"{ready_at.isoformat(timespec='seconds')}, whichever comes first)."
        )
    lines.extend(
        [
            "",
            "The ticket keeps its current status. No agent run is started while "
            "the checks are running.",
        ]
    )
    return "\n".join(lines)


def checkpoint_wait_cap_comment(
    *, waits: int, head_sha: str | None, checks_detail: str | None
) -> str:
    """Engine comment when the checkpoint wait budget is exhausted.

    Distinct from a crash escalation: the runs themselves were healthy, but
    the ticket made no progress across ``MAX_CHECKPOINT_WAITS`` checkpoints.
    """
    lines = [
        "ENGINE: checkpoint wait cap reached",
        "",
        f"The last {waits} runs ended as checkpoints without delivering a "
        "ready pull request, so the ticket is on hold for a human.",
    ]
    if head_sha:
        lines.append(f"Head: {head_sha[:12]}")
    if checks_detail:
        lines.append(f"Checks: {checks_detail}")
    lines.extend(
        [
            "",
            "Move the ticket back to In Progress to let the agent continue.",
        ]
    )
    return "\n".join(lines)


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
    artefact_path: str | None = None,
    note: str | None = None,
    max_bytes: int = DEFAULT_COMMENT_MAX_BYTES,
    line_max_bytes: int = DEFAULT_COMMENT_LINE_MAX_BYTES,
) -> str:
    """Engine comment posted when the crash retry cap is reached.

    ``artefact_path`` names the captured state of the failed run's worktree
    (``runs/<runId>/worktree``), so a human can find what the agent wrote even
    when it never committed or opened a PR.
    """
    lines = [
        "ENGINE: escalated",
        "",
        f"The agent run failed ({outcome}) and the crash retry cap is reached.",
        "Putting the ticket on hold for a human.",
    ]
    if outcome == "no_pr":
        lines.extend([
            "",
            "No progress: no ready PR or verified clean head advancement. "
            "Progress, not finished runs do not consume the crash retry budget.",
        ])
    if note:
        lines.extend(["", note])
    if detail:
        lines.extend(["", f"Failure detail: {detail}"])
    if exit_code is not None:
        lines.extend(["", f"Exit code: {exit_code}"])
    if artefact_path:
        lines.extend(
            [
                "",
                f"The failed run's worktree state was captured in: {artefact_path}",
                "It holds `git status`, the unstaged/staged patches, the "
                "unpushed commits and the untracked files.",
            ]
        )
    if tail_lines is not None:
        lines.extend(["", f"Last {len(tail_lines)} lines of the run log:"])
        lines.extend(tail_lines if tail_lines else ["(run log is empty)"])
    return bound_comment(
        "\n".join(lines), max_bytes=max_bytes, line_max_bytes=line_max_bytes
    )


def read_tail(path: Path | None, max_lines: int = ESCALATION_TAIL_LINES) -> list[str]:
    """The last *max_lines* lines of a log file (empty list if missing)."""
    if path is None or not path.is_file():
        return []
    if max_lines <= 0:
        return []
    with path.open(encoding="utf-8", errors="replace") as stream:
        return [line.rstrip("\r\n") for line in deque(stream, maxlen=max_lines)]
