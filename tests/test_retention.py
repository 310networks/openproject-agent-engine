"""Tests for the run-directory retention job."""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from op_agent_engine.retention import cleanup_run_dirs

NOW = datetime(2026, 10, 29, 12, 0, tzinfo=UTC)


def make_run_dir(runs_dir: Path, name: str, age_days: float) -> Path:
    directory = runs_dir / name
    directory.mkdir(parents=True)
    timestamp = (NOW - timedelta(days=age_days)).timestamp()
    for name in ("prompt.txt", "stdout.log"):
        path = directory / name
        path.write_text(name.split(".")[0], encoding="utf-8")
        os.utime(path, (timestamp, timestamp))
    os.utime(directory, (timestamp, timestamp))
    return directory


def test_removes_only_old_numeric_run_directories(tmp_path: Path) -> None:
    runs_dir = tmp_path / "runs"
    old = make_run_dir(runs_dir, "1", age_days=31)
    recent = make_run_dir(runs_dir, "2", age_days=29)
    other = runs_dir / "notes"
    other.mkdir()
    (runs_dir / "README.txt").write_text("keep", encoding="utf-8")

    report = cleanup_run_dirs(runs_dir, older_than_days=30, now=NOW)

    assert not old.exists()
    assert recent.is_dir()
    assert other.is_dir()
    assert (runs_dir / "README.txt").is_file()
    assert report.removed_count == 1
    assert report.removed[0].name == "1"
    assert report.kept == 1
    assert report.errors == ()
    assert report.bytes_freed > 0


def test_dry_run_reports_without_deleting(tmp_path: Path) -> None:
    runs_dir = tmp_path / "runs"
    old = make_run_dir(runs_dir, "1", age_days=90)

    report = cleanup_run_dirs(
        runs_dir, older_than_days=30, dry_run=True, now=NOW
    )

    assert old.is_dir()
    assert report.dry_run is True
    assert report.removed_count == 1
    assert "would remove" in "\n".join(report.summary())


def test_zero_days_selects_every_run_directory(tmp_path: Path) -> None:
    runs_dir = tmp_path / "runs"
    make_run_dir(runs_dir, "1", age_days=0.01)
    make_run_dir(runs_dir, "2", age_days=5)

    report = cleanup_run_dirs(runs_dir, older_than_days=0, now=NOW)

    assert report.removed_count == 2
    assert list(runs_dir.iterdir()) == []


def test_directory_at_the_cutoff_is_kept(tmp_path: Path) -> None:
    runs_dir = tmp_path / "runs"
    boundary = make_run_dir(runs_dir, "1", age_days=30)

    report = cleanup_run_dirs(runs_dir, older_than_days=30, now=NOW)

    assert boundary.is_dir()
    assert report.removed_count == 0
    assert report.kept == 1


def test_missing_runs_directory_is_not_an_error(tmp_path: Path) -> None:
    report = cleanup_run_dirs(tmp_path / "missing", now=NOW)

    assert report.removed_count == 0
    assert report.kept == 0
    assert report.errors == ()


def test_negative_retention_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="must not be negative"):
        cleanup_run_dirs(tmp_path, older_than_days=-1, now=NOW)


def test_report_json_is_stable(tmp_path: Path) -> None:
    runs_dir = tmp_path / "runs"
    make_run_dir(runs_dir, "7", age_days=40)

    payload = cleanup_run_dirs(runs_dir, older_than_days=30, now=NOW).as_dict()

    assert payload["runs_dir"] == str(runs_dir)
    assert payload["older_than_days"] == 30
    assert payload["dry_run"] is False
    assert payload["removed_count"] == 1
    assert payload["removed"] == [
        {"name": "7", "age_hours": pytest.approx(960.0, abs=1.0), "size_bytes": 12}
    ]


def test_recent_activity_keeps_an_old_run_directory(tmp_path: Path) -> None:
    runs_dir = tmp_path / "runs"
    directory = make_run_dir(runs_dir, "1", age_days=90)
    # The directory entry is old, but the runner appends to stdout today.
    fresh = (NOW - timedelta(hours=1)).timestamp()
    os.utime(directory / "stdout.log", (fresh, fresh))

    report = cleanup_run_dirs(runs_dir, older_than_days=30, now=NOW)

    assert directory.is_dir()
    assert report.removed_count == 0
    assert report.kept == 1
