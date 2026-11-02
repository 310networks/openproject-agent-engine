"""Read-only web dashboard for the engine.

The dashboard is a separate, optional process: it opens the SQLite store with
``mode=ro`` and never writes to it, to OpenProject, or to a workspace. That
keeps it safe to restart or leave running while the engine is mid-run.

Endpoints:

* ``GET /``                  the single-page UI (no external assets or CDNs)
* ``GET /api/status?limit=N`` the same data the UI renders, as JSON
* ``GET /healthz``           liveness for a reverse proxy or uptime check

The server binds to ``127.0.0.1`` by default and has no authentication. Put it
behind the LAN reverse proxy (or an SSH tunnel) if it needs to be reachable
from another machine.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from pathlib import Path
from typing import Any, cast
from urllib.parse import parse_qs, urlparse

from op_agent_engine import __version__
from op_agent_engine.config import EngineSettings, WorkflowConfig
from op_agent_engine.engine import PAUSE_FILE_NAME, STORE_FILE_NAME, read_heartbeat
from op_agent_engine.logging import get_logger
from op_agent_engine.store import (
    CANCELLED,
    FAILED,
    INTERRUPTED,
    RUNNING,
    TIMEOUT,
    Run,
    Store,
)

logger = get_logger(__name__)

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
DEFAULT_LIMIT = 25
MAX_LIMIT = 500
DEFAULT_REFRESH_SECONDS = 3

#: Outcomes that mean the work was delivered.
COMPLETED_OUTCOMES = frozenset(
    {"approved", "ready_for_review", "ready_for_review_existing_pr"}
)
#: Outcomes that mean the reviewer turned the work down.
DECLINED_OUTCOMES = frozenset({"rejected"})
#: Outcomes that need a human (the ticket is On Hold or should be).
BLOCKED_OUTCOMES = frozenset({"blocked", "rejection_cap_reached", "no_pr_for_branch"})
#: Terminal states that are not a delivered result.
FAILED_STATES = frozenset({FAILED, TIMEOUT, INTERRUPTED, CANCELLED})

ASSET_DIR = "dashboard_assets"
_ASSETS = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/style.css": ("style.css", "text/css; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
}


def _read_meta(run: Run) -> dict[str, Any]:
    """Read the non-secret parts of ``runs/<id>/meta.json``.

    The dashboard only needs identifiers and result metadata; argv, env and
    the agent command stay out of the HTTP response. A missing or malformed
    file is normal (the run may not have written it yet).
    """
    path = Path(run.run_dir) / "meta.json"
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(raw, dict):
        return {}
    return cast(dict[str, Any], raw)


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def _duration_seconds(run: Run, now: datetime) -> float | None:
    started = _parse_time(run.started_at)
    if started is None:
        return None
    finished = _parse_time(run.finished_at) or now
    return max(0.0, (finished - started).total_seconds())


def _bucket_counts(
    *, total: int, running: int, outcomes: dict[str, int]
) -> dict[str, int]:
    """Split a run count into the dashboard's headline buckets.

    Every bucket is derived from terminal outcomes; ``failed`` is the
    remainder, which keeps the numbers consistent even for outcomes added by
    a newer engine version.
    """
    completed = sum(outcomes.get(name, 0) for name in COMPLETED_OUTCOMES)
    declined = sum(outcomes.get(name, 0) for name in DECLINED_OUTCOMES)
    blocked = sum(outcomes.get(name, 0) for name in BLOCKED_OUTCOMES)
    return {
        "total": total,
        "running": running,
        "completed": completed,
        "declined": declined,
        "blocked": blocked,
        "failed": max(0, total - running - completed - declined - blocked),
    }


def _engine_status(
    settings: EngineSettings, *, heartbeat_age: float | None
) -> tuple[str, float]:
    """Return ``(status, stale_after_seconds)`` for the engine badge."""
    # A poll older than two intervals plus a small grace period means the
    # process is stopped or wedged. The floor avoids a red badge during a
    # short restart when POLL_INTERVAL_SECONDS is very small.
    stale_after = max(float(settings.poll_interval_seconds * 2 + 15), 45.0)
    if heartbeat_age is None:
        return "offline", stale_after
    if heartbeat_age > stale_after:
        return "stale", stale_after
    paused = settings.engine_paused or (
        settings.data_dir / PAUSE_FILE_NAME
    ).exists()
    return ("paused" if paused else "active"), stale_after


def _session_payload(
    run: Run,
    *,
    now: datetime,
    settings: EngineSettings,
    agent_names: dict[str, str],
) -> dict[str, Any]:
    meta = _read_meta(run)
    base_url = (settings.op_base_url or "").rstrip("/")
    duration = _duration_seconds(run, now)
    meta_duration = meta.get("duration_seconds")
    if isinstance(meta_duration, (int, float)):
        duration = float(meta_duration)
    display_id = run.display_id or f"#{run.work_package_id}"
    work_package_url = (
        f"{base_url}/work_packages/{run.work_package_id}" if base_url else None
    )
    return {
        "id": run.id,
        "work_package_id": run.work_package_id,
        "display_id": display_id,
        "subject": run.subject,
        "work_package_url": work_package_url,
        "agent": run.agent,
        "agent_name": agent_names.get(run.agent, run.agent),
        "kind": run.kind,
        "state": run.state,
        "outcome": run.outcome,
        "attempt": run.attempt,
        "branch": run.branch,
        "project": meta.get("project"),
        "repo": meta.get("repo"),
        "pr_url": meta.get("pr_url"),
        "transition": meta.get("transition"),
        "exit_code": meta.get("exit_code", run.exit_code),
        "result_line": run.result_line,
        "started_at": run.started_at,
        "finished_at": run.finished_at,
        "duration_seconds": round(duration, 1) if duration is not None else None,
        "run_dir": run.run_dir,
    }


@dataclass(frozen=True)
class DashboardData:
    """Builds one immutable snapshot of engine state for the UI."""

    config: WorkflowConfig
    settings: EngineSettings
    default_limit: int = DEFAULT_LIMIT

    def snapshot(self, *, limit: int | None = None) -> dict[str, Any]:
        limit = self.default_limit if limit is None else max(1, min(limit, MAX_LIMIT))
        now = datetime.now(UTC)
        store_path = self.settings.data_dir / STORE_FILE_NAME

        if store_path.exists():
            # Read-only: never contend with the engine's writes or migrate.
            store = Store(store_path, read_only=True)
            try:
                return self._snapshot_from_store(store, now=now, limit=limit)
            finally:
                store.close()
        return self._snapshot_from_store(
            Store(":memory:"), now=now, limit=limit
        )

    def _snapshot_from_store(
        self, store: Store, *, now: datetime, limit: int
    ) -> dict[str, Any]:
        active = store.active_runs()
        finished = store.finished_runs(limit)
        state_counts = store.state_counts()
        outcome_counts = store.outcome_counts()
        last_by_agent = store.last_finished_run_by_agent()
        per_agent = store.agent_counts()

        agent_names = {
            key: agent.name for key, agent in self.config.agents.items()
        }
        active_by_agent: dict[str, list[Run]] = {}
        for run in active:
            active_by_agent.setdefault(run.agent, []).append(run)
        total_runs = sum(state_counts.values())
        running = state_counts.get(RUNNING, 0)
        counts = _bucket_counts(
            total=total_runs, running=running, outcomes=outcome_counts
        )

        agents: list[dict[str, Any]] = []
        for key, agent in self.config.agents.items():
            raw_counts = per_agent.get(key, {})
            agent_outcomes = {
                name.removeprefix("outcome:"): value
                for name, value in raw_counts.items()
                if name.startswith("outcome:")
            }
            agent_total = sum(
                value
                for name, value in raw_counts.items()
                if not name.startswith("outcome:")
            )
            agent_running = raw_counts.get(RUNNING, 0)
            current_sessions = active_by_agent.get(key, [])
            current = current_sessions[-1] if current_sessions else None
            last = last_by_agent.get(key)
            scoped = self.config.projects_for_agent(agent)
            status = "running" if current_sessions else "idle"
            if not scoped:
                status = "unavailable"
            agents.append(
                {
                    "key": key,
                    "name": agent.name,
                    "kind": agent.kind,
                    "op_login": agent.op_login,
                    "runner": agent.runner,
                    "model": self.config.model_for(key),
                    "projects": [project.identifier for project in scoped],
                    "status": status,
                    "current_session": (
                        _session_payload(
                            current,
                            now=now,
                            settings=self.settings,
                            agent_names=agent_names,
                        )
                        if current is not None
                        else None
                    ),
                    "current_sessions": [
                        _session_payload(
                            run,
                            now=now,
                            settings=self.settings,
                            agent_names=agent_names,
                        )
                        for run in current_sessions
                    ],
                    "last_session": (
                        _session_payload(
                            last,
                            now=now,
                            settings=self.settings,
                            agent_names=agent_names,
                        )
                        if last is not None
                        else None
                    ),
                    "counts": _bucket_counts(
                        total=agent_total,
                        running=agent_running,
                        outcomes=agent_outcomes,
                    ),
                }
            )

        heartbeat_raw, heartbeat_age = read_heartbeat(self.settings)
        status, stale_after = _engine_status(
            self.settings, heartbeat_age=heartbeat_age
        )
        paused = self.settings.engine_paused or (
            self.settings.data_dir / PAUSE_FILE_NAME
        ).exists()

        return {
            "generated_at": now.isoformat(),
            "version": __version__,
            "data_dir": str(self.settings.data_dir),
            "refresh_seconds": DEFAULT_REFRESH_SECONDS,
            "engine": {
                "status": status,
                "polling": status in {"active", "paused"},
                "paused": paused,
                "heartbeat": heartbeat_raw,
                "heartbeat_age_seconds": (
                    round(heartbeat_age, 1)
                    if heartbeat_age is not None
                    else None
                ),
                "stale_after_seconds": round(stale_after, 1),
                "poll_interval_seconds": self.settings.poll_interval_seconds,
                "active_runs": running,
                "max_concurrent_runs": self.settings.max_concurrent_runs,
                "max_runs_per_agent": self.settings.max_runs_per_agent,
            },
            "counts": {
                **counts,
                "outcomes": dict(
                    sorted(outcome_counts.items(), key=lambda item: (-item[1], item[0]))
                ),
            },
            "agents": agents,
            "active_sessions": [
                _session_payload(
                    run,
                    now=now,
                    settings=self.settings,
                    agent_names=agent_names,
                )
                for run in active
            ],
            "recent_sessions": [
                _session_payload(
                    run,
                    now=now,
                    settings=self.settings,
                    agent_names=agent_names,
                )
                for run in finished
            ],
        }


def _asset(name: str) -> bytes:
    resource = resources.files("op_agent_engine").joinpath(ASSET_DIR, name)
    return resource.read_bytes()


class DashboardHTTPServer(ThreadingHTTPServer):
    """HTTP server carrying the immutable config for request handlers."""

    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self,
        address: tuple[str, int],
        data: DashboardData,
        *,
        refresh_seconds: int = DEFAULT_REFRESH_SECONDS,
    ) -> None:
        super().__init__(address, DashboardRequestHandler)
        self.data = data
        self.refresh_seconds = refresh_seconds


class DashboardRequestHandler(BaseHTTPRequestHandler):
    server_version = "op-agent-engine-dashboard"
    sys_version = ""

    @property
    def dashboard(self) -> DashboardHTTPServer:
        return cast(DashboardHTTPServer, self.server)

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        try:
            if path == "/healthz":
                self._send_json({"ok": True})
                return
            if path == "/api/status":
                limit = self._limit(parse_qs(parsed.query))
                if limit is None:
                    self._send_json(
                        {"error": "limit must be an integer between 1 and 500"},
                        status=HTTPStatus.BAD_REQUEST,
                    )
                    return
                payload = self.dashboard.data.snapshot(limit=limit)
                payload["refresh_seconds"] = self.dashboard.refresh_seconds
                self._send_json(payload)
                return
            if path == "/favicon.ico":
                self.send_response(HTTPStatus.NO_CONTENT)
                self.end_headers()
                return
            asset = _ASSETS.get(path)
            if asset is None:
                self._send_json(
                    {"error": "not found"}, status=HTTPStatus.NOT_FOUND
                )
                return
            name, content_type = asset
            body = _asset(name)
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            return

    def _limit(self, query: dict[str, list[str]]) -> int | None:
        raw_values = query.get("limit")
        if not raw_values:
            return self.dashboard.data.default_limit
        try:
            value = int(raw_values[0])
        except ValueError:
            return None
        if value < 1 or value > MAX_LIMIT:
            return None
        return value

    def _send_json(
        self, payload: dict[str, Any], *, status: HTTPStatus = HTTPStatus.OK
    ) -> None:
        body = json.dumps(payload, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        logger.debug("dashboard request: " + format, *args)


def serve_dashboard(
    config: WorkflowConfig,
    settings: EngineSettings,
    *,
    host: str | None = None,
    port: int | None = None,
    limit: int = DEFAULT_LIMIT,
    refresh_seconds: int = DEFAULT_REFRESH_SECONDS,
) -> int:
    """Serve the dashboard until interrupted. Returns a process exit code."""
    bind_host = host or settings.dashboard_host
    bind_port = port or settings.dashboard_port
    try:
        server = DashboardHTTPServer(
            (bind_host, bind_port),
            DashboardData(config, settings, default_limit=limit),
            refresh_seconds=refresh_seconds,
        )
    except OSError as error:
        logger.error(
            "cannot bind dashboard to %s:%s: %s", bind_host, bind_port, error
        )
        return 1
    address = server.server_address
    actual_host = address[0]
    if isinstance(actual_host, bytes):
        actual_host = actual_host.decode()
    actual_port = int(address[1])
    logger.info(
        "dashboard listening",
        extra={
            "host": actual_host,
            "port": actual_port,
            "data_dir": str(settings.data_dir),
        },
    )
    print(f"op-agent-engine dashboard: http://{actual_host}:{actual_port}")
    if actual_host not in {"127.0.0.1", "::1", "localhost"}:
        logger.warning(
            "dashboard is bound to a non-loopback address and has no "
            "authentication; restrict access at the reverse proxy",
            extra={"host": actual_host},
        )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


def dashboard_url(settings: EngineSettings) -> str:
    """The URL the dashboard would use with the configured host and port."""
    return f"http://{settings.dashboard_host}:{settings.dashboard_port}"


__all__ = [
    "DEFAULT_HOST",
    "DEFAULT_LIMIT",
    "DEFAULT_PORT",
    "DEFAULT_REFRESH_SECONDS",
    "MAX_LIMIT",
    "DashboardData",
    "DashboardHTTPServer",
    "dashboard_url",
    "serve_dashboard",
]
