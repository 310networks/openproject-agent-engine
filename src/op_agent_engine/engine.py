"""Engine loop: poll, schedule, and execute runs.

The loop:

1. Take the exclusive engine lock (``DATA_DIR/engine.lock``) so that only one
   engine instance runs at a time.
2. Open the run store and recover from a previous crash: every ``running``
   row is marked ``interrupted``.
3. Resolve the workflow against OpenProject (statuses, users, projects,
   types, agent field).
4. Repeat until stopped: poll for eligible work packages, schedule (claim)
   as many as the slot rules allow, and execute each claimed implement (M4),
   fix (M6), resume (M7) or review (M5) run.

On startup, leftover process groups from a crashed engine are terminated (PID
reuse safe) and every ``running`` row is marked ``interrupted``; In Progress
and In Testing work packages are then recovered by the poller (M7).

At a clean stop the engine waits for in-flight runs up to
``STOP_GRACE_SECONDS``; runs still going are terminated and their rows are
marked ``interrupted`` so the next start resumes them.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path

from op_agent_engine.config import ConfigError, EngineSettings, WorkflowConfig
from op_agent_engine.dispatcher import EXECUTED_KINDS, Dispatcher
from op_agent_engine.logging import get_logger
from op_agent_engine.openproject.client import (
    OpenProjectClient,
    OpenProjectError,
)
from op_agent_engine.openproject.resolve import OpenProjectResolution, resolve_workflow
from op_agent_engine.poller import Poller
from op_agent_engine.provision import (
    TokenVerificationError,
    enforce_agent_tokens,
)
from op_agent_engine.recovery import recover_interrupted_runs
from op_agent_engine.scheduler import ClaimedRun, Scheduler
from op_agent_engine.store import EngineLock, Store

logger = get_logger(__name__)

LOCK_FILE_NAME = "engine.lock"
STORE_FILE_NAME = "engine.db"
HEARTBEAT_FILE_NAME = "heartbeat"
PAUSE_FILE_NAME = "PAUSE"

DispatcherFactory = Callable[
    [OpenProjectResolution, Store, OpenProjectClient], Dispatcher
]


class Engine:
    """The long-running polling engine."""

    def __init__(
        self,
        config: WorkflowConfig,
        settings: EngineSettings,
        *,
        client: OpenProjectClient | None = None,
        dispatcher_factory: DispatcherFactory | None = None,
    ) -> None:
        self._config = config
        self._settings = settings
        self._client = client  # when given, the engine does not close it
        self._dispatcher_factory = dispatcher_factory

    @property
    def lock_path(self) -> Path:
        return self._settings.data_dir / LOCK_FILE_NAME

    @property
    def store_path(self) -> Path:
        return self._settings.data_dir / STORE_FILE_NAME

    @property
    def heartbeat_path(self) -> Path:
        return self._settings.data_dir / HEARTBEAT_FILE_NAME

    @property
    def pause_path(self) -> Path:
        return self._settings.data_dir / PAUSE_FILE_NAME

    def _make_dispatcher(
        self,
        resolution: OpenProjectResolution,
        store: Store,
        client: OpenProjectClient,
    ) -> Dispatcher:
        if self._dispatcher_factory is not None:
            return self._dispatcher_factory(resolution, store, client)
        return Dispatcher(
            config=self._config,
            resolution=resolution,
            store=store,
            client=client,
            settings=self._settings,
        )

    async def run(
        self, *, max_polls: int | None = None, dry_run: bool = False
    ) -> int:
        """Run the engine loop. Returns a process exit code.

        With ``dry_run`` the loop polls and evaluates the slot rules but
        claims nothing (no store writes, no OpenProject updates, no spawns).
        """
        lock = EngineLock(self.lock_path)
        if not dry_run and not lock.acquire():
            logger.error(
                "another engine instance holds the lock; exiting",
                extra={"lock": str(self.lock_path)},
            )
            return 1

        store = Store(
            self.store_path if not dry_run else (":memory:")
        )
        try:
            if not dry_run:
                recovery = recover_interrupted_runs(store)
                if recovery.interrupted:
                    logger.warning(
                        "crash recovery: marked %d running run(s) interrupted",
                        recovery.interrupted,
                        extra={"recovery": recovery.to_dict()},
                    )
            if self._client is not None:
                return await self._loop(
                    self._client, store, max_polls=max_polls, dry_run=dry_run
                )
            assert self._settings.op_base_url is not None
            assert self._settings.op_engine_token is not None
            async with OpenProjectClient(
                self._settings.op_base_url,
                self._settings.op_engine_token.get_secret_value(),
            ) as client:
                return await self._loop(
                    client, store, max_polls=max_polls, dry_run=dry_run
                )
        finally:
            if not dry_run:
                # A run left active by an unexpected failure is marked
                # interrupted so the next start recovers it (the in-flight
                # tasks were already waited for or terminated above).
                with contextlib.suppress(Exception):
                    store.mark_all_running_interrupted()
            with contextlib.suppress(Exception):
                store.close()
            if not dry_run:
                lock.release()

    async def _loop(
        self,
        client: OpenProjectClient,
        store: Store,
        *,
        max_polls: int | None = None,
        dry_run: bool = False,
    ) -> int:
        try:
            resolution = await resolve_workflow(client, self._config)
        except OpenProjectError as error:
            logger.error("workflow resolution failed: %s", error)
            return 1

        # M8: a token configured for an agent must belong to that agent's
        # login. Missing tokens are reported by ``provision``; a mismatch is a
        # startup failure.
        if self._settings.op_base_url:
            try:
                await enforce_agent_tokens(
                    self._settings.op_base_url, self._config, os.environ
                )
            except TokenVerificationError as error:
                logger.error("agent token verification failed: %s", error)
                return 1

        poller = Poller(
            client,
            self._config,
            resolution,
            max_concurrent_runs=self._settings.max_concurrent_runs,
            max_runs_per_agent=self._settings.max_runs_per_agent,
            blocker_refresh_seconds=float(
                self._settings.blocker_refresh_seconds
            ),
        )
        scheduler = Scheduler(
            client,
            self._config,
            resolution,
            store,
            max_concurrent_runs=self._settings.max_concurrent_runs,
            max_runs_per_agent=self._settings.max_runs_per_agent,
        )
        dispatcher = (
            None
            if dry_run
            else self._make_dispatcher(resolution, store, client)
        )
        logger.info(
            "engine started",
            extra={
                "poll_interval_seconds": self._settings.poll_interval_seconds,
                "blocker_refresh_seconds": self._settings.blocker_refresh_seconds,
                "max_concurrent_runs": self._settings.max_concurrent_runs,
                "max_runs_per_agent": self._settings.max_runs_per_agent,
                "paused": self._settings.engine_paused,
                "store": str(store.path),
            },
        )

        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            with contextlib.suppress(NotImplementedError):
                loop.add_signal_handler(sig, stop.set)

        in_flight: set[asyncio.Task[None]] = set()
        # A run that finishes frees a slot; polling the very next interval would
        # waste a cycle, so completion wakes the loop immediately.
        wake = asyncio.Event()

        def _run_finished(task: asyncio.Task[None]) -> None:
            in_flight.discard(task)
            wake.set()

        poll_number = 0
        last_idle_reason: str | None = None
        while not stop.is_set():
            # Consume a completion signal before deciding: the store snapshot
            # read below already reflects the finished run.
            wake.clear()
            active_work_package_ids: frozenset[int] = frozenset()
            active_run_counts: dict[str, int] = {}
            if not dry_run:
                active_work_package_ids = store.active_work_package_ids()
                active_run_counts = store.active_run_counts_by_agent()

            if max_polls is None and not dry_run:
                idle_reason = self._idle_reason(
                    len(active_work_package_ids), active_run_counts
                )
                if idle_reason is not None:
                    # Nothing can be claimed, so a poll would only burn
                    # OpenProject requests. The heartbeat still advances so the
                    # dashboard does not look stale.
                    self._write_heartbeat()
                    if idle_reason != last_idle_reason:
                        logger.info("poll skipped: %s", idle_reason)
                        last_idle_reason = idle_reason
                    await self._sleep_or_stop(stop, wake)
                    continue
            last_idle_reason = None

            poll_number += 1
            try:
                plan = await poller.poll(
                    active_run_work_package_ids=active_work_package_ids,
                    active_run_counts_by_agent=active_run_counts,
                    global_slots_used=len(active_work_package_ids),
                )
            except OpenProjectError as error:
                logger.error(
                    "poll failed (will retry next cycle): %s",
                    error,
                    extra={"poll_number": poll_number},
                )
                if not dry_run:
                    self._write_heartbeat()
                await self._sleep_or_stop(stop, wake)
                continue

            if not dry_run:
                self._write_heartbeat()

            # A stop signal that arrived during the poll must not dispatch.
            if stop.is_set():
                break

            if self._is_paused():
                logger.info(
                    "engine paused: %d item(s) not claimed",
                    len(plan.items),
                    extra={"poll_number": poll_number, "items": list(plan.work_package_ids)},
                )
            else:
                report = await scheduler.schedule(plan, dry_run=dry_run)
                if dispatcher is not None:
                    for claimed in report.claimed:
                        if claimed.kind not in EXECUTED_KINDS:
                            logger.warning(
                                "claim for %s has unsupported kind %s; leaving "
                                "the run active",
                                claimed.item.work_package.display_id,
                                claimed.kind,
                                extra={
                                    "run_id": claimed.run_id,
                                    "work_package_id": claimed.item.work_package.id,
                                    "kind": claimed.kind,
                                },
                            )
                            continue
                        task = asyncio.create_task(
                            self._execute_claimed(dispatcher, claimed)
                        )
                        in_flight.add(task)
                        task.add_done_callback(_run_finished)

            if max_polls is not None and poll_number >= max_polls:
                break
            await self._sleep_or_stop(stop, wake)

        if in_flight:
            logger.info(
                "waiting for %d in-flight run(s) (grace %ds)",
                len(in_flight),
                self._settings.stop_grace_seconds,
            )
            _, pending = await asyncio.wait(
                in_flight, timeout=self._settings.stop_grace_seconds
            )
            if pending:
                logger.warning(
                    "grace period expired; terminating %d run(s)",
                    len(pending),
                )
                for task in pending:
                    task.cancel()
                await asyncio.wait(pending)
            # Runs cancelled here were terminated by the runner and are still
            # ``running`` in the store; mark them so the next start recovers
            # them instead of treating them as live.
            store.mark_all_running_interrupted()

        logger.info("engine stopped", extra={"polls": poll_number})
        return 0

    # -- pause and health ---------------------------------------------------

    def _is_paused(self) -> bool:
        """Pause from ``ENGINE_PAUSED`` or the ``DATA_DIR/PAUSE`` file."""
        return is_paused(self._settings)

    def _write_heartbeat(self) -> None:
        """Record the time of the last completed poll."""
        write_heartbeat(self._settings)

    async def _execute_claimed(
        self, dispatcher: Dispatcher, claimed: ClaimedRun
    ) -> None:
        """Run one claimed run; keep the row active on unexpected failure."""
        try:
            await dispatcher.execute(claimed)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            logger.exception(
                "run execution failed (run stays active for recovery): %s",
                error,
                extra={
                    "run_id": claimed.run_id,
                    "work_package_id": claimed.item.work_package.id,
                    "agent": claimed.item.agent_key,
                },
            )

    def _idle_reason(
        self, active_total: int, active_run_counts: Mapping[str, int]
    ) -> str | None:
        """Why a poll would be wasted, or ``None`` when a slot is free.

        Every reason here means no candidate could be claimed this cycle, so
        the poll is skipped entirely. The scheduler re-checks the same limits
        when it claims, so this only saves requests.
        """
        if self._is_paused():
            return "engine is paused"
        if active_total >= self._settings.max_concurrent_runs:
            return (
                f"all {self._settings.max_concurrent_runs} run slot(s) in use"
            )
        busy_agents = [
            key
            for key in self._config.agents
            if active_run_counts.get(key, 0) >= self._settings.max_runs_per_agent
        ]
        if len(busy_agents) == len(self._config.agents):
            return "every agent already has an active run"
        return None

    async def _sleep_or_stop(
        self, stop: asyncio.Event, wake: asyncio.Event | None = None
    ) -> None:
        timeout = self._settings.poll_interval_seconds
        if wake is None:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=timeout)
            return
        waiters = [
            asyncio.ensure_future(stop.wait()),
            asyncio.ensure_future(wake.wait()),
        ]
        try:
            await asyncio.wait(
                waiters, timeout=timeout, return_when=asyncio.FIRST_COMPLETED
            )
        finally:
            for waiter in waiters:
                waiter.cancel()
            await asyncio.gather(*waiters, return_exceptions=True)


def is_paused(settings: EngineSettings) -> bool:
    """Whether new dispatches are paused (``ENGINE_PAUSED`` or PAUSE file)."""
    return settings.engine_paused or (
        settings.data_dir / PAUSE_FILE_NAME
    ).exists()


def read_heartbeat(settings: EngineSettings) -> tuple[str | None, float | None]:
    """Return the heartbeat timestamp and its age in seconds.

    ``(None, None)`` means the file is missing; ``(raw, None)`` means it could
    not be parsed as a timestamp.
    """
    heartbeat = settings.data_dir / HEARTBEAT_FILE_NAME
    try:
        raw = heartbeat.read_text(encoding="utf-8").strip()
    except OSError:
        return None, None
    if not raw:
        return None, None
    try:
        timestamp = datetime.fromisoformat(raw)
    except ValueError:
        return raw, None
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=UTC)
    age = max(0.0, (datetime.now(UTC) - timestamp).total_seconds())
    return raw, age


def write_heartbeat(settings: EngineSettings) -> None:
    """Write the ``DATA_DIR/heartbeat`` timestamp atomically."""
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    heartbeat = settings.data_dir / HEARTBEAT_FILE_NAME
    timestamp = datetime.now(UTC).isoformat()
    temporary = heartbeat.with_suffix(".tmp")
    temporary.write_text(timestamp + "\n", encoding="utf-8")
    temporary.replace(heartbeat)


def _require_agent_tokens(settings: EngineSettings) -> None:
    """A run needs one token per agent; refuse to start without them."""
    missing = settings.missing_agent_tokens()
    if missing:
        raise ConfigError(
            "missing required environment variables: " + ", ".join(missing)
        )


def run_engine(
    config: WorkflowConfig, settings: EngineSettings, *, dry_run: bool = False
) -> int:
    """Synchronous entry point for the ``run`` subcommand."""
    settings.require_api_values()
    _require_agent_tokens(settings)
    return asyncio.run(Engine(config, settings).run(dry_run=dry_run))


def run_engine_once(
    config: WorkflowConfig,
    settings: EngineSettings,
    *,
    dry_run: bool = False,
) -> int:
    """Synchronous entry point for ``once`` / ``dry-run``.

    Performs a single poll (and, unless ``dry_run``, a single schedule pass
    and executes the claimed implement and review runs) and exits. Neither
    form takes the engine lock. In dry-run mode no store file is created (an
    existing one is read so slot rules stay faithful); ``once`` uses the store
    so its claims are durable.
    """
    settings.require_api_values()
    _require_agent_tokens(settings)
    return asyncio.run(
        _run_once_async(config, settings, dry_run=dry_run)
    )


async def _run_once_async(
    config: WorkflowConfig, settings: EngineSettings, *, dry_run: bool
) -> int:
    assert settings.op_base_url is not None
    assert settings.op_engine_token is not None
    async with OpenProjectClient(
        settings.op_base_url, settings.op_engine_token.get_secret_value()
    ) as client:
        resolution = await resolve_workflow(client, config)
        if settings.op_base_url:
            await enforce_agent_tokens(settings.op_base_url, config, os.environ)
        poller = Poller(
            client,
            config,
            resolution,
            max_concurrent_runs=settings.max_concurrent_runs,
            max_runs_per_agent=settings.max_runs_per_agent,
        )
        plan = await poller.poll()
        if not dry_run:
            write_heartbeat(settings)

        if dry_run:
            store_path = settings.data_dir / STORE_FILE_NAME
            store = Store(store_path) if store_path.exists() else Store(":memory:")
            scheduler = Scheduler(
                client,
                config,
                resolution,
                store,
                max_concurrent_runs=settings.max_concurrent_runs,
                max_runs_per_agent=settings.max_runs_per_agent,
            )
            report = await scheduler.schedule(plan, dry_run=True)
            logger.info(
                "dry-run complete: %d would be claimed, %d skipped",
                report.claimed_count,
                report.skipped_count,
                extra={"dry_run_report": report.to_dict()},
            )
            return 0

        store = Store(settings.data_dir / STORE_FILE_NAME)
        try:
            # Crash recovery from a previous start: leftover processes are
            # terminated and runs claimed but never executed become
            # interrupted. Runs claimed by this call that fail to execute
            # stay active; the next engine start recovers them the same way.
            recovery = recover_interrupted_runs(store)
            if recovery.interrupted:
                logger.warning(
                    "recovery: marked %d running run(s) interrupted",
                    recovery.interrupted,
                    extra={"recovery": recovery.to_dict()},
                )
            if is_paused(settings):
                logger.info(
                    "engine paused: %d item(s) not claimed", len(plan.items)
                )
                return 0
            scheduler = Scheduler(
                client,
                config,
                resolution,
                store,
                max_concurrent_runs=settings.max_concurrent_runs,
                max_runs_per_agent=settings.max_runs_per_agent,
            )
            report = await scheduler.schedule(plan)
            logger.info(
                "once complete: %d claimed, %d skipped",
                report.claimed_count,
                report.skipped_count,
                extra={"schedule_report": report.to_dict()},
            )

            if report.claimed_count:
                dispatcher = Dispatcher(
                    config=config,
                    resolution=resolution,
                    store=store,
                    client=client,
                    settings=settings,
                )
                for claimed in report.claimed:
                    if claimed.kind not in EXECUTED_KINDS:
                        logger.warning(
                            "once: claim for %s has unsupported kind %s; "
                            "leaving the run active",
                            claimed.item.work_package.display_id,
                            claimed.kind,
                        )
                        continue
                    try:
                        await dispatcher.execute(claimed)
                    except asyncio.CancelledError:
                        raise
                    except Exception as error:
                        logger.exception(
                            "once: run execution failed (run stays active "
                            "for recovery): %s",
                            error,
                            extra={
                                "run_id": claimed.run_id,
                                "work_package_id": claimed.item.work_package.id,
                            },
                        )
        finally:
            store.close()
        return 0
