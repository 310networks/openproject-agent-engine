"""SQLite persistence for runs and crash counters (PLAN section 12).

The store is the engine's single source of truth for which work packages and
agents have active runs. The unique partial indexes make double-claiming a
work package or starting a second run for the same agent impossible at the
database level: the insert simply fails.

The engine runs as a single instance; :class:`EngineLock` guards startup with
an exclusive non-blocking ``flock`` on ``DATA_DIR/engine.lock`` (PLAN §9).
"""

from __future__ import annotations

import fcntl
import os
import sqlite3
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

# Run states (PLAN section 12).
RUNNING = "running"
SUCCEEDED = "succeeded"
FAILED = "failed"
TIMEOUT = "timeout"
CANCELLED = "cancelled"
INTERRUPTED = "interrupted"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
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
CREATE UNIQUE INDEX IF NOT EXISTS one_active_per_wp
  ON runs(work_package_id) WHERE state = 'running';
CREATE UNIQUE INDEX IF NOT EXISTS one_active_per_agent
  ON runs(agent) WHERE state = 'running';

CREATE TABLE IF NOT EXISTS crash_counts (
  work_package_id INTEGER PRIMARY KEY,
  count INTEGER NOT NULL DEFAULT 0
);
"""

_RUN_COLUMNS = (
    "id",
    "work_package_id",
    "project_id",
    "agent",
    "kind",
    "state",
    "attempt",
    "branch",
    "pid",
    "pgid",
    "proc_start",
    "started_at",
    "finished_at",
    "exit_code",
    "result_line",
    "outcome",
    "run_dir",
)


class ClaimConflictError(Exception):
    """A work package or agent already has an active run."""


@dataclass(frozen=True)
class Run:
    id: int
    work_package_id: int
    project_id: int
    agent: str
    kind: str
    state: str
    attempt: int
    branch: str
    pid: int | None
    pgid: int | None
    proc_start: str | None
    started_at: str
    finished_at: str | None
    exit_code: int | None
    result_line: str | None
    outcome: str | None
    run_dir: str


def _utcnow() -> str:
    return datetime.now(UTC).isoformat()


def _row_to_run(row: sqlite3.Row) -> Run:
    return Run(**{column: row[column] for column in _RUN_COLUMNS})


class Store:
    """SQLite-backed run state.

    All methods are synchronous and safe to call from the asyncio event loop:
    operations are short local file accesses.

    Pass the string ``":memory:"`` for an ephemeral store (dry-run mode).
    """

    def __init__(self, path: Path | str) -> None:
        in_memory = isinstance(path, str) and path == ":memory:"
        if in_memory:
            self._path = Path("__in_memory__")
        else:
            self._path = path if isinstance(path, Path) else Path(path)
            self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(
            ":memory:" if in_memory else str(self._path), check_same_thread=False
        )
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(_SCHEMA)
            self._conn.commit()

    @property
    def path(self) -> Path:
        return self._path

    @property
    def runs_dir(self) -> Path:
        """Directory for per-run artifacts (``DATA_DIR/runs/<runId>/``)."""
        if self._path == Path("__in_memory__"):
            return Path("__in_memory__") / "runs"
        return self._path.parent / "runs"

    # -- lifecycle -------------------------------------------------------

    def begin_run(
        self,
        *,
        work_package_id: int,
        project_id: int,
        agent: str,
        kind: str,
        attempt: int,
        branch: str,
        run_dir_template: str,
    ) -> int:
        """Insert a ``running`` run row and return its id.

        Raises :class:`ClaimConflictError` when the work package or the agent
        already has a ``running`` row (unique partial indexes).

        ``run_dir_template`` may contain a single ``{id}`` placeholder, which
        is filled with the new run id (the run directory is named after the
        run).
        """
        try:
            with self._lock:
                cursor = self._conn.execute(
                    """
                    INSERT INTO runs (
                      work_package_id, project_id, agent, kind, state, attempt,
                      branch, started_at, run_dir
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        work_package_id,
                        project_id,
                        agent,
                        kind,
                        RUNNING,
                        attempt,
                        branch,
                        _utcnow(),
                        run_dir_template,
                    ),
                )
                run_id = cursor.lastrowid
                assert run_id is not None
                self._conn.execute(
                    "UPDATE runs SET run_dir = ? WHERE id = ?",
                    (run_dir_template.format(id=run_id), run_id),
                )
                self._conn.commit()
        except sqlite3.IntegrityError as error:
            raise ClaimConflictError(
                f"work package {work_package_id} or agent '{agent}' already has "
                "an active run"
            ) from error
        return run_id

    def set_process(
        self, run_id: int, *, pid: int, pgid: int, proc_start: str
    ) -> None:
        """Record the started process (PID, process group, start time)."""
        with self._lock:
            self._conn.execute(
                "UPDATE runs SET pid = ?, pgid = ?, proc_start = ? WHERE id = ?",
                (pid, pgid, proc_start, run_id),
            )
            self._conn.commit()

    def finish_run(
        self,
        run_id: int,
        *,
        state: str,
        outcome: str,
        exit_code: int | None = None,
        result_line: str | None = None,
    ) -> None:
        """Transition a run out of ``running`` into a terminal state."""
        self._update_run(
            run_id,
            state=state,
            finished_at=_utcnow(),
            outcome=outcome,
            exit_code=exit_code,
            result_line=result_line,
        )

    def cancel_run(self, run_id: int, *, outcome: str) -> None:
        """Mark a run ``cancelled`` (claim abandoned or failed)."""
        self.finish_run(run_id, state=CANCELLED, outcome=outcome)

    def mark_interrupted(self, run_id: int, *, outcome: str | None = None) -> None:
        """Mark a run ``interrupted`` (engine stopped while it was running)."""
        self._update_run(
            run_id, state=INTERRUPTED, finished_at=_utcnow(), outcome=outcome
        )

    def mark_all_running_interrupted(self) -> int:
        """Mark every ``running`` row ``interrupted``; return how many."""
        with self._lock:
            cursor = self._conn.execute(
                "UPDATE runs SET state = ?, finished_at = ? WHERE state = ?",
                (INTERRUPTED, _utcnow(), RUNNING),
            )
            self._conn.commit()
            return cursor.rowcount

    def _update_run(self, run_id: int, **fields: object) -> None:
        assignments = ", ".join(f"{name} = ?" for name in fields)
        with self._lock:
            self._conn.execute(
                f"UPDATE runs SET {assignments} WHERE id = ?",
                (*fields.values(), run_id),
            )
            self._conn.commit()

    # -- queries ---------------------------------------------------------

    def get_run(self, run_id: int) -> Run | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM runs WHERE id = ?", (run_id,)
            ).fetchone()
        return _row_to_run(row) if row is not None else None

    def active_runs(self) -> list[Run]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM runs WHERE state = ? ORDER BY id", (RUNNING,)
            ).fetchall()
        return [_row_to_run(row) for row in rows]

    def active_run_count(self) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) AS n FROM runs WHERE state = ?", (RUNNING,)
            ).fetchone()
        return int(row["n"])

    def active_work_package_ids(self) -> frozenset[int]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT work_package_id FROM runs WHERE state = ?", (RUNNING,)
            ).fetchall()
        return frozenset(int(row["work_package_id"]) for row in rows)

    def active_agent_keys(self) -> frozenset[str]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT agent FROM runs WHERE state = ?", (RUNNING,)
            ).fetchall()
        return frozenset(str(row["agent"]) for row in rows)

    def finished_runs(self, limit: int = 20) -> list[Run]:
        """Most recently finished runs, newest first."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM runs WHERE state != ? ORDER BY finished_at DESC, id DESC LIMIT ?",
                (RUNNING, limit),
            ).fetchall()
        return [_row_to_run(row) for row in rows]

    # -- crash counts ----------------------------------------------------

    def crash_count(self, work_package_id: int) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT count FROM crash_counts WHERE work_package_id = ?",
                (work_package_id,),
            ).fetchone()
        return int(row["count"]) if row is not None else 0

    def increment_crash_count(self, work_package_id: int) -> int:
        """Bump the crash counter and return the new value."""
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO crash_counts (work_package_id, count) VALUES (?, 1)
                ON CONFLICT(work_package_id)
                DO UPDATE SET count = count + 1
                """,
                (work_package_id,),
            )
            row = self._conn.execute(
                "SELECT count FROM crash_counts WHERE work_package_id = ?",
                (work_package_id,),
            ).fetchone()
            self._conn.commit()
        return int(row["count"])

    def close(self) -> None:
        with self._lock:
            self._conn.close()


class EngineLock:
    """Exclusive single-instance lock on ``DATA_DIR/engine.lock``.

    Uses a non-blocking ``flock``: acquisition fails immediately when another
    engine instance holds the lock. The lock is released automatically if the
    process dies (the kernel drops the flock when the fd closes).
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._fd: int | None = None

    @property
    def path(self) -> Path:
        return self._path

    def acquire(self) -> bool:
        """Try to take the lock. Returns False when another instance holds it."""
        if self._fd is not None:
            return True
        self._path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self._path, os.O_RDWR | os.O_CREAT, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(fd)
            return False
        os.ftruncate(fd, 0)
        os.write(fd, f"{os.getpid()}\n".encode())
        self._fd = fd
        return True

    def release(self) -> None:
        if self._fd is None:
            return
        fcntl.flock(self._fd, fcntl.LOCK_UN)
        os.close(self._fd)
        self._fd = None

    def __enter__(self) -> EngineLock:
        if not self.acquire():
            raise RuntimeError(f"engine lock is already held: {self._path}")
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.release()
