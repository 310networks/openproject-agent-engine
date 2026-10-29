"""Activity parsing for the rejection loop and comment selection (M6).

OpenProject work package histories are exposed through
``GET /api/v3/work_packages/<id>/activities``. Each entry is a journal with:

* ``comment.raw`` for a journal that carries a comment (``Activity::Comment``),
* ``details[].raw`` for field changes (for example ``Status set to Rejected``
  or ``Status changed from In Testing to Rejected``),
* ``_links.user`` for the author,
* ``createdAt`` for the journal timestamp.

The shapes are verified against the live instance (docs/VERIFICATION.md,
section 11). OpenProject aggregates consecutive journals made by the same
user within a short window (``Setting.journal_aggregation_time_minutes``,
default 5), so rejection counting counts *details* rather than journals.

This module is deliberately tolerant about missing/extra fields: a malformed
entry is ignored instead of crashing a dispatch.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

_HREF_ID_RE = re.compile(r"/(\d+)/?$")
_STATUS_SET_RE = re.compile(r"^Status set to (?P<to>.+)$")
_STATUS_CHANGED_RE = re.compile(r"^Status changed from (?P<from>.+?) to (?P<to>.+)$")


@dataclass(frozen=True)
class ActivityComment:
    """One comment entry from a work package activity collection."""

    activity_id: int | None
    author_id: int | None
    author_title: str | None
    created_at: datetime | None
    created_at_raw: str | None
    text: str


def id_from_href(href: Any) -> int | None:
    """The numeric id at the end of a HAL ``href`` (``/users/12`` -> 12)."""
    if not isinstance(href, str):
        return None
    match = _HREF_ID_RE.search(href)
    return int(match.group(1)) if match else None


def parse_timestamp(value: Any) -> datetime | None:
    """Parse a HAL timestamp (``2026-02-10T12:00:00Z``) into an aware datetime."""
    if not isinstance(value, str) or not value:
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed


def activity_id(activity: dict[str, Any]) -> int | None:
    value = activity.get("id")
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def comment_text(activity: dict[str, Any]) -> str | None:
    """The comment text of an activity entry (None for non-comment entries)."""
    comment = activity.get("comment")
    raw: Any = None
    if isinstance(comment, dict):
        raw = comment.get("raw")
    elif isinstance(comment, str):
        raw = comment
    if not isinstance(raw, str) or not raw.strip():
        return None
    return raw


def author_id(activity: dict[str, Any]) -> int | None:
    """The author id of an activity.

    The live API exposes ``_links.user``; plain ``author``/``user`` references
    are tolerated for fixtures shaped that way.
    """
    for candidate in (activity.get("author"), activity.get("user")):
        if isinstance(candidate, dict):
            value = candidate.get("id")
            if isinstance(value, int) and not isinstance(value, bool):
                return value
            links = candidate.get("_links")
            if isinstance(links, dict):
                self_link = links.get("self")
                if isinstance(self_link, dict):
                    found = id_from_href(self_link.get("href"))
                    if found is not None:
                        return found
    links = activity.get("_links")
    if isinstance(links, dict):
        for key in ("user", "author"):
            link = links.get(key)
            if isinstance(link, dict):
                found = id_from_href(link.get("href"))
                if found is not None:
                    return found
    return None


def author_title(activity: dict[str, Any]) -> str | None:
    """The human-readable author name when the API includes it."""
    links = activity.get("_links")
    if isinstance(links, dict):
        for key in ("user", "author"):
            link = links.get(key)
            if isinstance(link, dict):
                title = link.get("title")
                if isinstance(title, str) and title.strip():
                    return title.strip()
    return None


def created_at(activity: dict[str, Any]) -> datetime | None:
    for key in ("createdAt", "updatedAt"):
        parsed = parse_timestamp(activity.get(key))
        if parsed is not None:
            return parsed
    return None


def created_at_raw(activity: dict[str, Any]) -> str | None:
    value = activity.get("createdAt")
    return value if isinstance(value, str) and value else None


def comment_from_activity(activity: dict[str, Any]) -> ActivityComment | None:
    """Reduce an activity entry to a comment, or None if it has no comment."""
    text = comment_text(activity)
    if text is None:
        return None
    return ActivityComment(
        activity_id=activity_id(activity),
        author_id=author_id(activity),
        author_title=author_title(activity),
        created_at=created_at(activity),
        created_at_raw=created_at_raw(activity),
        text=text,
    )


# -- status change details ----------------------------------------------------


def _detail_raw(detail: Any) -> str | None:
    if isinstance(detail, str):
        return detail
    if isinstance(detail, dict):
        raw = detail.get("raw")
        return raw if isinstance(raw, str) else None
    return None


def status_targets(activity: dict[str, Any]) -> list[str]:
    """The target status names of every status change in one activity."""
    details = activity.get("details")
    if not isinstance(details, list):
        return []
    targets: list[str] = []
    for detail in details:
        raw = _detail_raw(detail)
        if raw is None:
            continue
        match = _STATUS_SET_RE.match(raw.strip())
        if match is not None:
            targets.append(match.group("to").strip())
            continue
        match = _STATUS_CHANGED_RE.match(raw.strip())
        if match is not None:
            targets.append(match.group("to").strip())
    return targets


def count_status_changes(
    activities: Iterable[dict[str, Any]], status_name: str
) -> int:
    """How many times the history moved a work package to *status_name*.

    Used for rejection counting (PLAN section 4). OpenProject aggregates
    consecutive same-user journals, so a single journal can hold the final
    target of a short burst; counting details matches the visible history.
    """
    return sum(
        1
        for activity in activities
        for target in status_targets(activity)
        if target == status_name
    )
