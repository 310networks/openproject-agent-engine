"""SQLite persistence for runs and crash counters.

The store is the engine's single source of truth for which work packages and
agents have active runs. A unique partial index makes double-claiming a work
package impossible at the database level: the insert simply fails. The
per-agent capacity is enforced by the scheduler (``MAX_RUNS_PER_AGENT``).

The engine runs as a single instance; :class:`EngineLock` guards startup with
an exclusive non-blocking ``flock`` on ``DATA_DIR/engine.lock``.
"""

from __future__ import annotations

import fcntl
import os
import sqlite3
import threading
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

# Run artifact directory name.
RUNS_DIR_NAME = "runs"

# Run states.
RUNNING = "running"
SUCCEEDED = "succeeded"
FAILED = "failed"
TIMEOUT = "timeout"
CANCELLED = "cancelled"
INTERRUPTED = "interrupted"

#: Outcomes that are deliberate checkpoints rather than delivery or failure.
#: They never consume the crash retry budget and keep the ticket In Progress.
CHECKPOINT_OUTCOMES = frozenset(
    {"progress_not_finished", "waiting_ci", "checkpoint_no_progress"}
)

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
  display_id TEXT,
  subject TEXT,
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

CREATE TABLE IF NOT EXISTS crash_counts (
  work_package_id INTEGER PRIMARY KEY,
  count INTEGER NOT NULL DEFAULT 0
);

-- A resume hold parks an In Progress ticket until an external event (the
-- commit's CI checks settling) instead of spending an agent run to find out.
-- The hold is advisory: the scheduler skips the ticket only while ready_at is
-- in the future, and the engine's resume gate clears it when the checks
-- finish or when the evidence is unusable.
-- Separate task convergence from cheap CI polling. No historical sessions
-- are backfilled on upgrade; these counters start with the next checkpoint.
CREATE TABLE IF NOT EXISTS checkpoint_unholds (
  work_package_id INTEGER PRIMARY KEY,
  event_id TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS checkpoint_progress (
  work_package_id INTEGER PRIMARY KEY,
  head_sha TEXT,
  stagnant INTEGER NOT NULL DEFAULT 0,
  revisions INTEGER NOT NULL DEFAULT 0,
  published_head TEXT
);
CREATE TABLE IF NOT EXISTS checkpoint_milestones (
  work_package_id INTEGER NOT NULL,
  milestone TEXT NOT NULL,
  PRIMARY KEY (work_package_id, milestone)
);
CREATE TABLE IF NOT EXISTS checkpoint_diagnoses (
  work_package_id INTEGER NOT NULL,
  head_sha TEXT NOT NULL,
  fingerprint TEXT NOT NULL,
  PRIMARY KEY (work_package_id, head_sha, fingerprint)
);
CREATE TABLE IF NOT EXISTS checkpoint_registrations (
  work_package_id INTEGER NOT NULL,
  repo TEXT NOT NULL,
  head_sha TEXT NOT NULL,
  PRIMARY KEY (work_package_id, repo, head_sha)
);
CREATE TABLE IF NOT EXISTS resume_holds (
  work_package_id INTEGER PRIMARY KEY,
  repo TEXT NOT NULL,
  head_sha TEXT,
  ready_at TEXT NOT NULL,
  reason TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  attempts INTEGER NOT NULL DEFAULT 0,
  diagnosis_allowed INTEGER NOT NULL DEFAULT 0,
  pr_branch TEXT
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
    "display_id",
    "subject",
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
    """A work package already has an active run."""


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
    display_id: str | None
    subject: str | None
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


@dataclass(frozen=True)
class ResumeHold:
    """A parked resume: the ticket is skipped until ``ready_at``."""

    work_package_id: int
    repo: str
    head_sha: str | None
    ready_at: str
    reason: str
    created_at: str
    updated_at: str
    attempts: int
    diagnosis_allowed: bool = False
    pr_branch: str | None = None

    def ready(self, now: datetime | None = None) -> bool:
        """Whether the hold has expired (``ready_at`` is in the past)."""
        current = now or datetime.now(UTC)
        try:
            ready_at = datetime.fromisoformat(self.ready_at)
        except ValueError:
            return True
        if ready_at.tzinfo is None:
            ready_at = ready_at.replace(tzinfo=UTC)
        return ready_at <= current


def _row_to_resume_hold(row: sqlite3.Row) -> ResumeHold:
    return ResumeHold(
        work_package_id=int(row["work_package_id"]),
        repo=str(row["repo"]),
        head_sha=row["head_sha"],
        ready_at=str(row["ready_at"]),
        reason=str(row["reason"]),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
        attempts=int(row["attempts"]),
        diagnosis_allowed=(bool(row["diagnosis_allowed"])
                           if "diagnosis_allowed" in set(row.keys()) else False),
        pr_branch=row["pr_branch"] if "pr_branch" in set(row.keys()) else None,
    )


def _row_to_run(row: sqlite3.Row) -> Run:
    # A read-only reader may open a database created before a column was
    # added (the engine migrates it on its next start); treat it as NULL so
    # the dashboard can still show the old history.
    available = set(row.keys())
    values: dict[str, Any] = {
        column: row[column] if column in available else None
        for column in _RUN_COLUMNS
    }
    return Run(**values)


class Store:
    """SQLite-backed run state.

    All methods are synchronous and safe to call from the asyncio event loop:
    operations are short local file accesses.

    Pass the string ``":memory:"`` for an ephemeral store (dry-run mode).
    """

    def __init__(self, path: Path | str, *, read_only: bool = False) -> None:
        in_memory = isinstance(path, str) and path == ":memory:"
        if in_memory:
            self._path = Path("__in_memory__")
        else:
            self._path = path if isinstance(path, Path) else Path(path)
            if not read_only:
                self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._read_only = read_only
        if read_only:
            if in_memory:
                raise ValueError("a read-only store cannot be in-memory")
            self._conn = sqlite3.connect(
                f"file:{self._path}?mode=ro",
                uri=True,
                check_same_thread=False,
            )
            self._conn.row_factory = sqlite3.Row
            return
        self._conn = sqlite3.connect(
            ":memory:" if in_memory else str(self._path), check_same_thread=False
        )
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(_SCHEMA)
            self._migrate()
            self._conn.commit()

    def _migrate(self) -> None:
        """Add columns introduced after the database was first created.

        ``CREATE TABLE IF NOT EXISTS`` does not alter an existing table, and
        the run history must survive an upgrade (it is the dashboard's data
        source). Adding a nullable column is safe and cheap.
        """
        columns = {
            str(row["name"])
            for row in self._conn.execute("PRAGMA table_info(runs)").fetchall()
        }
        for name in ("display_id", "subject"):
            if name not in columns:
                self._conn.execute(f"ALTER TABLE runs ADD COLUMN {name} TEXT")
        progress_columns = {str(r["name"]) for r in
                            self._conn.execute("PRAGMA table_info(checkpoint_progress)")}
        if "published_head" not in progress_columns:
            self._conn.execute("ALTER TABLE checkpoint_progress ADD COLUMN published_head TEXT")
            # Conservative first observation: do not re-credit a legacy matching
            # source as new evidence. This column is never used as a polling ID.
            self._conn.execute("UPDATE checkpoint_progress SET published_head = head_sha")
        hold_columns = {str(r["name"]) for r in
                        self._conn.execute("PRAGMA table_info(resume_holds)")}
        if "diagnosis_allowed" not in hold_columns:
            self._conn.execute(
                "ALTER TABLE resume_holds ADD COLUMN diagnosis_allowed INTEGER NOT NULL DEFAULT 0"
            )
        if "pr_branch" not in hold_columns:
            self._conn.execute("ALTER TABLE resume_holds ADD COLUMN pr_branch TEXT")
        # ``MAX_RUNS_PER_AGENT`` replaced the one-run-per-agent database rule;
        # drop the index so an existing deployment can run more than one.
        self._conn.execute("DROP INDEX IF EXISTS one_active_per_agent")

        # The checkpoint wait cap only exists since 0.9.x. Seeded once so the
        # checkpoints a long ticket recorded *before* the cap existed do not
        # count against it on the first run after the upgrade (that put six
        # tickets straight into On Hold at the cap). A later un-hold resets the
        # baseline again through ``reset_checkpoint_streak``.
        seeded = self._conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' "
            "AND name = 'checkpoint_baselines'"
        ).fetchone() is not None
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS checkpoint_baselines ("
            "  work_package_id INTEGER PRIMARY KEY,"
            "  after_run_id INTEGER NOT NULL"
            ")"
        )
        if not seeded:
            self._conn.execute(
                "INSERT OR REPLACE INTO checkpoint_baselines "
                "(work_package_id, after_run_id) "
                "SELECT work_package_id, MAX(id) FROM runs GROUP BY work_package_id"
            )

    @property
    def path(self) -> Path:
        return self._path

    @property
    def read_only(self) -> bool:
        """Whether this store was opened with ``mode=ro``."""
        return self._read_only

    @property
    def runs_dir(self) -> Path:
        """Directory for per-run artifacts (``DATA_DIR/runs/<runId>/``)."""
        if self._path == Path("__in_memory__"):
            return Path("__in_memory__") / "runs"
        return self._path.parent / RUNS_DIR_NAME

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
        display_id: str | None = None,
        subject: str | None = None,
    ) -> int:
        """Insert a ``running`` run row and return its id.

        Raises :class:`ClaimConflictError` when the work package already has a
        ``running`` row (unique partial index). The per-agent limit is enforced
        by the scheduler, because it is configurable (``MAX_RUNS_PER_AGENT``).

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
                      branch, display_id, subject, started_at, run_dir
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        work_package_id,
                        project_id,
                        agent,
                        kind,
                        RUNNING,
                        attempt,
                        branch,
                        display_id,
                        subject,
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
                f"work package {work_package_id} already has an active run"
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

    def set_attempt(self, run_id: int, attempt: int) -> None:
        """Record the run's attempt number (fix passes count rejections)."""
        self._update_run(run_id, attempt=attempt)

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

    def active_run_counts_by_agent(self) -> dict[str, int]:
        """Running runs per agent key (an agent with no run is absent)."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT agent, COUNT(*) AS n FROM runs WHERE state = ? GROUP BY agent",
                (RUNNING,),
            ).fetchall()
        return {str(row["agent"]): int(row["n"]) for row in rows}

    def finished_runs(self, limit: int = 20) -> list[Run]:
        """Most recently finished runs, newest first."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM runs WHERE state != ? ORDER BY finished_at DESC, id DESC LIMIT ?",
                (RUNNING, limit),
            ).fetchall()
        return [_row_to_run(row) for row in rows]

    def recent_ticket_runs(self, *, active: list[Run], ticket_limit: int,
                           history_limit: int) -> list[Run]:
        """Finished histories for the newest tickets not in the active snapshot."""
        active_ids = [run.work_package_id for run in active]
        placeholders = ",".join("?" for _ in active_ids)
        with self._lock:
            rows = self._conn.execute(
                f"""WITH ranked AS (
                    SELECT id, work_package_id, started_at,
                           ROW_NUMBER() OVER (PARTITION BY work_package_id
                                              ORDER BY started_at DESC, id DESC) AS position
                    FROM runs WHERE state != ? AND work_package_id NOT IN ({placeholders})
                ), tickets AS (
                    SELECT * FROM ranked WHERE position = 1
                    ORDER BY started_at DESC, id DESC LIMIT ?
                )
                SELECT runs.* FROM tickets
                JOIN ranked USING (work_package_id)
                JOIN runs ON runs.id = ranked.id
                WHERE ranked.position <= ?
                ORDER BY tickets.started_at DESC, tickets.id DESC, ranked.position""",
                (RUNNING, *active_ids, ticket_limit, history_limit),
            ).fetchall()
        return [_row_to_run(row) for row in rows]

    def last_review_run(self, work_package_id: int) -> Run | None:
        """The most recent finished review run for *work_package_id*.

        Used to tell whether the reviewer's latest verdict is still waiting
        for work: a resume run that skipped because the branch already had a
        pull request used to hide a rejection that the interrupted fix run
        never addressed.
        """
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM runs WHERE work_package_id = ? AND kind = ? "
                "AND state != ? AND outcome IS NOT NULL "
                "ORDER BY finished_at DESC, id DESC LIMIT 1",
                (work_package_id, "review", RUNNING),
            ).fetchone()
        return None if row is None else _row_to_run(row)

    def last_code_run(self, work_package_id: int) -> Run | None:
        """The most recent run that produced reviewable work, if any.

        Only a successful implement/fix/resume run carries the work to review
        (``ready_for_review``). A run that skipped because the PR was already
        open is deliberately excluded: it produced no code, so counting it
        would make an unaddressed rejection look handled.
        """
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM runs WHERE work_package_id = ? "
                "AND kind IN ('implement', 'fix', 'resume') "
                "AND outcome = 'ready_for_review' "
                "ORDER BY finished_at DESC, id DESC LIMIT 1",
                (work_package_id,),
            ).fetchone()
        return None if row is None else _row_to_run(row)

    def last_implementer_run(self, work_package_id: int) -> Run | None:
        """The most recent finished implement/fix/resume run, or None.

        Used to brief the next run: its result line, branch head, report and
        whether it ended at a checkpoint. Unlike :meth:`last_code_run` this
        includes checkpoints and failures, which is exactly what the resume
        prompt needs to know.
        """
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM runs WHERE work_package_id = ? "
                "AND kind IN ('implement', 'fix', 'resume') "
                "AND state != ? AND outcome IS NOT NULL "
                "ORDER BY id DESC LIMIT 1",
                (work_package_id, RUNNING),
            ).fetchone()
        return None if row is None else _row_to_run(row)

    def checkpoint_streak(self, work_package_id: int) -> int:
        """How many consecutive finished runs ended as checkpoints.

        The count stops at the first run with any other outcome, so a
        checkpoint streak measures one uninterrupted wait, not the ticket's
        whole history. Runs at or before the ticket's baseline do not count:
        that is how an upgrade (or a human moving the ticket off On Hold)
        starts a fresh budget instead of inheriting old checkpoints.
        """
        with self._lock:
            rows = self._conn.execute(
                "SELECT outcome FROM runs WHERE work_package_id = ? "
                "AND state != ? AND outcome IS NOT NULL "
                "AND id > COALESCE((SELECT after_run_id FROM checkpoint_baselines "
                "WHERE work_package_id = runs.work_package_id), 0) "
                "ORDER BY id DESC LIMIT 100",
                (work_package_id, RUNNING),
            ).fetchall()
        streak = 0
        for row in rows:
            if str(row["outcome"]) not in CHECKPOINT_OUTCOMES:
                break
            streak += 1
        return streak

    def reset_checkpoint_streak(
        self, work_package_id: int, *, after_run_id: int | None = None,
        unhold_event: str | None = None,
    ) -> int:
        """Start the checkpoint streak over after the newest run.

        Called when the ticket leaves On Hold: the human who un-held it has
        seen the cap comment, so a fresh ``MAX_CHECKPOINT_WAITS`` budget is the
        useful behaviour. Returns the baseline that was stored.
        """
        with self._lock:
            if unhold_event is not None:
                previous = self._conn.execute(
                    "SELECT event_id FROM checkpoint_unholds WHERE work_package_id = ?",
                    (work_package_id,),
                ).fetchone()
                if previous is not None and previous["event_id"] == unhold_event:
                    return 0
                self._conn.execute("INSERT OR REPLACE INTO checkpoint_unholds VALUES (?, ?)",
                                   (work_package_id, unhold_event))
                self._conn.execute("DELETE FROM crash_counts WHERE work_package_id = ?",
                                   (work_package_id,))
            if after_run_id is None:
                row = self._conn.execute(
                    "SELECT MAX(id) AS newest FROM runs WHERE work_package_id = ?",
                    (work_package_id,),
                ).fetchone()
                after_run_id = int(row["newest"] or 0) if row is not None else 0
            self._conn.execute(
                "INSERT INTO checkpoint_baselines (work_package_id, after_run_id) "
                "VALUES (?, ?) ON CONFLICT(work_package_id) DO UPDATE SET "
                "after_run_id = excluded.after_run_id",
                (work_package_id, after_run_id),
            )
            self._conn.execute(
                "DELETE FROM checkpoint_progress WHERE work_package_id = ?", (work_package_id,)
            )
            self._conn.execute(
                "DELETE FROM checkpoint_diagnoses WHERE work_package_id = ?", (work_package_id,)
            )
            self._conn.commit()
        return after_run_id

    def last_interrupted_run(self, work_package_id: int) -> Run | None:
        """Most recent restart checkpoint, separate from successful code runs."""
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM runs WHERE work_package_id = ? "
                "AND state = ? AND outcome = 'drained' ORDER BY id DESC LIMIT 1",
                (work_package_id, INTERRUPTED),
            ).fetchone()
        return None if row is None else _row_to_run(row)

    def state_counts(self) -> dict[str, int]:
        """Number of runs per state (``running`` included)."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT state, COUNT(*) AS n FROM runs GROUP BY state"
            ).fetchall()
        return {str(row["state"]): int(row["n"]) for row in rows}

    def outcome_counts(self) -> dict[str, int]:
        """Number of finished runs per outcome (``NULL`` outcomes omitted)."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT outcome, COUNT(*) AS n FROM runs "
                "WHERE outcome IS NOT NULL GROUP BY outcome"
            ).fetchall()
        return {str(row["outcome"]): int(row["n"]) for row in rows}

    def agent_counts(self) -> dict[str, dict[str, int]]:
        """Runs per agent, split by state and outcome.

        The shape is ``{agent: {state: n, ..., "outcome:<name>": n}}``. It is
        computed in one grouped query so the dashboard does not load the whole
        run history on every refresh.
        """
        with self._lock:
            rows = self._conn.execute(
                "SELECT agent, state, outcome, COUNT(*) AS n FROM runs "
                "GROUP BY agent, state, outcome"
            ).fetchall()
        counts: dict[str, dict[str, int]] = {}
        for row in rows:
            agent = str(row["agent"])
            entry = counts.setdefault(agent, {})
            state = str(row["state"])
            entry[state] = entry.get(state, 0) + int(row["n"])
            outcome = row["outcome"]
            if outcome is not None:
                key = f"outcome:{outcome}"
                entry[key] = entry.get(key, 0) + int(row["n"])
        return counts

    def last_finished_run_by_agent(self) -> dict[str, Run]:
        """The most recent finished run for each agent, keyed by agent."""
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT * FROM (
                  SELECT runs.*, ROW_NUMBER() OVER (
                    PARTITION BY agent ORDER BY finished_at DESC, id DESC
                  ) AS row_number
                  FROM runs WHERE state != ?
                ) WHERE row_number = 1
                """,
                (RUNNING,),
            ).fetchall()
        return {str(row["agent"]): _row_to_run(row) for row in rows}

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

    def checkpoint_progress(self, work_package_id: int) -> dict[str, object]:
        """Small durable delivery counters, distinct from passive hold attempts."""
        with self._lock:
            row = self._conn.execute(
                "SELECT head_sha, stagnant, revisions FROM checkpoint_progress "
                "WHERE work_package_id = ?", (work_package_id,),
            ).fetchone()
        return dict(row) if row is not None else {}

    def complete_checkpoint_episode(self, work_package_id: int) -> None:
        """A verified delivery handoff ends progress/diagnosis waits, not milestone history."""
        with self._lock:
            for table in ("checkpoint_progress", "checkpoint_diagnoses",
                          "checkpoint_registrations", "resume_holds"):
                self._conn.execute(
                    f"DELETE FROM {table} WHERE work_package_id = ?", (work_package_id,)
                )
            self._conn.commit()

    def latest_ci_diagnosis(self, work_package_id: int) -> dict[str, str] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT head_sha, fingerprint FROM checkpoint_diagnoses "
                "WHERE work_package_id = ? ORDER BY rowid DESC LIMIT 1", (work_package_id,),
            ).fetchone()
        return dict(row) if row is not None else None

    def claim_ci_diagnosis(self, work_package_id: int, head_sha: str, fingerprint: str) -> bool:
        """Persist a once-per-source/failure model wake, including across restarts."""
        with self._lock:
            cursor = self._conn.execute(
                "INSERT OR IGNORE INTO checkpoint_diagnoses VALUES (?, ?, ?)",
                (work_package_id, head_sha, fingerprint),
            )
            self._conn.commit()
            return cursor.rowcount == 1

    def claim_checks_registration(self, work_package_id: int, repo: str, head_sha: str) -> bool:
        """Allow one passive registration grace per provider head, across restarts."""
        with self._lock:
            cursor = self._conn.execute(
                "INSERT OR IGNORE INTO checkpoint_registrations VALUES (?, ?, ?)",
                (work_package_id, repo, head_sha),
            )
            self._conn.commit()
            return cursor.rowcount == 1

    def record_checkpoint_progress(
        self, work_package_id: int, *, head_sha: str | None, milestone: str | None = None,
        waiting: bool = False, published_head: str | None = None,
    ) -> tuple[int, int]:
        """Return unchanged-source checkpoints and revisions since a new milestone.

        A SHA is activity, not delivery. Only a previously unseen, declared
        completed milestone restarts the convergence budget. Independent review
        still verifies that declaration; this is scheduling, never acceptance.
        """
        with self._lock:
            row = self._conn.execute(
                "SELECT head_sha, stagnant, revisions, published_head FROM checkpoint_progress "
                "WHERE work_package_id = ?", (work_package_id,),
            ).fetchone()
            # Single-head callers supply one published source; a dispatcher with no
            # provider identity supplies neither. Local-only movement is activity,
            # never a fresh source for a milestone against an unchanged publication.
            published = published_head or head_sha
            previous = row["published_head"] if row else None
            fresh_publication = published is not None and published != previous
            moved = bool(head_sha) and (row is None or row["head_sha"] != head_sha)
            stagnant = 0 if moved else (int(row["stagnant"]) if row else 0) + int(not waiting)
            revisions = (int(row["revisions"]) if row else 0) + int(moved)
            if (fresh_publication and milestone
                    and milestone.lower() not in {"none", "omit unless newly completed"}):
                cursor = self._conn.execute(
                    "INSERT OR IGNORE INTO checkpoint_milestones VALUES (?, ?)",
                    (work_package_id, milestone),
                )
                if cursor.rowcount:
                    revisions = 0
            self._conn.execute(
                "INSERT OR REPLACE INTO checkpoint_progress "
                "(work_package_id, head_sha, stagnant, revisions, published_head) "
                "VALUES (?, ?, ?, ?, ?)",
                (work_package_id, head_sha, stagnant, revisions, published or previous),
            )
            self._conn.commit()
        return stagnant, revisions

    # -- resume holds ----------------------------------------------------

    def set_resume_hold(
        self,
        work_package_id: int,
        *,
        repo: str,
        ready_at: str,
        reason: str,
        head_sha: str | None = None,
        attempts: int = 0,
        diagnosis_allowed: bool = False,
        pr_branch: str | None = None,
    ) -> ResumeHold:
        """Create or replace the resume hold of one work package."""
        now = _utcnow()
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO resume_holds (
                  work_package_id, repo, head_sha, ready_at, reason,
                  created_at, updated_at, attempts, diagnosis_allowed, pr_branch
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(work_package_id) DO UPDATE SET
                  repo = excluded.repo,
                  head_sha = excluded.head_sha,
                  ready_at = excluded.ready_at,
                  reason = excluded.reason,
                  created_at = CASE WHEN resume_holds.head_sha IS excluded.head_sha
                    THEN resume_holds.created_at ELSE excluded.created_at END,
                  updated_at = excluded.updated_at,
                  attempts = excluded.attempts,
                  diagnosis_allowed = excluded.diagnosis_allowed,
                  pr_branch = excluded.pr_branch
                """,
                (
                    work_package_id,
                    repo,
                    head_sha,
                    ready_at,
                    reason,
                    now,
                    now,
                    attempts,
                    int(diagnosis_allowed),
                    pr_branch,
                ),
            )
            self._conn.commit()
        hold = self.resume_hold(work_package_id)
        assert hold is not None
        return hold

    def resume_hold(self, work_package_id: int) -> ResumeHold | None:
        """The hold of one work package, or None."""
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM resume_holds WHERE work_package_id = ?",
                (work_package_id,),
            ).fetchone()
        return None if row is None else _row_to_resume_hold(row)

    def extend_resume_hold(
        self, work_package_id: int, *, ready_at: str, reason: str | None = None
    ) -> None:
        """Push a hold's deadline back and count the attempt."""
        assignments = "ready_at = ?, updated_at = ?, attempts = attempts + 1"
        values: list[object] = [ready_at, _utcnow()]
        if reason is not None:
            assignments += ", reason = ?"
            values.append(reason)
        values.append(work_package_id)
        with self._lock:
            self._conn.execute(
                f"UPDATE resume_holds SET {assignments} WHERE work_package_id = ?",
                (*values,),
            )
            self._conn.commit()

    def clear_resume_hold(self, work_package_id: int) -> bool:
        """Drop a hold; return whether one existed."""
        with self._lock:
            cursor = self._conn.execute(
                "DELETE FROM resume_holds WHERE work_package_id = ?",
                (work_package_id,),
            )
            self._conn.commit()
        return cursor.rowcount > 0

    def resume_holds(self) -> list[ResumeHold]:
        """Every resume hold, oldest deadline first."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM resume_holds ORDER BY ready_at, work_package_id"
            ).fetchall()
        return [_row_to_resume_hold(row) for row in rows]

    def prune_resume_holds(
        self, *, older_than_days: int, now: datetime | None = None
    ) -> int:
        """Drop holds that were never released or extended for a long time.

        A hold is normally deleted as soon as the run resumes or the ticket
        leaves In Progress. This sweep only catches leftovers (a ticket a
        human moved to On Hold while it was held); it uses the same retention
        window as the run directories.
        """
        if older_than_days < 0:
            raise ValueError("older_than_days must not be negative")
        current = now or datetime.now(UTC)
        if current.tzinfo is None:
            current = current.replace(tzinfo=UTC)
        cutoff = (current - timedelta(days=older_than_days)).isoformat()
        with self._lock:
            cursor = self._conn.execute(
                "DELETE FROM resume_holds WHERE updated_at < ?", (cutoff,)
            )
            self._conn.commit()
        return cursor.rowcount

    def reset_crash_count(self, work_package_id: int) -> int:
        """Delete the crash counter and return the value it had.

        The counter only ever incremented, so an escalation consumed the whole
        retry budget permanently: WP 144 needed a hand-written ``DELETE`` to be
        requeued. A human moving a ticket out of On Hold is the signal that
        the cause was addressed, so the budget is restored then.
        """
        # Read inside the same lock: ``crash_count`` takes it too, and it is
        # not reentrant.
        with self._lock:
            row = self._conn.execute(
                "SELECT count FROM crash_counts WHERE work_package_id = ?",
                (work_package_id,),
            ).fetchone()
            if row is None:
                return 0
            self._conn.execute(
                "DELETE FROM crash_counts WHERE work_package_id = ?",
                (work_package_id,),
            )
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
