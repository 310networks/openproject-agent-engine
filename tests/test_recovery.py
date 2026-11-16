"""Tests for startup crash recovery."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from op_agent_engine.recovery import (
    process_start_identity,
    reconcile_stale_runs,
    recover_interrupted_runs,
)
from op_agent_engine.store import INTERRUPTED, RUNNING, Store


def test_drained_run_keeps_budget_and_can_be_claimed_after_restart(tmp_path: Path) -> None:
    store = Store(tmp_path / "engine.db")
    try:
        def claim() -> int:
            return store.begin_run(
                work_package_id=100, project_id=4, agent="IMPL", kind="resume",
                attempt=1, branch="op-4-100",
                run_dir_template=str(store.runs_dir / "{id}"),
            )

        run_id = claim()
        store.increment_crash_count(100)
        store.mark_interrupted(run_id, outcome="drained")
        assert recover_interrupted_runs(store).interrupted == 0
        assert store.crash_count(100) == 1
        assert store.last_interrupted_run(100).id == run_id
        assert claim() != run_id
    finally:
        store.close()


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


@pytest.mark.parametrize(
    ("current", "meta", "reason"),
    [
        (None, None, "dead_pid"),
        ("different:1", None, "pid_reused"),
        ("recorded:1", {"state": "running", "finished_at": "2026-11-14T00:00:00Z"},
         "finished_meta"),
        ("recorded:1", None, None),
        ("recorded:1", {"state": "running", "finished_at": None}, None),
        ("recorded:1", [], None),
    ],
)
def test_poll_reconciliation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    current: str | None,
    meta: object,
    reason: str | None,
) -> None:
    store = _store(tmp_path)
    try:
        run_id = store.begin_run(
            work_package_id=100, project_id=4, agent="IMPL", kind="implement",
            attempt=1, branch="op-4-100",
            run_dir_template=str(store.runs_dir / "{id}"),
        )
        store.set_process(run_id, pid=123, pgid=123, proc_start="recorded:1")
        monkeypatch.setattr("op_agent_engine.recovery.process_start_identity", lambda _: current)
        # Poll-time recovery must never signal a PID, including a reused one.
        def unexpected_kill(*args: object) -> None:
            pytest.fail("poll reconciliation signalled a process")

        monkeypatch.setattr("op_agent_engine.recovery.os.killpg", unexpected_kill)
        if meta is not None:
            run_dir = store.runs_dir / str(run_id)
            run_dir.mkdir(parents=True)
            (run_dir / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
        store.increment_crash_count(100)
        assert reconcile_stale_runs(store) == int(reason is not None)
        run = store.get_run(run_id)
        assert run is not None
        assert run.state == (INTERRUPTED if reason else RUNNING)
        assert store.crash_count(100) == 1
        if reason:
            assert run.outcome == f"reconciled_{reason}"
            assert run.finished_at is not None
            assert len(caplog.records) == 1
            assert reason in caplog.text
            assert reconcile_stale_runs(store) == 0
            assert len(caplog.records) == 1
            assert store.active_run_count() == 0
            # The unique partial index no longer prevents a new claim.
            store.begin_run(
                work_package_id=100, project_id=4, agent="IMPL", kind="resume",
                attempt=2, branch="op-4-100", run_dir_template="unused/{id}",
            )
        else:
            assert run.finished_at is None
            assert not caplog.records
    finally:
        store.close()


@pytest.mark.parametrize("meta", ["{invalid", "[]", '{"state":"running","finished_at":null}'])
def test_missing_identity_and_invalid_meta_are_not_proof_of_an_orphan(
    tmp_path: Path, meta: str
) -> None:
    store = _store(tmp_path)
    try:
        run_id = store.begin_run(
            work_package_id=100, project_id=4, agent="IMPL", kind="implement",
            attempt=1, branch="op-4-100", run_dir_template=str(store.runs_dir / "{id}"),
        )
        run_dir = store.runs_dir / str(run_id)
        run_dir.mkdir(parents=True)
        (run_dir / "meta.json").write_text(meta, encoding="utf-8")
        assert reconcile_stale_runs(store) == 0
        assert store.active_run_count() == 1
    finally:
        store.close()


@pytest.mark.parametrize("spawned", [False, True])
def test_executing_dispatcher_keeps_ownership(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, spawned: bool
) -> None:
    store = _store(tmp_path)
    try:
        run_id = store.begin_run(
            work_package_id=100, project_id=4, agent="IMPL", kind="implement",
            attempt=1, branch="op-4-100", run_dir_template=str(store.runs_dir / "{id}"),
        )
        if spawned:
            store.set_process(run_id, pid=123, pgid=123, proc_start="recorded:1")
        run_dir = store.runs_dir / str(run_id)
        run_dir.mkdir(parents=True)
        (run_dir / "meta.json").write_text(
            '{"state":"running","finished_at":"2026-11-14T00:00:00Z"}', encoding="utf-8"
        )
        monkeypatch.setattr("op_agent_engine.recovery.process_start_identity", lambda _: None)
        assert reconcile_stale_runs(store, executing_run_ids=frozenset({run_id})) == 0
        assert store.active_run_count() == 1
        # After dispatch exits, the finished meta is sufficient even without a PID.
        assert reconcile_stale_runs(store) == 1
        run = store.get_run(run_id)
        assert run is not None and run.outcome == "reconciled_finished_meta"
    finally:
        store.close()


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
