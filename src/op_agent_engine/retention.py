"""Run-directory retention (PLAN section 16).

Each run writes its artifacts to ``DATA_DIR/runs/<runId>/`` (``prompt.txt``,
``stdout.log``, ``stderr.log`` and ``meta.json``). The retention job removes
those directories once they are older than ``RUN_RETENTION_DAYS`` (default
30). The SQLite run history is deliberately left untouched: deleting the
database loses history but the artifacts are the bulky part, and keeping the
rows preserves ``status`` output and crash counters.

Safety rules:

* Only direct children of ``DATA_DIR/runs`` whose name is numeric (a run id)
  are considered. Anything else in that directory is never touched.
* A directory's age is the newest ``mtime`` among the directory and its
  files, so an active run whose logs are still being written is not pruned
  merely because the directory entry is old.
* Removal is best-effort: a failure is reported and the job continues, so one
  permission problem cannot block the rest of the cleanup.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

DEFAULT_RETENTION_DAYS = 30


@dataclass(frozen=True)
class RemovedRunDir:
    """One run directory selected (and unless dry-run, removed) by cleanup."""

    name: str
    path: Path
    age_seconds: float
    size_bytes: int


@dataclass(frozen=True)
class CleanupReport:
    """Result of one retention pass."""

    runs_dir: Path
    older_than_days: int
    dry_run: bool
    removed: tuple[RemovedRunDir, ...] = ()
    kept: int = 0
    errors: tuple[str, ...] = ()

    @property
    def removed_count(self) -> int:
        return len(self.removed)

    @property
    def bytes_freed(self) -> int:
        return sum(item.size_bytes for item in self.removed)

    def as_dict(self) -> dict[str, object]:
        return {
            "runs_dir": str(self.runs_dir),
            "older_than_days": self.older_than_days,
            "dry_run": self.dry_run,
            "removed_count": self.removed_count,
            "kept": self.kept,
            "bytes_freed": self.bytes_freed,
            "removed": [
                {
                    "name": item.name,
                    "age_hours": round(item.age_seconds / 3600, 1),
                    "size_bytes": item.size_bytes,
                }
                for item in self.removed
            ],
            "errors": list(self.errors),
        }

    def summary(self) -> list[str]:
        verb = "would remove" if self.dry_run else "removed"
        lines = [
            f"runs directory: {self.runs_dir}",
            f"older than: {self.older_than_days} day(s)",
            f"{verb}: {self.removed_count} run director(ies), "
            f"{self.bytes_freed} bytes",
            f"kept: {self.kept}",
        ]
        for item in self.removed:
            lines.append(
                f"  - {item.name}: {item.size_bytes} bytes, "
                f"{item.age_seconds / 3600:.1f}h old"
            )
        for error in self.errors:
            lines.append(f"  ! {error}")
        return lines


def _directory_usage(path: Path) -> tuple[int, float]:
    """Return ``(bytes, newest_mtime)`` for a run directory.

    The newest mtime (including the directory itself) keeps a long-running
    run alive as long as the runner is still appending to its logs.
    """
    total = 0
    newest = path.stat().st_mtime
    for child in path.rglob("*"):
        try:
            stat = child.stat()
        except OSError:
            continue
        newest = max(newest, stat.st_mtime)
        if child.is_file() and not child.is_symlink():
            total += stat.st_size
    return total, newest


def cleanup_run_dirs(
    runs_dir: Path,
    *,
    older_than_days: int = DEFAULT_RETENTION_DAYS,
    dry_run: bool = False,
    now: datetime | None = None,
) -> CleanupReport:
    """Remove numeric run directories older than ``older_than_days``.

    ``older_than_days=0`` selects every existing run directory (the cutoff is
    the current time), which is useful for tests and for a deliberate purge.
    A missing ``runs_dir`` is not an error: nothing has run yet.
    """
    if older_than_days < 0:
        raise ValueError("older_than_days must not be negative")

    current = now or datetime.now(UTC)
    if current.tzinfo is None:
        current = current.replace(tzinfo=UTC)
    cutoff = current - timedelta(days=older_than_days)

    removed: list[RemovedRunDir] = []
    errors: list[str] = []
    kept = 0

    try:
        entries = sorted(runs_dir.iterdir(), key=lambda item: item.name)
    except FileNotFoundError:
        entries = []
    except OSError as error:
        return CleanupReport(
            runs_dir=runs_dir,
            older_than_days=older_than_days,
            dry_run=dry_run,
            errors=(f"cannot read {runs_dir}: {error}",),
        )

    for entry in entries:
        if not entry.name.isdigit():
            continue
        try:
            if not entry.is_dir() or entry.is_symlink():
                continue
            entry.stat()
        except OSError as error:
            errors.append(f"cannot stat {entry.name}: {error}")
            continue

        try:
            size_bytes, newest_mtime = _directory_usage(entry)
        except OSError as error:
            errors.append(f"cannot stat {entry.name}: {error}")
            continue

        modified = datetime.fromtimestamp(newest_mtime, tz=UTC)
        if modified >= cutoff:
            kept += 1
            continue

        age_seconds = max(0.0, (current - modified).total_seconds())
        if not dry_run:
            try:
                shutil.rmtree(entry)
            except OSError as error:
                errors.append(f"cannot remove {entry.name}: {error}")
                continue
        removed.append(
            RemovedRunDir(
                name=entry.name,
                path=entry,
                age_seconds=age_seconds,
                size_bytes=size_bytes,
            )
        )

    return CleanupReport(
        runs_dir=runs_dir,
        older_than_days=older_than_days,
        dry_run=dry_run,
        removed=tuple(removed),
        kept=kept,
        errors=tuple(errors),
    )
