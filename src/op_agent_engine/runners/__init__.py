"""Runner abstraction (PLAN section 6).

The engine knows nothing about the agent CLI itself: a runner is a command
template (argv/cwd/env/stdin_file) whose placeholders are filled per run.
The only contract is that the agent prints a final ``RESULT:`` line on
stdout (PLAN section 10), which the engine extracts from the log tail.
"""

from op_agent_engine.runners.base import (
    RunnerError,
    RunRequest,
    RunResult,
    extract_result_line,
    run_command,
)
from op_agent_engine.runners.generic import GenericRunner

__all__ = [
    "GenericRunner",
    "RunRequest",
    "RunResult",
    "RunnerError",
    "extract_result_line",
    "run_command",
]
