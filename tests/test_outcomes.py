"""Tests for outcome verification (PLAN sections 4 and 10)."""

from pathlib import Path

from op_agent_engine.outcomes import (
    blocked_comment,
    blocked_reason,
    decide_implementer_outcome,
    escalation_comment,
    parse_result_line,
    read_tail,
)
from op_agent_engine.store import FAILED, SUCCEEDED, TIMEOUT

# -- decide_implementer_outcome -------------------------------------------


def test_timeout_is_crash_retry() -> None:
    decision = decide_implementer_outcome(
        exit_code=-15, timed_out=True, result_line="RESULT: ok", pr_found=True
    )
    assert decision.run_state == TIMEOUT
    assert decision.outcome == "timeout"
    assert decision.crash_retry is True
    assert decision.transition is None
    assert decision.comment is None


def test_nonzero_exit_is_crash_retry() -> None:
    decision = decide_implementer_outcome(
        exit_code=1, timed_out=False, result_line=None, pr_found=True
    )
    assert decision.run_state == FAILED
    assert decision.outcome == "nonzero_exit"
    assert decision.crash_retry is True
    assert decision.transition is None


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
