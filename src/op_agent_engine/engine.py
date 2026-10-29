"""Engine loop: poll, schedule, and execute runs.

The loop (PLAN section 3):

1. Take the exclusive engine lock (``DATA_DIR/engine.lock``) so that only one
   engine instance runs at a time.
2. Open the run store and recover from a previous crash: every ``running``
   row is marked ``interrupted``.
3. Resolve the workflow against OpenProject (statuses, users, projects,
   types, agent field).
4. Repeat until stopped: poll for eligible work packages, schedule (claim)
   as many as the slot rules allow, and execute each claimed implement (M4)
   or review (M5) run. Fix runs are claimed but not executed yet (M6).

At a clean stop the engine waits for in-flight runs up to
``STOP_GRACE_SECONDS``; runs still going are cancelled and their rows stay
``running`` until the next start marks them ``interrupted`` (M7 then resumes
them).
"""

from __future__ import annotations

import asyncio
import contextlib
import signal
from collections.abc import Callable
from pathlib import Path

from op_agent_engine.config import EngineSettings, WorkflowConfig
from op_agent_engine.dispatcher import EXECUTED_KINDS, Dispatcher
from op_agent_engine.logging import get_logger
from op_agent_engine.openproject.client import (
    OpenProjectClient,
    OpenProjectError,
)
from op_agent_engine.openproject.resolve import OpenProjectResolution, resolve_workflow
from op_agent_engine.poller import Poller
from op_agent_engine.scheduler import ClaimedRun, Scheduler
from op_agent_engine.store import EngineLock, Store

logger = get_logger(__name__)

LOCK_FILE_NAME = "engine.lock"
STORE_FILE_NAME = "engine.db"

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
                interrupted = store.mark_all_running_interrupted()
                if interrupted:
                    logger.warning(
                        "crash recovery: marked %d running run(s) interrupted",
                        interrupted,
                        extra={"interrupted": interrupted},
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
                # Runs still in flight (grace exceeded) have no child process
                # to stop here; mark them interrupted so the state is
                # consistent right away (M7 resumes them on the next start).
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

        poller = Poller(
            client,
            self._config,
            resolution,
            max_concurrent_runs=self._settings.max_concurrent_runs,
        )
        scheduler = Scheduler(
            client,
            self._config,
            resolution,
            store,
            max_concurrent_runs=self._settings.max_concurrent_runs,
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
                "max_concurrent_runs": self._settings.max_concurrent_runs,
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
        poll_number = 0
        while not stop.is_set():
            poll_number += 1
            try:
                plan = await poller.poll()
            except OpenProjectError as error:
                logger.error(
                    "poll failed (will retry next cycle): %s",
                    error,
                    extra={"poll_number": poll_number},
                )
                await self._sleep_or_stop(stop)
                continue

            if self._settings.engine_paused:
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
                            logger.info(
                                "claim for %s has kind %s; execution is M6/M7",
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
                        task.add_done_callback(in_flight.discard)

            if max_polls is not None and poll_number >= max_polls:
                break
            await self._sleep_or_stop(stop)

        if in_flight:
            logger.info(
                "waiting for %d in-flight run(s) (grace %ds)",
                len(in_flight),
                self._settings.stop_grace_seconds,
            )
            _, pending = await asyncio.wait(
                in_flight, timeout=self._settings.stop_grace_seconds
            )
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.wait(pending)

        logger.info("engine stopped", extra={"polls": poll_number})
        return 0

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

    async def _sleep_or_stop(self, stop: asyncio.Event) -> None:
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(
                stop.wait(), timeout=self._settings.poll_interval_seconds
            )


def run_engine(
    config: WorkflowConfig, settings: EngineSettings, *, dry_run: bool = False
) -> int:
    """Synchronous entry point for the ``run`` subcommand."""
    settings.require_api_values()
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
        poller = Poller(
            client,
            config,
            resolution,
            max_concurrent_runs=settings.max_concurrent_runs,
        )
        plan = await poller.poll()

        if dry_run:
            store_path = settings.data_dir / STORE_FILE_NAME
            store = Store(store_path) if store_path.exists() else Store(":memory:")
            scheduler = Scheduler(
                client,
                config,
                resolution,
                store,
                max_concurrent_runs=settings.max_concurrent_runs,
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
            # Crash recovery from a previous start: runs claimed but never
            # executed become interrupted. Runs claimed by this call that
            # fail to execute stay active; the next engine start recovers
            # them the same way.
            store.mark_all_running_interrupted()
            scheduler = Scheduler(
                client,
                config,
                resolution,
                store,
                max_concurrent_runs=settings.max_concurrent_runs,
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
                        logger.info(
                            "once: claim for %s has kind %s; execution is "
                            "M6/M7",
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
