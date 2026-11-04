"""Tests for the SQLite run store and the engine lock."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from op_agent_engine.store import (
    CANCELLED,
    FAILED,
    INTERRUPTED,
    RUNNING,
    SUCCEEDED,
    TIMEOUT,
    ClaimConflictError,
    EngineLock,
    Store,
)

TEMPLATE = "/tmp/runs/{id}"


def begin(
    store: Store,
    *,
    work_package_id: int = 100,
    project_id: int = 4,
    agent: str = "IMPL",
    kind: str = "implement",
    attempt: int = 1,
    branch: str | None = None,
) -> int:
    return store.begin_run(
        work_package_id=work_package_id,
        project_id=project_id,
        agent=agent,
        kind=kind,
        attempt=attempt,
        branch=branch or f"op-{project_id}-{work_package_id}",
        run_dir_template=TEMPLATE,
    )


@pytest.fixture
def store(tmp_path: Path) -> Iterator[Store]:
    store = Store(tmp_path / "engine.db")
    yield store
    store.close()


# -- begin_run -------------------------------------------------------------


def test_begin_run_returns_id_and_fills_run_dir(store: Store) -> None:
    run_id = begin(store)
    assert run_id == 1

    run = store.get_run(run_id)
    assert run is not None
    assert run.state == RUNNING
    assert run.work_package_id == 100
    assert run.project_id == 4
    assert run.agent == "IMPL"
    assert run.kind == "implement"
    assert run.attempt == 1
    assert run.branch == "op-4-100"
    assert run.run_dir == "/tmp/runs/1"
    assert run.started_at
    assert run.finished_at is None
    assert run.pid is None


def test_run_ids_increment(store: Store) -> None:
    first = begin(store, work_package_id=1, agent="IMPL")
    second = begin(store, work_package_id=2, agent="REVIEWER")
    assert second == first + 1
    second_run = store.get_run(second)
    assert second_run is not None
    assert second_run.run_dir == f"/tmp/runs/{second}"


def test_double_claim_same_work_package_raises(store: Store) -> None:
    begin(store, work_package_id=100, agent="IMPL")
    with pytest.raises(ClaimConflictError):
        begin(store, work_package_id=100, agent="REVIEWER")


def test_second_active_run_for_same_agent_is_allowed(store: Store) -> None:
    # The scheduler enforces MAX_RUNS_PER_AGENT; the store only guards the work
    # package, so a configurable per-agent limit can exceed one.
    begin(store, work_package_id=100, agent="IMPL")
    begin(store, work_package_id=200, agent="IMPL")

    assert store.active_run_counts_by_agent() == {"IMPL": 2}


def test_conflict_does_not_leave_a_row(store: Store) -> None:
    begin(store, work_package_id=100, agent="IMPL")
    with pytest.raises(ClaimConflictError):
        begin(store, work_package_id=100, agent="REVIEWER")
    assert store.active_run_count() == 1


def test_finished_run_frees_the_slots(store: Store) -> None:
    run_id = begin(store, work_package_id=100, agent="IMPL")
    store.finish_run(run_id, state=SUCCEEDED, outcome="done", exit_code=0)

    # Both the work package and the agent can be claimed again.
    again = begin(store, work_package_id=100, agent="IMPL")
    assert again != run_id
    again_run = store.get_run(again)
    assert again_run is not None
    assert again_run.state == RUNNING


# -- finishing runs --------------------------------------------------------


@pytest.mark.parametrize(
    ("state", "exit_code"),
    [
        (SUCCEEDED, 0),
        (FAILED, 1),
        (TIMEOUT, None),
        (CANCELLED, None),
    ],
)
def test_finish_run_terminal_states(
    store: Store, state: str, exit_code: int | None
) -> None:
    run_id = begin(store)
    store.finish_run(
        run_id, state=state, outcome="outcome", exit_code=exit_code, result_line="ok"
    )

    run = store.get_run(run_id)
    assert run is not None
    assert run.state == state
    assert run.outcome == "outcome"
    assert run.exit_code == exit_code
    assert run.result_line == "ok"
    assert run.finished_at
    assert store.active_run_count() == 0


def test_set_process_records_process_identity(store: Store) -> None:
    run_id = begin(store)
    store.set_process(run_id, pid=4242, pgid=4242, proc_start="2026-01-01T00:00:00")

    run = store.get_run(run_id)
    assert run is not None
    assert run.pid == 4242
    assert run.pgid == 4242
    assert run.proc_start == "2026-01-01T00:00:00"


# -- crash recovery --------------------------------------------------------


def test_mark_interrupted(store: Store) -> None:
    run_id = begin(store)
    store.mark_interrupted(run_id, outcome="engine stopped")

    run = store.get_run(run_id)
    assert run is not None
    assert run.state == INTERRUPTED
    assert run.outcome == "engine stopped"


def test_mark_all_running_interrupted_counts_only_running(store: Store) -> None:
    first = begin(store, work_package_id=1, agent="IMPL")
    second = begin(store, work_package_id=2, agent="REVIEWER")
    store.finish_run(first, state=SUCCEEDED, outcome="done")

    assert store.mark_all_running_interrupted() == 1

    first_run = store.get_run(first)
    assert first_run is not None and first_run.state == SUCCEEDED
    second_run = store.get_run(second)
    assert second_run is not None and second_run.state == INTERRUPTED
    # Idempotent.
    assert store.mark_all_running_interrupted() == 0


# -- queries ---------------------------------------------------------------


def test_active_queries(store: Store) -> None:
    begin(store, work_package_id=100, agent="IMPL")
    begin(store, work_package_id=200, agent="REVIEWER")
    finished = begin(store, work_package_id=300, agent="DESIGNER")
    store.finish_run(finished, state=SUCCEEDED, outcome="done")

    assert store.active_run_count() == 2
    assert store.active_work_package_ids() == frozenset({100, 200})
    assert store.active_run_counts_by_agent() == {"IMPL": 1, "REVIEWER": 1}
    active = store.active_runs()
    assert [run.work_package_id for run in active] == [100, 200]


def test_finished_runs_newest_first(store: Store) -> None:
    first = begin(store, work_package_id=1, agent="IMPL")
    second = begin(store, work_package_id=2, agent="REVIEWER")
    store.finish_run(first, state=SUCCEEDED, outcome="a")
    store.finish_run(second, state=FAILED, outcome="b")

    finished = store.finished_runs()
    assert [run.id for run in finished] == [second, first]
    assert store.finished_runs(limit=1)[0].id == second


# -- crash counts ----------------------------------------------------------


def test_crash_count_defaults_to_zero_and_increments(store: Store) -> None:
    assert store.crash_count(100) == 0
    assert store.increment_crash_count(100) == 1
    assert store.increment_crash_count(100) == 2
    assert store.crash_count(100) == 2
    # Independent per work package.
    assert store.increment_crash_count(200) == 1
    assert store.crash_count(100) == 2


# -- persistence -----------------------------------------------------------


def test_state_survives_reopen(tmp_path: Path) -> None:
    path = tmp_path / "engine.db"
    store = Store(path)
    run_id = begin(store)
    store.set_process(run_id, pid=1, pgid=1, proc_start="t")
    store.close()

    reopened = Store(path)
    try:
        run = reopened.get_run(run_id)
        assert run is not None
        assert run.state == RUNNING
        assert run.pid == 1
        assert reopened.active_run_count() == 1
        # Crash recovery works on the reopened database.
        assert reopened.mark_all_running_interrupted() == 1
        recovered = reopened.get_run(run_id)
        assert recovered is not None and recovered.state == INTERRUPTED
    finally:
        reopened.close()


def test_in_memory_store(tmp_path: Path) -> None:
    store = Store(":memory:")
    try:
        run_id = begin(store)
        run = store.get_run(run_id)
        assert run is not None and run.state == RUNNING
        # No file is created anywhere.
        assert not (tmp_path / "engine.db").exists()
    finally:
        store.close()


def test_store_creates_parent_directories(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "dirs" / "engine.db"
    store = Store(path)
    try:
        assert path.exists()
        assert store.runs_dir == tmp_path / "nested" / "dirs" / "runs"
    finally:
        store.close()


# -- engine lock -----------------------------------------------------------


def test_engine_lock_single_instance(tmp_path: Path) -> None:
    path = tmp_path / "engine.lock"
    first = EngineLock(path)
    second = EngineLock(path)

    assert first.acquire() is True
    assert second.acquire() is False
    # Re-acquiring an already-held lock is a no-op success.
    assert first.acquire() is True

    first.release()
    assert second.acquire() is True
    second.release()
    # Releasing twice is safe.
    second.release()


def test_engine_lock_is_released_when_the_fd_closes(tmp_path: Path) -> None:
    import os

    path = tmp_path / "engine.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    import fcntl

    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

    contender = EngineLock(path)
    assert contender.acquire() is False
    # Simulate the holder dying: the kernel drops the flock on close.
    os.close(fd)
    assert contender.acquire() is True
    contender.release()


def test_engine_lock_context_manager(tmp_path: Path) -> None:
    path = tmp_path / "engine.lock"
    with EngineLock(path) as held:
        assert held.acquire() is True
        with pytest.raises(RuntimeError), EngineLock(path):
            pass


# -- dashboard metadata and aggregates ------------------------------------


def test_begin_run_records_ticket_metadata(store: Store) -> None:
    run_id = store.begin_run(
        work_package_id=77,
        project_id=4,
        agent="BACKEND",
        kind="implement",
        attempt=1,
        branch="op-4-77",
        run_dir_template=TEMPLATE,
        display_id="#77",
        subject="Build the dashboard",
    )

    run = store.get_run(run_id)
    assert run is not None
    assert run.display_id == "#77"
    assert run.subject == "Build the dashboard"


def test_state_and_outcome_counts(store: Store) -> None:
    running = begin(store, work_package_id=1, agent="IMPL")
    completed = begin(store, work_package_id=2, agent="REVIEWER")
    declined = begin(store, work_package_id=3, agent="DESIGNER")
    failed = begin(store, work_package_id=4, agent="OTHER")
    store.finish_run(completed, state=SUCCEEDED, outcome="approved")
    store.finish_run(declined, state=SUCCEEDED, outcome="rejected")
    store.finish_run(failed, state=FAILED, outcome="nonzero_exit")

    assert store.state_counts() == {
        RUNNING: 1,
        SUCCEEDED: 2,
        FAILED: 1,
    }
    assert store.outcome_counts() == {
        "approved": 1,
        "rejected": 1,
        "nonzero_exit": 1,
    }
    assert store.get_run(running) is not None


def test_agent_counts_split_states_and_outcomes(store: Store) -> None:
    first = begin(store, work_package_id=1, agent="IMPL")
    store.finish_run(first, state=SUCCEEDED, outcome="ready_for_review")
    second = begin(store, work_package_id=2, agent="IMPL")
    third = begin(store, work_package_id=3, agent="REVIEWER")
    store.finish_run(second, state=FAILED, outcome="nonzero_exit")
    store.finish_run(third, state=SUCCEEDED, outcome="approved")

    counts = store.agent_counts()
    assert counts["IMPL"][SUCCEEDED] == 1
    assert counts["IMPL"][FAILED] == 1
    assert counts["IMPL"]["outcome:ready_for_review"] == 1
    assert counts["IMPL"]["outcome:nonzero_exit"] == 1
    assert counts["REVIEWER"][SUCCEEDED] == 1
    assert counts["REVIEWER"]["outcome:approved"] == 1


def test_last_finished_run_by_agent(store: Store) -> None:
    first = begin(store, work_package_id=1, agent="IMPL")
    store.finish_run(first, state=SUCCEEDED, outcome="ready_for_review")
    second = begin(store, work_package_id=2, agent="IMPL")
    other = begin(store, work_package_id=3, agent="REVIEWER")
    store.finish_run(other, state=SUCCEEDED, outcome="approved")
    store.finish_run(second, state=FAILED, outcome="nonzero_exit")

    last = store.last_finished_run_by_agent()
    assert set(last) == {"IMPL", "REVIEWER"}
    assert last["IMPL"].id == second
    assert last["REVIEWER"].id == other


def test_store_migrates_a_database_created_before_dashboard_columns(
    tmp_path: Path,
) -> None:
    import sqlite3

    path = tmp_path / "old.db"
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE runs (
          id INTEGER PRIMARY KEY,
          work_package_id INTEGER NOT NULL,
          project_id INTEGER NOT NULL,
          agent TEXT NOT NULL,
          kind TEXT NOT NULL,
          state TEXT NOT NULL,
          attempt INTEGER NOT NULL,
          branch TEXT NOT NULL,
          pid INTEGER,
          pgid INTEGER,
          proc_start TEXT,
          started_at TEXT NOT NULL,
          finished_at TEXT,
          exit_code INTEGER,
          result_line TEXT,
          outcome TEXT,
          run_dir TEXT NOT NULL
        );
        INSERT INTO runs (
          work_package_id, project_id, agent, kind, state, attempt, branch,
          started_at, run_dir
        ) VALUES (7, 4, 'IMPL', 'implement', 'succeeded', 1, 'op-4-7',
                  '2026-01-01T00:00:00+00:00', '/tmp/runs/1');
        CREATE TABLE crash_counts (
          work_package_id INTEGER PRIMARY KEY,
          count INTEGER NOT NULL DEFAULT 0
        );
        """
    )
    connection.commit()
    connection.close()

    reopened = Store(path)
    try:
        run = reopened.get_run(1)
        assert run is not None
        assert run.display_id is None
        assert run.subject is None
        new_id = begin(reopened, work_package_id=8, agent="REVIEWER")
        new_run = reopened.get_run(new_id)
        assert new_run is not None
        assert new_run.display_id is None  # helper did not pass one
    finally:
        reopened.close()


# -- read-only readers -----------------------------------------------------


def test_read_only_store_reads_without_writing(tmp_path: Path) -> None:
    import sqlite3

    path = tmp_path / "engine.db"
    writer = Store(path)
    run_id = writer.begin_run(
        work_package_id=9,
        project_id=4,
        agent="IMPL",
        kind="implement",
        attempt=1,
        branch="op-4-9",
        run_dir_template=TEMPLATE,
        display_id="#9",
        subject="Read me",
    )
    writer.finish_run(run_id, state=SUCCEEDED, outcome="approved")
    writer.close()

    reader = Store(path, read_only=True)
    try:
        assert reader.read_only is True
        run = reader.get_run(run_id)
        assert run is not None
        assert run.subject == "Read me"
        assert reader.state_counts() == {SUCCEEDED: 1}
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            reader.increment_crash_count(9)
    finally:
        reader.close()


def test_read_only_store_tolerates_a_pre_migration_schema(
    tmp_path: Path,
) -> None:
    import sqlite3

    path = tmp_path / "old.db"
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE runs (
          id INTEGER PRIMARY KEY,
          work_package_id INTEGER NOT NULL,
          project_id INTEGER NOT NULL,
          agent TEXT NOT NULL,
          kind TEXT NOT NULL,
          state TEXT NOT NULL,
          attempt INTEGER NOT NULL,
          branch TEXT NOT NULL,
          pid INTEGER,
          pgid INTEGER,
          proc_start TEXT,
          started_at TEXT NOT NULL,
          finished_at TEXT,
          exit_code INTEGER,
          result_line TEXT,
          outcome TEXT,
          run_dir TEXT NOT NULL
        );
        INSERT INTO runs (
          work_package_id, project_id, agent, kind, state, attempt, branch,
          started_at, run_dir
        ) VALUES (7, 4, 'IMPL', 'implement', 'succeeded', 1, 'op-4-7',
                  '2026-01-01T00:00:00+00:00', '/tmp/runs/1');
        """
    )
    connection.commit()
    connection.close()

    reader = Store(path, read_only=True)
    try:
        run = reader.get_run(1)
        assert run is not None
        assert run.display_id is None
        assert run.subject is None
    finally:
        reader.close()

    # The read-only reader must not have migrated the file.
    check = sqlite3.connect(path)
    try:
        columns = {row[1] for row in check.execute("PRAGMA table_info(runs)")}
    finally:
        check.close()
    assert "display_id" not in columns
    assert "subject" not in columns


def test_read_only_store_requires_an_existing_file(tmp_path: Path) -> None:
    import sqlite3

    with pytest.raises(sqlite3.OperationalError):
        Store(tmp_path / "missing.db", read_only=True)


# -- crash counter reset ----------------------------------------------------


def test_reset_crash_count_returns_the_previous_value(store: Store) -> None:
    store.increment_crash_count(100)
    store.increment_crash_count(100)

    assert store.reset_crash_count(100) == 2
    assert store.crash_count(100) == 0
    # Idempotent: a second reset has nothing to delete.
    assert store.reset_crash_count(100) == 0
