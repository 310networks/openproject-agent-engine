"""Engine-owned current-status observation; the dashboard only reads its snapshot.

Separate from run history: dependency-blocked tickets need not have a run at all.
The small sidecar is replaced atomically, like the heartbeat, not a DB migration.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from op_agent_engine.config import EngineSettings, WorkflowConfig
from op_agent_engine.logging import get_logger
from op_agent_engine.openproject.client import OpenProjectClient, OpenProjectError
from op_agent_engine.openproject.resolve import OpenProjectResolution

FILE_NAME = "work-package-counts.json"
logger = get_logger(__name__)


async def refresh_counts(
    client: OpenProjectClient,
    config: WorkflowConfig,
    resolution: OpenProjectResolution,
    settings: EngineSettings,
) -> None:
    """Publish an all-or-nothing observation, never partial totals or fake zeros."""
    started_at = datetime.now(UTC).isoformat()
    projects = sorted(project.identifier for project in config.enabled_projects())
    payload: dict[str, Any] = {
        "source": client.base_url,
        "projects": projects,
        "observed_at": None,
        "blocked": None,
        "on_hold": None,
    }
    try:
        project_ids = sorted({str(resolution.projects[name].id) for name in projects})
        if not project_ids:
            raise ValueError("no monitored projects")
        counts = {}
        for key in ("blocked", "on_hold"):
            filters = [
                {"project": {"operator": "=", "values": project_ids}},
                {"status": {"operator": "=", "values": [str(resolution.statuses[key].id)]}},
            ]
            # Collection.total counts all matches, independent of page size. Two
            # tiny requests include all types, agents, and human-assigned tickets.
            response = await client.get_json(
                "/api/v3/work_packages",
                params={"filters": json.dumps(filters), "pageSize": 1},
            )
            total = response.get("total") if isinstance(response, dict) else None
            if type(total) is not int or total < 0:
                raise ValueError("invalid work-package collection total")
            counts[key] = total
        payload.update(counts)
        payload["observed_at"] = started_at
    except (OpenProjectError, KeyError, ValueError):
        # Do not publish provider errors/URLs (which may contain sensitive data).
        logger.warning("current work-package counts unavailable; retrying next cycle")
    payload["checked_at"] = datetime.now(UTC).isoformat()
    path = settings.data_dir / FILE_NAME
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(payload), encoding="utf-8")
        temporary.replace(path)
    except OSError:
        logger.warning("could not persist current work-package counts")


def read_counts(
    config: WorkflowConfig, settings: EngineSettings, *, now: datetime
) -> dict[str, Any]:
    """Expose only fresh values; missing, invalid, failed or old data is not zero."""
    stale_after = max(float(settings.poll_interval_seconds * 2 + 15), 45.0)
    result: dict[str, Any] = {
        "status": "unavailable",
        "source": (settings.op_base_url or "").rstrip("/"),
        "projects": [],
        "checked_at": None,
        "observed_at": None,
        "age_seconds": None,
        "stale_after_seconds": stale_after,
        "blocked": None,
        "on_hold": None,
    }
    try:
        payload = json.loads((settings.data_dir / FILE_NAME).read_text(encoding="utf-8"))
        if not isinstance(payload, dict) or payload.get("source") != result["source"]:
            return result
        projects = payload["projects"]
        if not isinstance(projects, list) or not projects or not all(
            isinstance(name, str) for name in projects
        ):
            return result
        expected = sorted(project.identifier for project in config.enabled_projects())
        if "*" not in expected and projects != expected:
            return result
        checked = datetime.fromisoformat(payload["checked_at"])
        if checked.tzinfo is None or checked > now:
            return result
        result.update(projects=projects, checked_at=checked.isoformat())
        if payload["observed_at"] is None:
            return result
        observed = datetime.fromisoformat(payload["observed_at"])
        if observed.tzinfo is None or observed > checked:
            return result
        if any(type(payload[key]) is not int or payload[key] < 0 for key in ("blocked", "on_hold")):
            return result
        age = (now - observed).total_seconds()
        result.update(observed_at=observed.isoformat(), age_seconds=round(age, 1))
        if age > stale_after:
            result["status"] = "stale"
        else:
            result.update(status="fresh", blocked=payload["blocked"], on_hold=payload["on_hold"])
    except (OSError, ValueError, TypeError, KeyError):
        pass
    return result
