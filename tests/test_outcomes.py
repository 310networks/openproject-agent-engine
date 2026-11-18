"""Tests for outcome verification."""

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from op_agent_engine.outcomes import (
    NO_RESULT_LINE_NOTE,
    already_open_pr_comment,
    blocked_comment,
    blocked_reason,
    bound_comment,
    checkpoint_comment,
    checkpoint_wait_cap_comment,
    decide_implementer_outcome,
    decide_reviewer_outcome,
    escalation_comment,
    is_rejection,
    is_waiting_ci,
    no_pr_comment,
    parse_result_line,
    read_tail,
    rejection_is_outstanding,
    rejection_reason,
    waiting_ci_detail,
    waiting_ci_hold_comment,
)
from op_agent_engine.store import FAILED, SUCCEEDED, TIMEOUT, Run


@pytest.mark.parametrize(("ready_pr", "advanced", "expected", "retry"), [
    (False, True, "progress_not_finished", False),
    (False, False, "no_pr", True),
    (True, True, "ready_for_review", False),
    (True, False, "ready_for_review", False),
])
def test_partial_progress(ready_pr: bool, advanced: bool, expected: str, retry: bool) -> None:
    decision = decide_implementer_outcome(
        exit_code=0, timed_out=False, result_line="RESULT: PR_OPENED https://example.org/pr",
        pr_found=ready_pr, progress=advanced,
    )
    assert decision.outcome == expected
    assert decision.crash_retry is retry
    assert decision.transition == ("ready_for_review" if ready_pr else None)


@pytest.mark.parametrize(("exit_code", "timed_out", "result_line", "expected"), [
    (1, False, "RESULT: PR_OPENED url", "nonzero_exit"),
    (0, True, "RESULT: PR_OPENED url", "timeout"),
    (0, False, None, "no_pr"),
    (0, False, "RESULT: BLOCKED needs help", "blocked"),
])
def test_progress_does_not_mask_failure(
    exit_code: int, timed_out: bool, result_line: str | None, expected: str,
) -> None:
    decision = decide_implementer_outcome(
        exit_code=exit_code, timed_out=timed_out, result_line=result_line,
        pr_found=False, progress=True,
    )
    assert decision.outcome == expected


# -- decide_implementer_outcome -------------------------------------------


def test_restart_comment_cap_is_utf8_byte_bounded() -> None:
    short = "ENGINE: run interrupted"
    assert bound_comment(short, max_bytes=1024) == short
    text = bound_comment("ENGINE: " + "🙂" * 300000, max_bytes=1024)
    assert len(text.encode("utf-8")) <= 1024
    assert "truncated" in text


def test_timeout_without_pr_is_crash_retry() -> None:
    decision = decide_implementer_outcome(
        exit_code=-15, timed_out=True, result_line="RESULT: ok", pr_found=False
    )
    assert decision.run_state == TIMEOUT
    assert decision.outcome == "timeout"
    assert decision.crash_retry is True
    assert decision.transition is None
    assert decision.comment is None


def test_nonzero_exit_without_pr_is_crash_retry() -> None:
    decision = decide_implementer_outcome(
        exit_code=1, timed_out=False, result_line=None, pr_found=False
    )
    assert decision.run_state == FAILED
    assert decision.outcome == "nonzero_exit"
    assert decision.crash_retry is True
    assert decision.transition is None


def test_timeout_after_the_pr_was_opened_is_ready_for_review() -> None:
    """An open PR is authoritative: a hung run that finished the work is not retried."""
    decision = decide_implementer_outcome(
        exit_code=-15, timed_out=True, result_line=None, pr_found=True
    )
    assert decision.run_state == SUCCEEDED
    assert decision.outcome == "ready_for_review"
    assert decision.transition == "ready_for_review"
    assert decision.crash_retry is False


def test_nonzero_exit_after_the_pr_was_opened_is_ready_for_review() -> None:
    decision = decide_implementer_outcome(
        exit_code=1, timed_out=False, result_line=None, pr_found=True
    )
    assert decision.run_state == SUCCEEDED
    assert decision.outcome == "ready_for_review"
    assert decision.transition == "ready_for_review"
    assert decision.crash_retry is False


def test_blocked_moves_to_on_hold_with_comment() -> None:
    decision = decide_implementer_outcome(
        exit_code=0,
        timed_out=False,
        result_line="RESULT: BLOCKED need access to the staging DB",
        pr_found=False,
    )
    assert decision.run_state == SUCCEEDED
    assert decision.outcome == "blocked"
    assert decision.transition == "on_hold"
    assert decision.comment == "ENGINE: blocked\n\nneed access to the staging DB"
    assert decision.crash_retry is False


def test_blocked_wins_over_pr() -> None:
    """A blocked result is reported even if a PR happens to exist."""
    decision = decide_implementer_outcome(
        exit_code=0,
        timed_out=False,
        result_line="RESULT: blocked missing credentials",
        pr_found=True,
    )
    assert decision.outcome == "blocked"
    assert decision.transition == "on_hold"


def test_pr_found_moves_to_ready_for_review() -> None:
    decision = decide_implementer_outcome(
        exit_code=0,
        timed_out=False,
        result_line="RESULT: PR opened at https://github.com/o/r/pull/7",
        pr_found=True,
    )
    assert decision.run_state == SUCCEEDED
    assert decision.outcome == "ready_for_review"
    assert decision.transition == "ready_for_review"
    assert decision.comment is None
    assert decision.crash_retry is False


def test_no_pr_is_crash_retry() -> None:
    decision = decide_implementer_outcome(
        exit_code=0, timed_out=False, result_line=None, pr_found=False
    )
    assert decision.run_state == FAILED
    assert decision.outcome == "no_pr"
    assert decision.crash_retry is True
    assert decision.transition is None


# -- decide_reviewer_outcome (M5) ------------------------------------------


def test_review_merged_pr_is_approved() -> None:
    decision = decide_reviewer_outcome(
        exit_code=0,
        timed_out=False,
        result_line="RESULT: APPROVED",
        pr_merged=True,
        rejection_comment_found=False,
    )
    assert decision.run_state == SUCCEEDED
    assert decision.outcome == "approved"
    assert decision.transition == "closed"
    assert decision.comment is None
    assert decision.crash_retry is False


def test_review_merged_pr_wins_over_a_bad_exit_code() -> None:
    """External verification is authoritative."""
    decision = decide_reviewer_outcome(
        exit_code=-9,
        timed_out=True,
        result_line=None,
        pr_merged=True,
        rejection_comment_found=False,
    )
    assert decision.outcome == "approved"
    assert decision.transition == "closed"


def test_review_rejection_with_comment_moves_to_rejected() -> None:
    decision = decide_reviewer_outcome(
        exit_code=0,
        timed_out=False,
        result_line="RESULT: REJECTED the tests are missing",
        pr_merged=False,
        rejection_comment_found=True,
    )
    assert decision.run_state == SUCCEEDED
    assert decision.outcome == "rejected"
    assert decision.transition == "rejected"
    assert decision.comment is None
    assert decision.crash_retry is False


def test_review_rejection_without_comment_is_a_failure() -> None:
    decision = decide_reviewer_outcome(
        exit_code=0,
        timed_out=False,
        result_line="RESULT: REJECTED the tests are missing",
        pr_merged=False,
        rejection_comment_found=False,
    )
    assert decision.run_state == FAILED
    assert decision.outcome == "rejected_without_comment"
    assert decision.transition is None
    assert decision.crash_retry is True


def test_review_blocked_moves_to_on_hold_with_comment() -> None:
    decision = decide_reviewer_outcome(
        exit_code=0,
        timed_out=False,
        result_line="RESULT: BLOCKED no access to the CI logs",
        pr_merged=False,
        rejection_comment_found=False,
    )
    assert decision.run_state == SUCCEEDED
    assert decision.outcome == "blocked"
    assert decision.transition == "on_hold"
    assert decision.comment == "ENGINE: blocked\n\nno access to the CI logs"


def test_review_timeout_and_nonzero_exit_are_crash_retries() -> None:
    timed_out = decide_reviewer_outcome(
        exit_code=-15,
        timed_out=True,
        result_line=None,
        pr_merged=False,
        rejection_comment_found=False,
    )
    assert timed_out.run_state == TIMEOUT
    assert timed_out.outcome == "timeout"
    assert timed_out.crash_retry is True

    nonzero = decide_reviewer_outcome(
        exit_code=1,
        timed_out=False,
        result_line=None,
        pr_merged=False,
        rejection_comment_found=False,
    )
    assert nonzero.run_state == FAILED
    assert nonzero.outcome == "nonzero_exit"
    assert nonzero.crash_retry is True


def test_review_not_merged_without_a_result_is_a_crash_retry() -> None:
    decision = decide_reviewer_outcome(
        exit_code=0,
        timed_out=False,
        result_line="RESULT: ok",
        pr_merged=False,
        rejection_comment_found=False,
    )
    assert decision.run_state == FAILED
    assert decision.outcome == "not_merged"
    assert decision.crash_retry is True


# -- rejection parsing -------------------------------------------------------


def test_rejection_reason_parsing() -> None:
    assert rejection_reason("RESULT: REJECTED the tests are missing") == (
        "the tests are missing"
    )
    assert rejection_reason("RESULT: rejected") == ""
    assert rejection_reason("RESULT: blocked nope") is None
    assert rejection_reason(None) is None


def test_is_rejection() -> None:
    assert is_rejection("RESULT: REJECTED nope") is True
    assert is_rejection("RESULT: REJECTED") is True
    assert is_rejection("RESULT: REJECTEDNESS") is False
    assert is_rejection("RESULT: APPROVED") is False


def test_no_pr_comment_names_the_branch() -> None:
    comment = no_pr_comment("op-4-100")
    assert comment.startswith("ENGINE: no PR found")
    assert "op-4-100" in comment


# -- result line parsing ----------------------------------------------------


def test_parse_result_line_returns_payload() -> None:
    assert parse_result_line("RESULT: PR opened") == "PR opened"
    assert parse_result_line("  RESULT:   spaced out  ") == "spaced out"


def test_parse_result_line_rejects_garbage() -> None:
    assert parse_result_line(None) is None
    assert parse_result_line("no result line here") is None
    assert parse_result_line("RESULT:") is None


def test_blocked_reason() -> None:
    assert blocked_reason("RESULT: BLOCKED something broke") == "something broke"
    assert blocked_reason("RESULT: blocked lowercase works") == "lowercase works"
    assert blocked_reason("RESULT: PR opened") is None
    assert blocked_reason(None) is None


# -- comments -----------------------------------------------------------------


def test_blocked_comment_shape() -> None:
    comment = blocked_comment("the repo is gone")
    assert comment.startswith("ENGINE:")
    assert "the repo is gone" in comment


def test_escalation_comment_includes_outcome_and_tail() -> None:
    comment = escalation_comment(
        outcome="timeout",
        exit_code=-15,
        tail_lines=["line one", "line two"],
        detail="run timed out after 3600s",
    )
    assert comment.startswith("ENGINE: escalated")
    assert "timeout" in comment
    assert "Exit code: -15" in comment
    assert "run timed out after 3600s" in comment
    assert "line two" in comment


def test_escalation_comment_empty_tail() -> None:
    comment = escalation_comment(outcome="no_pr", tail_lines=[])
    assert "no_pr" in comment
    assert "(run log is empty)" in comment


# -- read_tail ------------------------------------------------------------------


def test_read_tail_returns_last_lines(tmp_path: Path) -> None:
    log = tmp_path / "stdout.log"
    log.write_text("\n".join(f"line {i}" for i in range(100)))
    assert read_tail(log, max_lines=5) == [
        "line 95",
        "line 96",
        "line 97",
        "line 98",
        "line 99",
    ]


def test_read_tail_missing_file(tmp_path: Path) -> None:
    assert read_tail(tmp_path / "nope.log") == []
    assert read_tail(None) == []


# -- already_open_pr_comment -----------------------------------------------------


def test_already_open_pr_comment_names_branch_and_pr() -> None:
    comment = already_open_pr_comment("op-4-100", "https://github.com/o/r/pull/7")
    assert comment.startswith("ENGINE: resume skipped (PR already open)")
    assert "`op-4-100`" in comment
    assert "https://github.com/o/r/pull/7" in comment


# -- rejection_is_outstanding (resume path) ----------------------------------


def make_run(
    *,
    kind: str,
    outcome: str | None,
    finished_at: str | None,
    run_id: int = 1,
) -> Run:
    return Run(
        id=run_id,
        work_package_id=100,
        project_id=4,
        agent="IMPL",
        kind=kind,
        state=SUCCEEDED,
        attempt=1,
        branch="op-4-100",
        display_id="OP#100",
        subject="demo",
        pid=None,
        pgid=None,
        proc_start=None,
        started_at="2026-11-04T07:00:00+00:00",
        finished_at=finished_at,
        exit_code=0,
        result_line="RESULT: ok",
        outcome=outcome,
        run_dir="/tmp/run",
    )


def test_rejection_after_the_last_code_run_is_outstanding() -> None:
    """WP 144: the fix run was killed, so the PR predates the rejection."""
    review = make_run(
        kind="review",
        outcome="rejected",
        finished_at="2026-11-04T07:40:16+00:00",
        run_id=2,
    )
    code = make_run(
        kind="implement",
        outcome="ready_for_review",
        finished_at="2026-11-04T07:35:00+00:00",
    )
    assert rejection_is_outstanding(review, code) is True


def test_rejection_before_the_last_code_run_is_addressed() -> None:
    review = make_run(
        kind="review",
        outcome="rejected",
        finished_at="2026-11-04T07:35:00+00:00",
    )
    code = make_run(
        kind="fix",
        outcome="ready_for_review",
        finished_at="2026-11-04T07:50:00+00:00",
        run_id=2,
    )
    assert rejection_is_outstanding(review, code) is False


def test_no_rejection_is_never_outstanding() -> None:
    review = make_run(
        kind="review",
        outcome="approved",
        finished_at="2026-11-04T07:40:16+00:00",
    )
    code = make_run(
        kind="implement",
        outcome="ready_for_review",
        finished_at="2026-11-04T07:35:00+00:00",
    )
    assert rejection_is_outstanding(review, code) is False


def test_rejection_without_any_code_run_is_outstanding() -> None:
    review = make_run(
        kind="review",
        outcome="rejected",
        finished_at="2026-11-04T07:40:16+00:00",
    )
    assert rejection_is_outstanding(review, None) is True


def test_no_review_run_is_never_outstanding() -> None:
    code = make_run(
        kind="implement",
        outcome="ready_for_review",
        finished_at="2026-11-04T07:35:00+00:00",
    )
    assert rejection_is_outstanding(None, code) is False


def test_rejection_at_the_same_instant_as_the_code_run_is_outstanding() -> None:
    """Ties resolve towards running the agent: the safer outcome."""
    review = make_run(
        kind="review", outcome="rejected", finished_at="2026-11-04T07:40:16+00:00"
    )
    code = make_run(
        kind="fix",
        outcome="ready_for_review",
        finished_at="2026-11-04T07:40:16.000001+00:00",
        run_id=2,
    )
    assert rejection_is_outstanding(review, code) is False
    same = make_run(
        kind="fix",
        outcome="ready_for_review",
        finished_at="2026-11-04T07:40:16+00:00",
        run_id=3,
    )
    assert rejection_is_outstanding(review, same) is True


# -- a silent run is its own outcome (WP 366) --------------------------------


def test_silent_run_without_pr_is_no_result_line() -> None:
    decision = decide_implementer_outcome(
        exit_code=0,
        timed_out=False,
        result_line=None,
        pr_found=False,
        silent=True,
    )
    assert decision.run_state == FAILED
    assert decision.outcome == "no_result_line"
    assert decision.crash_retry is True
    # The comment only lands at the cap; the decision itself stays quiet.
    assert decision.comment is None


def test_silent_run_with_a_pr_is_still_delivered() -> None:
    decision = decide_implementer_outcome(
        exit_code=0,
        timed_out=False,
        result_line=None,
        pr_found=True,
        silent=True,
    )
    assert decision.outcome == "ready_for_review"


def test_silent_timeout_and_nonzero_exit_keep_their_outcomes() -> None:
    """"Silent" only renames the exit-0/no-PR case, nothing else."""
    timed_out = decide_implementer_outcome(
        exit_code=-15, timed_out=True, result_line=None, pr_found=False, silent=True
    )
    assert timed_out.outcome == "timeout"
    nonzero = decide_implementer_outcome(
        exit_code=1, timed_out=False, result_line=None, pr_found=False, silent=True
    )
    assert nonzero.outcome == "nonzero_exit"


def test_a_run_with_output_is_still_no_pr() -> None:
    decision = decide_implementer_outcome(
        exit_code=0,
        timed_out=False,
        result_line=None,
        pr_found=False,
        silent=False,
    )
    assert decision.outcome == "no_pr"


def test_escalation_comment_carries_an_optional_note() -> None:
    comment = escalation_comment(
        outcome="no_result_line",
        exit_code=0,
        tail_lines=[],
        note=NO_RESULT_LINE_NOTE,
    )
    assert "(no_result_line)" in comment
    assert NO_RESULT_LINE_NOTE in comment
    assert "(run log is empty)" in comment
    without = escalation_comment(outcome="no_pr", exit_code=0, tail_lines=[])
    assert "wrote nothing" not in without
    assert "No progress" in without
    assert "Progress, not finished" in without


@pytest.mark.parametrize("limit", [1024, 8192, 65536])
def test_escalation_bounds_large_unicode_lines(limit: int) -> None:
    comment = escalation_comment(
        outcome="no_pr",
        detail="start" + "界" * 350000 + "end",
        tail_lines=[f"head{i}" + "界" * 20000 + f"tail{i}" for i in range(40)],
        max_bytes=limit,
        line_max_bytes=512,
    )
    assert len(comment.encode("utf-8")) <= limit
    assert max(len(line.encode("utf-8")) for line in comment.splitlines()) <= 512
    assert "… truncated " in comment
    assert " lines …" in comment
    assert comment.startswith("ENGINE: escalated")
    assert comment.endswith("tail39")


def test_escalation_shortens_one_line_retaining_both_ends() -> None:
    comment = escalation_comment(
        outcome="failure", detail="HEAD" + "x" * 1000000 + "TAIL",
        max_bytes=4096, line_max_bytes=256,
    )
    assert "HEAD" in comment
    assert "TAIL" in comment
    assert "truncated 1 lines … (line shortened)" in comment
    assert len(comment.encode("utf-8")) < 4096


def test_comment_json_envelope_stays_below_default_proxy_limit() -> None:
    # Control characters expand sixfold in JSON; non-ASCII may be escaped too.
    comment = bound_comment(
        ("\x00" * 20000 + "界" * 20000 + "\n") * 40,
        max_bytes=131072, line_max_bytes=16384,
    )
    assert len(comment.encode("utf-8")) <= 131072
    assert len(json.dumps({"comment": {"raw": comment}}).encode("utf-8")) < 1024 * 1024


def test_comment_omission_marker_counts_dropped_lines() -> None:
    lines = [f"{i:03d}: " + "x" * 95 for i in range(100)]
    comment = bound_comment("\n".join(lines), max_bytes=1024, line_max_bytes=128)
    retained = [line for line in comment.splitlines() if line in lines]
    assert f"… truncated {100 - len(retained)} lines …" in comment
    assert comment.startswith(lines[0])
    assert comment.endswith(lines[-1])


def test_comment_under_limit_is_unchanged() -> None:
    text = "ENGINE: blocked\n\nMissing data is not zero. 界\n"
    assert bound_comment(text) == text


# -- checkpoints and CI waits -------------------------------------------------


def test_waiting_ci_is_a_checkpoint_not_a_failure() -> None:
    decision = decide_implementer_outcome(
        exit_code=0,
        timed_out=False,
        result_line="RESULT: WAITING_CI https://github.com/acme/game/actions/runs/1",
        pr_found=False,
    )
    assert decision.outcome == "waiting_ci"
    assert decision.run_state == SUCCEEDED
    assert decision.transition is None
    assert decision.crash_retry is False


def test_waiting_ci_does_not_override_a_ready_pr_or_a_blocker() -> None:
    ready = decide_implementer_outcome(
        exit_code=0, timed_out=False,
        result_line="RESULT: WAITING_CI url", pr_found=True,
    )
    assert ready.outcome == "ready_for_review"
    blocked = decide_implementer_outcome(
        exit_code=0, timed_out=False,
        result_line="RESULT: BLOCKED need access", pr_found=False,
    )
    assert blocked.outcome == "blocked"


def test_a_failed_run_is_never_a_checkpoint() -> None:
    nonzero = decide_implementer_outcome(
        exit_code=1, timed_out=False,
        result_line="RESULT: WAITING_CI url", pr_found=False,
    )
    assert nonzero.outcome == "nonzero_exit"
    assert nonzero.crash_retry is True
    timed_out = decide_implementer_outcome(
        exit_code=0, timed_out=True,
        result_line="RESULT: WAITING_CI url", pr_found=False,
    )
    assert timed_out.outcome == "timeout"


def test_a_draft_pr_without_a_push_is_a_checkpoint_only_with_a_result() -> None:
    waiting = decide_implementer_outcome(
        exit_code=0, timed_out=False,
        result_line="RESULT: PR_OPENED url", pr_found=False,
        draft_pr_open=True,
    )
    assert waiting.outcome == "checkpoint_no_progress"
    assert waiting.run_state == SUCCEEDED
    assert waiting.crash_retry is False

    silent = decide_implementer_outcome(
        exit_code=0, timed_out=False, result_line=None,
        pr_found=False, draft_pr_open=True,
    )
    assert silent.outcome == "no_pr"
    assert silent.crash_retry is True


def test_reviewer_waiting_ci_keeps_the_ticket_in_testing() -> None:
    decision = decide_reviewer_outcome(
        exit_code=0, timed_out=False,
        result_line="RESULT: WAITING_CI https://example.org/run/1",
        pr_merged=False, rejection_comment_found=False,
    )
    assert decision.outcome == "waiting_ci"
    assert decision.run_state == SUCCEEDED
    assert decision.transition is None
    assert decision.crash_retry is False


def test_waiting_ci_result_line_parsing() -> None:
    assert is_waiting_ci("RESULT: WAITING_CI https://example.org/run/1")
    assert is_waiting_ci("RESULT: waiting_ci")
    assert waiting_ci_detail("RESULT: WAITING_CI https://example.org/run/1") == (
        "https://example.org/run/1"
    )
    assert waiting_ci_detail("RESULT: WAITING_CI") is None
    assert not is_waiting_ci("RESULT: PR_OPENED url")
    assert not is_waiting_ci(None)


def test_checkpoint_comments_stay_short_and_specific() -> None:
    hold = waiting_ci_hold_comment(
        head_sha="abcdef1234567890",
        checks_detail="2 of 3 check(s) still running",
        ready_at=datetime(2026, 1, 1, tzinfo=UTC),
        detail="https://example.org/run/1",
    )
    assert hold.startswith("ENGINE: waiting on CI")
    assert "abcdef123456" in hold
    assert "2 of 3 check(s) still running" in hold
    assert "https://example.org/run/1" in hold
    assert "No agent run is started" in hold

    checkpoint = checkpoint_comment(
        outcome="progress_not_finished",
        head_sha="abcdef1234567890",
        pr_url="https://example.org/pr/1",
    )
    assert checkpoint.startswith("ENGINE: checkpoint")
    assert "https://example.org/pr/1" in checkpoint

    cap = checkpoint_wait_cap_comment(
        waits=9, head_sha="abcdef1234567890", checks_detail="1 of 1 check(s) failed"
    )
    assert "checkpoint wait cap reached" in cap
    assert "9 unchanged-source checkpoints" in cap
