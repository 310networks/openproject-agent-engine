"""The generic command runner: fills placeholders and executes.

A runner is a pure command template (PLAN section 6): argv, cwd, env and
optional stdin_file with ``{placeholder}`` substitution. The engine knows
nothing about the agent CLI itself. The only contract is the final
``RESULT:`` line on stdout (PLAN section 10).
"""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from pathlib import Path

from op_agent_engine.config import (
    ALLOWED_RUNNER_PLACEHOLDERS,
    AgentConfig,
    ConfigError,
    RunnerConfig,
    WorkflowConfig,
)
from op_agent_engine.runners.base import (
    RunRequest,
    RunResult,
    run_command,
)

#: Inherited environment: the minimum an agent CLI needs to behave like a
#: manual run (section 6). Everything else is passed explicitly.
_INHERITED_ENV_VARS = ("PATH", "HOME", "USER", "LOGNAME", "TZ", "LANG", "LC_ALL")


class GenericRunner:
    """Builds :class:`RunRequest` objects from the workflow config and runs them."""

    def __init__(self, config: WorkflowConfig) -> None:
        self._config = config

    def build_request(
        self,
        agent_key: str,
        *,
        run_id: int,
        work_package_id: int,
        run_dir: Path,
        repo_dir: Path,
        workspace_dir: Path,
        branch: str,
        prompt_file: Path,
        timeout_seconds: float,
    ) -> RunRequest:
        """Fill the agent's runner template for one concrete run."""
        agent = self._config.agents[agent_key]
        runner = self._config.runners[agent.runner]
        values = {
            "prompt_file": str(prompt_file),
            "repo_dir": str(repo_dir),
            "workspace_dir": str(workspace_dir),
            "run_dir": str(run_dir),
            "model": agent.model or "",
            "branch": branch,
            "work_package_id": str(work_package_id),
        }
        argv = tuple(
            self._fill(part, values, where=f"runners.{agent.runner}.argv")
            for part in runner.argv
        )
        cwd = Path(
            self._fill(runner.cwd, values, where=f"runners.{agent.runner}.cwd")
        )
        env = self._build_env(agent_key, agent, runner, values)
        stdin_file: Path | None = None
        if runner.stdin_file is not None:
            stdin_file = Path(
                self._fill(
                    runner.stdin_file,
                    values,
                    where=f"runners.{agent.runner}.stdin_file",
                )
            )
        return RunRequest(
            argv=argv,
            cwd=cwd,
            env=env,
            stdin_file=stdin_file,
            timeout_seconds=timeout_seconds,
            run_dir=run_dir,
            run_id=run_id,
            work_package_id=work_package_id,
            agent=agent_key,
        )

    async def run(
        self,
        request: RunRequest,
        *,
        on_spawned: Callable[[int, int, str], None] | None = None,
    ) -> RunResult:
        """Start the process and wait for it (timeout included)."""
        return await run_command(request, on_spawned=on_spawned)

    # -- helpers -----------------------------------------------------------

    def _build_env(
        self,
        agent_key: str,
        agent: AgentConfig,
        runner: RunnerConfig,
        values: Mapping[str, str],
    ) -> dict[str, str]:
        """Inherited env, then runner env, then per-agent env (overrides)."""
        env = {
            name: os.environ[name]
            for name in _INHERITED_ENV_VARS
            if name in os.environ
        }
        for name, value in runner.env.items():
            env[name] = self._fill(
                value, values, where=f"runners.{agent.runner}.env.{name}"
            )
        for name, value in agent.env.items():
            env[name] = self._fill(
                value, values, where=f"agents.{agent_key}.env.{name}"
            )
        return env

    @staticmethod
    def _fill(template: str, values: Mapping[str, str], *, where: str) -> str:
        try:
            return template.format_map(values)
        except KeyError as error:
            name = error.args[0]
            allowed = ", ".join(sorted(ALLOWED_RUNNER_PLACEHOLDERS))
            raise ConfigError(
                f"{where}: unknown placeholder '{name}' (allowed: {allowed})"
            ) from error
