"""Work package model and HAL entry parsing for the poller.

The poller queries candidate work packages and reduces each HAL entry to the
small set of fields the eligibility rules and dispatch ordering need.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

_HREF_ID_RE = re.compile(r"/(\d+)$")


@dataclass(frozen=True)
class WorkPackage:
    """A candidate work package with the fields the engine needs."""

    id: int
    display_id: str
    subject: str
    project_id: int
    type_id: int
    type_name: str
    status_id: int
    status_name: str
    priority_id: int
    priority_name: str
    assignee_id: int | None
    agent_value: str | None
    # Optimistic-locking version; required in the PATCH body when claiming
    # (verified live: omitting it yields HTTP 409 UpdateConflict).
    lock_version: int | None = None
    # Ticket description (rendered into the prompt; may be empty).
    description: str = ""


def _id_from_href(href: Any) -> int | None:
    if not isinstance(href, str):
        return None
    match = _HREF_ID_RE.search(href)
    return int(match.group(1)) if match else None


def _link(entry: dict[str, Any], name: str) -> dict[str, Any] | None:
    links = entry.get("_links")
    if not isinstance(links, dict):
        return None
    value = links.get(name)
    return value if isinstance(value, dict) else None


def _option_id(ref: Any) -> int | None:
    """Extract a custom option id from the shapes OpenProject uses."""
    if isinstance(ref, bool):
        return None
    if isinstance(ref, int):
        return ref
    if isinstance(ref, dict):
        links = ref.get("_links")
        if isinstance(links, dict):
            self_link = links.get("self")
            if isinstance(self_link, dict):
                href_id = _id_from_href(self_link.get("href"))
                if href_id is not None:
                    return href_id
        ref_id = ref.get("id")
        if isinstance(ref_id, int) and not isinstance(ref_id, bool):
            return ref_id
        # OpenProject returns list custom-field values in ``_links`` as a
        # plain HAL reference such as
        # ``{"href": "/api/v3/custom_options/2", "title": "BACKEND"}``.
        href_id = _id_from_href(ref.get("href"))
        if href_id is not None:
            return href_id
    return None


def _extract_agent_value(
    entry: dict[str, Any],
    agent_field_id: str,
    option_id_to_value: dict[int, str],
) -> str | None:
    raw = entry.get(agent_field_id)
    if raw is None:
        # List custom fields are exposed as links, not top-level attributes:
        # ``_links: {"customField1": {"href": ..., "title": "BACKEND"}}``.
        links = entry.get("_links")
        if isinstance(links, dict):
            raw = links.get(agent_field_id)
    if raw is None:
        return None
    refs: list[Any] = raw if isinstance(raw, list) else [raw]
    for ref in refs:
        option_id = _option_id(ref)
        if option_id is not None and option_id in option_id_to_value:
            return option_id_to_value[option_id]
    return None


def parse_work_package(
    entry: dict[str, Any],
    agent_field_id: str,
    option_id_to_value: dict[int, str],
) -> WorkPackage:
    """Reduce a work package HAL entry to a :class:`WorkPackage`."""
    status = _link(entry, "status") or {}
    type_link = _link(entry, "type") or {}
    priority = _link(entry, "priority") or {}
    project = _link(entry, "project") or {}
    assignee = _link(entry, "assignee") or {}
    raw_id = entry.get("id")
    work_package_id = int(raw_id) if isinstance(raw_id, int) else 0
    raw_lock = entry.get("lockVersion")
    return WorkPackage(
        id=work_package_id,
        display_id=str(entry.get("displayId") or work_package_id),
        subject=str(entry.get("subject", "")),
        project_id=_id_from_href(project.get("href")) or 0,
        type_id=_id_from_href(type_link.get("href")) or 0,
        type_name=str(type_link.get("title", "")),
        status_id=_id_from_href(status.get("href")) or 0,
        status_name=str(status.get("title", "")),
        priority_id=_id_from_href(priority.get("href")) or 0,
        priority_name=str(priority.get("title", "")),
        assignee_id=_id_from_href(assignee.get("href")),
        agent_value=_extract_agent_value(entry, agent_field_id, option_id_to_value),
        lock_version=(
            raw_lock
            if isinstance(raw_lock, int) and not isinstance(raw_lock, bool)
            else None
        ),
        description=str(entry.get("description") or ""),
    )
