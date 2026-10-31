"""Crash recovery helpers.

On startup every ``running`` row is marked ``interrupted``. Before that, the
process group recorded for each such run is terminated so a hard engine crash
does not leave agents running. Killing is guarded by the process start
identity recorded at launch: if the PID now belongs to a different process
(PID reuse after a reboot or a long downtime) it is left alone.

The ``interrupted`` state never increments the crash counter; only a run that
actually failed or timed out does.
"""

from __future__ import annotations

import contextlib
import os
import signal
import time
from dataclasses import dataclass
from pathlib import Path

from op_agent_engine.logging import get_logger
from op_agent_engine.store import Store

logger = get_logger(__name__)

#: Linux process start time (field 22 of /proc/<pid>/stat) combined with the
#: boot id identifies a process across PID reuse.
_PROC = Path("/proc")
_BOOT_ID = Path("/proc/sys/kernel/random/boot_id")

#: Grace period between SIGTERM and SIGKILL when cleaning up a leftover group.
TERM_GRACE_SECONDS = 5.0
_POLL_INTERVAL_SECONDS = 0.05


def process_start_identity(pid: int) -> str | None:
    """A stable identity for *pid* at launch, or None when it is gone.

    The value is ``<boot-id>:<starttime-ticks>`` on Linux. On a platform
    without ``/proc`` (or when the process already exited) it returns None,
    which makes the caller skip the kill because the start time cannot be
    verified.
    """
    try:
        stat = (_PROC / str(pid) / "stat").read_text(
            encoding="utf-8", errors="replace"
        )
    except OSError:
        return None
    try:
        # The comm field is parenthesised and may contain spaces, so split
        # after the last ')' and index from field 3 (state). Overall field 22
        # (starttime) is therefore index 19.
        fields = stat.rsplit(")", 1)[1].split()
        start_ticks = fields[19]
    except IndexError:
        return None

    boot_id = ""
    with contextlib.suppress(OSError):
        boot_id = _BOOT_ID.read_text(encoding="utf-8").strip()
    return f"{boot_id}:{start_ticks}" if boot_id else start_ticks


def _process_group_exists(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def terminate_process_group(
    pgid: int, *, grace_seconds: float = TERM_GRACE_SECONDS
) -> None:
    """Terminate a whole process group (SIGTERM, then SIGKILL after grace)."""
    with contextlib.suppress(ProcessLookupError):
        os.killpg(pgid, signal.SIGTERM)
    deadline = time.monotonic() + grace_seconds
    while time.monotonic() < deadline:
        if not _process_group_exists(pgid):
            return
        time.sleep(_POLL_INTERVAL_SECONDS)
    with contextlib.suppress(ProcessLookupError):
        os.killpg(pgid, signal.SIGKILL)


@dataclass(frozen=True)
class RecoveryReport:
    """What :func:`recover_interrupted_runs` did at startup."""

    interrupted: int
    killed_process_groups: tuple[int, ...]
    skipped_reused_pids: tuple[int, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "interrupted": self.interrupted,
            "killed_process_groups": list(self.killed_process_groups),
            "skipped_reused_pids": list(self.skipped_reused_pids),
        }


def recover_interrupted_runs(store: Store, *, kill: bool = True) -> RecoveryReport:
    """Clean up leftover agent processes and mark every active run interrupted.

    A recorded process group is killed only when the live process at ``pid``
    still has the start identity recorded at launch. Rows are always marked
    ``interrupted`` afterwards (that is the normal recovery path); the crash
    counter is untouched.
    """
    running = store.active_runs()
    killed: list[int] = []
    skipped: list[int] = []
    for run in running:
        if not kill or run.pid is None or run.pgid is None or not run.proc_start:
            continue
        current = process_start_identity(run.pid)
        if current is None:
            # The process already exited; there is nothing to kill.
            continue
        if current != run.proc_start:
            skipped.append(run.pid)
            logger.warning(
                "not killing process group %d for run %d: PID %d was reused",
                run.pgid,
                run.id,
                run.pid,
                extra={"run_id": run.id, "work_package_id": run.work_package_id},
            )
            continue
        logger.warning(
            "terminating leftover process group %d from run %d",
            run.pgid,
            run.id,
            extra={"run_id": run.id, "work_package_id": run.work_package_id},
        )
        terminate_process_group(run.pgid)
        killed.append(run.pgid)

    interrupted = store.mark_all_running_interrupted()
    return RecoveryReport(
        interrupted=interrupted,
        killed_process_groups=tuple(killed),
        skipped_reused_pids=tuple(skipped),
    )
