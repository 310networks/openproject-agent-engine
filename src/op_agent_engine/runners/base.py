"""Process execution for agent runs.

``run_command`` starts one process in its own session (process group),
streams stdout/stderr into the per-run directory, enforces the run timeout
(SIGTERM to the whole group, then SIGKILL after a grace period), and
extracts the agent's final ``RESULT:`` line from the tail of stdout.

Killing the process group is what makes the timeout reliable: the agent
spawns its own children (shells, compilers, git), and all of them must die
with the run.
"""

from __future__ import annotations

import asyncio
import collections
import contextlib
import os
import signal
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import IO

from op_agent_engine.logging import get_logger
from op_agent_engine.recovery import process_start_identity
from op_agent_engine.runners.events import LAST_MESSAGE_FILE, AssistantStream

logger = get_logger(__name__)

#: Lines of stdout kept in memory for RESULT extraction (the line is printed
#: at the very end of the agent's output).
TAIL_LINES = 500

#: Seconds to wait for the process group after SIGTERM before SIGKILL.
KILL_GRACE_SECONDS = 20.0

#: Seconds to wait for the log pumps after the process exited (children may
#: keep pipe ends open briefly).
PUMP_GRACE_SECONDS = 5.0

RESULT_PREFIX = "RESULT:"

OnSpawned = Callable[[int, int, str], None]


class RunnerError(Exception):
    """The runner process could not be started."""


@dataclass(frozen=True)
class RunRequest:
    """Everything needed to start one agent process."""

    argv: tuple[str, ...]
    cwd: Path
    env: dict[str, str]
    stdin_file: Path | None
    timeout_seconds: float
    run_dir: Path
    run_id: int
    work_package_id: int
    agent: str
    #: Parse stdout as the agent's JSONL event stream (``--mode json``), so the
    #: last assistant message can be shown while the run is still active.
    json_events: bool = False


@dataclass(frozen=True)
class RunResult:
    """The observed outcome of one agent process."""

    exit_code: int
    timed_out: bool
    duration_seconds: float
    stdout_path: Path
    stderr_path: Path
    result_line: str | None

    def is_silent(self) -> bool:
        """True when the process wrote nothing at all to stdout and stderr.

        The agent CLI is a known offender: pi's print mode only writes the
        final assistant message and exits 0 when the last message carries no
        text, so a whole run can leave two empty logs (runs 258 and 304). That
        is not the same failure as "ran, found no PR".
        """
        return self._size(self.stdout_path) == 0 and self._size(self.stderr_path) == 0

    @staticmethod
    def _size(path: Path) -> int:
        try:
            return path.stat().st_size
        except OSError:
            return 0


def extract_result_line(tail: Sequence[str]) -> str | None:
    """Return the last line of *tail* that starts with ``RESULT:`` (stripped)."""
    for line in reversed(tail):
        stripped = line.strip()
        if stripped.startswith(RESULT_PREFIX):
            return stripped
    return None


async def _pump(
    reader: asyncio.StreamReader,
    path: Path,
    tail: collections.deque[str],
    on_line: Callable[[bytes], object] | None = None,
) -> None:
    """Stream a pipe into *path*, feeding complete lines into *tail*.

    ``on_line`` sees every complete line (raw bytes, before decoding) so the
    JSONL parser can follow the stream while it is written to disk.
    """
    buffer = b""
    with path.open("wb") as handle:
        while True:
            chunk = await reader.read(65536)
            if not chunk:
                break
            handle.write(chunk)
            buffer += chunk
            while b"\n" in buffer:
                line, buffer = buffer.split(b"\n", 1)
                tail.append(line.decode("utf-8", errors="replace"))
                if on_line is not None:
                    on_line(line)
    if buffer:
        tail.append(buffer.decode("utf-8", errors="replace"))
        if on_line is not None:
            on_line(buffer)


def _kill_process_group(pgid: int, signum: int) -> None:
    with contextlib.suppress(ProcessLookupError):
        os.killpg(pgid, signum)


async def run_command(
    request: RunRequest,
    *,
    on_spawned: OnSpawned | None = None,
) -> RunResult:
    """Run one agent process and return the observed :class:`RunResult`.

    ``on_spawned(pid, pgid, proc_start)`` is called right after a successful
    start so the caller can record the process in the store.
    """
    request.run_dir.mkdir(parents=True, exist_ok=True)
    stdout_path = request.run_dir / "stdout.log"
    stderr_path = request.run_dir / "stderr.log"
    tail: collections.deque[str] = collections.deque(maxlen=TAIL_LINES)

    # In JSON mode the stdout lines are events, not a transcript: parse them as
    # they arrive so the dashboard can show what the agent is saying, and so a
    # run that ends without printing anything still leaves its last message.
    events: AssistantStream | None = None
    if request.json_events:
        events = AssistantStream(path=stdout_path.with_name(LAST_MESSAGE_FILE))

    stdin: int | IO[bytes] | None = asyncio.subprocess.DEVNULL
    stdin_file: IO[bytes] | None = None
    if request.stdin_file is not None:
        stdin_file = request.stdin_file.open("rb")
        stdin = stdin_file

    try:
        process = await asyncio.create_subprocess_exec(
            *request.argv,
            cwd=str(request.cwd),
            env=request.env,
            stdin=stdin,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
    except (OSError, ValueError) as error:
        raise RunnerError(
            f"failed to start runner ({request.argv[0]}): {error}"
        ) from error
    finally:
        if stdin_file is not None:
            stdin_file.close()

    started_at = time.monotonic()
    if on_spawned is not None:
        # The process start identity (boot id + starttime ticks) makes later
        # cleanup PID-reuse safe. Fall back to a timestamp
        # on platforms without /proc; recovery then refuses to kill the PID.
        identity = process_start_identity(process.pid) or datetime.now(UTC).isoformat()
        on_spawned(process.pid, process.pid, identity)

    assert process.stdout is not None
    assert process.stderr is not None
    on_stdout_line = None if events is None else events.feed_line
    stdout_task = asyncio.create_task(
        _pump(process.stdout, stdout_path, tail, on_stdout_line)
    )
    stderr_task = asyncio.create_task(_pump(process.stderr, stderr_path, tail))

    timed_out = False
    try:
        try:
            await asyncio.wait_for(process.wait(), timeout=request.timeout_seconds)
        except TimeoutError:
            timed_out = True
            logger.warning(
                "run timed out after %.0fs; sending SIGTERM to process group %d",
                request.timeout_seconds,
                process.pid,
                extra={
                    "run_id": request.run_id,
                    "work_package_id": request.work_package_id,
                    "agent": request.agent,
                },
            )
            _kill_process_group(process.pid, signal.SIGTERM)
            try:
                await asyncio.wait_for(process.wait(), timeout=KILL_GRACE_SECONDS)
            except TimeoutError:
                logger.warning(
                    "process group %d did not exit after SIGTERM; sending SIGKILL",
                    process.pid,
                    extra={"run_id": request.run_id},
                )
                _kill_process_group(process.pid, signal.SIGKILL)
                await process.wait()
        except asyncio.CancelledError:
            # Graceful shutdown exceeded or the engine is stopping: terminate
            # the whole group and re-raise so the engine marks the run
            # interrupted.
            logger.warning(
                "run cancelled; sending SIGTERM to process group %d",
                process.pid,
                extra={"run_id": request.run_id},
            )
            _kill_process_group(process.pid, signal.SIGTERM)
            try:
                await asyncio.wait_for(
                    asyncio.shield(process.wait()), timeout=KILL_GRACE_SECONDS
                )
            except TimeoutError:
                logger.warning(
                    "process group %d did not exit after SIGTERM; sending SIGKILL",
                    process.pid,
                    extra={"run_id": request.run_id},
                )
                _kill_process_group(process.pid, signal.SIGKILL)
                with contextlib.suppress(Exception):
                    await asyncio.shield(process.wait())
            raise
    finally:
        # The pumps finish when every pipe writer is gone; guard against a
        # grandchild in another group holding a pipe end.
        await asyncio.wait({stdout_task, stderr_task}, timeout=PUMP_GRACE_SECONDS)
        for task in (stdout_task, stderr_task):
            if not task.done():
                task.cancel()
        for task in (stdout_task, stderr_task):
            with contextlib.suppress(BaseException):
                await task

    exit_code = process.returncode if process.returncode is not None else -signal.SIGKILL

    duration = time.monotonic() - started_at
    # Text mode prints the RESULT line verbatim on stdout. In JSON mode stdout
    # is JSONL, where a line-based search would return the JSON tail of the
    # record ("RESULT: ok\"}]}}"), so only the parser's copy counts.
    result_line = (
        events.result_line if events is not None else extract_result_line(tail)
    )
    result = RunResult(
        exit_code=exit_code,
        timed_out=timed_out,
        duration_seconds=duration,
        stdout_path=stdout_path,
        stderr_path=stderr_path,
        result_line=result_line,
    )
    logger.info(
        "runner finished: exit_code=%d timed_out=%s duration=%.1fs",
        exit_code,
        timed_out,
        duration,
        extra={
            "run_id": request.run_id,
            "work_package_id": request.work_package_id,
            "agent": request.agent,
            "exit_code": exit_code,
            "timed_out": timed_out,
            "duration_seconds": round(duration, 2),
            "result_line": result.result_line,
        },
    )
    return result
