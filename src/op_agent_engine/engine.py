"""Engine loop: poll, schedule, and (from M4) execute runs.

The loop (PLAN section 3):

1. Take the exclusive engine lock (``DATA_DIR/engine.lock``) so that only one
   engine instance runs at a time.
2. Open the run store and recover from a previous crash: every ``running``
   row is marked ``interrupted``.
3. Resolve the workflow against OpenProject (statuses, users, projects,
   types, agent field).
4. Repeat until stopped: poll for eligible work packages, schedule (claim)
   as many as the slot rules allow, then sleep for the poll interval.

Execution of claimed runs (workspace setup, prompt, runner) is M4 and later;
in this milestone claimed runs simply stay active until the next engine
start marks them interrupted.
"""

from __future__ import annotations

import asyncio
import contextlib
import signal
from pathlib import Path

from op_agent_engine.config import EngineSettings, WorkflowConfig
from op_agent_engine.logging import get_logger
from op_agent_engine.openproject.client import (
    OpenProjectClient,
    OpenProjectError,
)
from op_agent_engine.openproject.resolve import resolve_workflow
from op_agent_engine.poller import Poller
from op_agent_engine.scheduler import Scheduler
from op_agent_engine.store import EngineLock, Store

logger = get_logger(__name__)

LOCK_FILE_NAME = "engine.lock"
STORE_FILE_NAME = "engine.db"


class Engine:
    """The long-running polling engine."""

    def __init__(
        self,
        config: WorkflowConfig,
        settings: EngineSettings,
        *,
        client: OpenProjectClient | None = None,
    ) -> None:
        self._config = config
        self._settings = settings
        self._client = client  # when given, the engine does not close it

    @property
    def lock_path(self) -> Path:
        return self._settings.data_dir / LOCK_FILE_NAME

    @property
    def store_path(self) -> Path:
        return self._settings.data_dir / STORE_FILE_NAME

    async def run(
        self, *, max_polls: int | None = None, dry_run: bool = False
    ) -> int:
        """Run the engine loop. Returns a process exit code.

        With ``dry_run`` the loop polls and evaluates the slot rules but
        claims nothing (no store writes, no OpenProject updates).
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
                # Claimed-but-unexecuted runs have no child process to stop;
                # mark them interrupted so the state is consistent right away.
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
                if not dry_run and report.claimed_count:
                    logger.info(
                        "note: execution is not implemented yet (M4); claimed "
                        "runs stay active until the next engine start",
                        extra={"claimed": [c.run_id for c in report.claimed]},
                    )

            if max_polls is not None and poll_number >= max_polls:
                break
            await self._sleep_or_stop(stop)

        logger.info("engine stopped", extra={"polls": poll_number})
        return 0

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

    Performs a single poll (and, unless ``dry_run``, a single schedule pass)
    and exits. Neither form takes the engine lock. In dry-run mode no store
    file is created (an existing one is read so slot rules stay faithful);
    ``once`` uses the store so its claims are durable.
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
            # executed become interrupted. Runs claimed by this call stay
            # active; the next engine start recovers them the same way.
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
        finally:
            store.close()
        return 0
