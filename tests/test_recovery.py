"""Tests for startup crash recovery."""

from __future__ import annotations

import subprocess
from pathlib import Path

from op_agent_engine.recovery import (
    process_start_identity,
    recover_interrupted_runs,
)
from op_agent_engine.store import INTERRUPTED, Store


def _store(tmp_path: Path) -> Store:
    return Store(tmp_path / "engine.db")


def _active_run_with_process(
    store: Store,
    *,
    proc_start: str,
    work_package_id: int = 100,
) -> tuple[int, subprocess.Popen[bytes]]:
    process = subprocess.Popen(["sleep", "30"], start_new_session=True)
    run_id = store.begin_run(
        work_package_id=work_package_id,
        project_id=4,
        agent=f"IMPL{work_package_id}",
        kind="implement",
        attempt=1,
        branch=f"op-4-{work_package_id}",
        run_dir_template=str(store.runs_dir / "{id}"),
    )
    store.set_process(
        run_id, pid=process.pid, pgid=process.pid, proc_start=proc_start
    )
    return run_id, process


def test_process_start_identity_is_stable_for_a_live_process() -> None:
    process = subprocess.Popen(["sleep", "30"], start_new_session=True)
    try:
        first = process_start_identity(process.pid)
        second = process_start_identity(process.pid)
        assert first is not None
        assert first == second
    finally:
        process.terminate()
        process.wait(timeout=10)


def test_recovery_kills_a_leftover_process_group(tmp_path: Path) -> None:
    store = _store(tmp_path)
    process: subprocess.Popen[bytes] | None = None
    try:
        identity = None
        process = subprocess.Popen(["sleep", "30"], start_new_session=True)
        identity = process_start_identity(process.pid)
        assert identity is not None
        run_id = store.begin_run(
            work_package_id=100,
            project_id=4,
            agent="IMPL",
            kind="implement",
            attempt=1,
            branch="op-4-100",
            run_dir_template=str(store.runs_dir / "{id}"),
        )
        store.set_process(
            run_id, pid=process.pid, pgid=process.pid, proc_start=identity
        )

        report = recover_interrupted_runs(store)

        assert report.interrupted == 1
        assert report.killed_process_groups == (process.pid,)
        assert report.skipped_reused_pids == ()
        assert process.wait(timeout=15) is not None
        run = store.get_run(run_id)
        assert run is not None
        assert run.state == INTERRUPTED
    finally:
        if process is not None and process.poll() is None:
            process.kill()
            process.wait(timeout=10)
        store.close()


def test_recovery_does_not_kill_a_reused_pid(tmp_path: Path) -> None:
    store = _store(tmp_path)
    process: subprocess.Popen[bytes] | None = None
    try:
        run_id, process = _active_run_with_process(store, proc_start="stale:0")

        report = recover_interrupted_runs(store)

        assert report.interrupted == 1
        assert report.killed_process_groups == ()
        assert report.skipped_reused_pids == (process.pid,)
        # The unrelated live process survives.
        assert process.poll() is None
        run = store.get_run(run_id)
        assert run is not None
        assert run.state == INTERRUPTED
    finally:
        if process is not None and process.poll() is None:
            process.kill()
            process.wait(timeout=10)
        store.close()
