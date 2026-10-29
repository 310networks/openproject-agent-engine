"""Tests for activity parsing (M6): comments and rejection counting."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from op_agent_engine.openproject.activities import (
    author_id,
    comment_from_activity,
    count_status_changes,
    status_targets,
)

START = datetime(2026, 2, 10, 12, 0, tzinfo=UTC)


def activity(details: list[Any], *, activity_id: int = 1) -> dict[str, Any]:
    return {
        "id": activity_id,
        "workPackageId": 100,
        "_type": "Activity",
        "details": details,
        "createdAt": START.isoformat(),
        "_links": {"user": {"href": "/api/v3/users/10"}},
    }


def test_counts_status_set_and_changed_details() -> None:
    activities = [
        activity([{"raw": "Status set to Rejected"}], activity_id=1),
        activity(
            [{"raw": "Status changed from In Testing to Rejected"}], activity_id=2
        ),
        activity([{"raw": "Status changed from Rejected to In Progress"}], activity_id=3),
        activity([{"raw": "Status set to New"}], activity_id=4),
        # Non-status details and string details are tolerated.
        activity(["Priority set to High"], activity_id=5),
        activity([{"raw": "Subject set to Rejected work"}], activity_id=6),
    ]

    assert count_status_changes(activities, "Rejected") == 2


def test_rejection_counting_ignores_case_and_partial_names() -> None:
    activities = [
        activity([{"raw": "Status changed from In Testing to Rejected"}]),
        activity([{"raw": "Status changed from In Testing to rejected"}]),
        activity([{"raw": "Status changed from In Testing to Rejected for good"}]),
    ]

    assert count_status_changes(activities, "Rejected") == 1


def test_status_targets_tolerates_malformed_details() -> None:
    activities = [
        {"id": 1, "details": None},
        {"id": 2, "details": [None, 7, {"raw": None}, {"raw": "Status set to Closed"}]},
        {"id": 3},
    ]

    assert [target for entry in activities for target in status_targets(entry)] == [
        "Closed"
    ]


def test_comment_from_activity_live_shape() -> None:
    entry = {
        "id": 42,
        "workPackageId": 100,
        "_type": "Activity::Comment",
        "comment": {"format": "markdown", "raw": "Please fix the branch."},
        "details": [],
        "createdAt": "2026-02-10T12:00:00Z",
        "_links": {"user": {"href": "/api/v3/users/12", "title": "Code Reviewer"}},
    }

    comment = comment_from_activity(entry)
    assert comment is not None
    assert comment.activity_id == 42
    assert comment.author_id == 12
    assert comment.author_title == "Code Reviewer"
    assert comment.created_at == START
    assert comment.created_at_raw == "2026-02-10T12:00:00Z"
    assert comment.text == "Please fix the branch."


def test_comment_from_activity_ignores_non_comments_and_blank_comments() -> None:
    assert comment_from_activity(activity([{"raw": "Status set to New"}])) is None
    assert (
        comment_from_activity(
            {"id": 3, "_type": "Activity::Comment", "comment": {"raw": "   "}}
        )
        is None
    )


def test_author_id_accepts_plain_and_nested_shapes() -> None:
    assert author_id({"_links": {"user": {"href": "/api/v3/users/12"}}}) == 12
    assert author_id({"author": {"id": 7}}) == 7
    assert author_id({"user": {"_links": {"self": {"href": "/api/v3/users/9"}}}}) == 9
    assert author_id({}) is None
