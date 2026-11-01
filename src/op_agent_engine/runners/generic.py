"""The generic command runner: fills placeholders and executes.

A runner is a command template: argv, cwd, env and optional stdin_file with
``{placeholder}`` substitution. The engine does not special-case pi's CLI. The
only contract is the final ``RESULT:`` line on stdout. The one provider-specific
step is a per-run ``models.json`` for llama.cpp, because ``pi -p`` does not
discover that router's model catalog itself.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Callable, Mapping
from pathlib import Path

from op_agent_engine.config import (
    ALLOWED_RUNNER_PLACEHOLDERS,
    AgentConfig,
    ConfigError,
    RunnerConfig,
    WorkflowConfig,
)
from op_agent_engine.providers import (
    EXTRA_AGENT_ENV_VARS,
    PROVIDER_ENV_VARS,
    provider_of,
)
from op_agent_engine.runners.base import (
    RunRequest,
    RunResult,
    run_command,
)

#: Inherited environment: the minimum pi needs to behave like a manual run
#: (section 6). Everything else is passed explicitly.
_BASE_ENV_VARS = ("PATH", "HOME", "USER", "LOGNAME", "TZ", "LANG", "LC_ALL")

#: Provider credentials and endpoints pi reads directly. The deployment
#: supplies them as environment variables; no auth.json or settings.json is
#: mounted. The provider table is shared with check-config and
#: with the setup script, so a provider can never be asked for a key the runner
#: then refuses to forward. GH_TOKEN/GITHUB_TOKEN let the agents push and open
#: pull requests with gh.
_AGENT_ENV_VARS = tuple(
    dict.fromkeys(
        [
            name
            for variables in PROVIDER_ENV_VARS.values()
            for name in variables
        ]
        + list(EXTRA_AGENT_ENV_VARS)
    )
)

_INHERITED_ENV_VARS = _BASE_ENV_VARS + _AGENT_ENV_VARS

#: An argv element that is nothing but one placeholder, e.g. ``{model}``.
_WHOLE_PLACEHOLDER = re.compile(r"\{[a-z_]+\}")
#: A stand-alone argv flag such as ``--model`` or ``-m``.
_FLAG = re.compile(r"--?[A-Za-z0-9][A-Za-z0-9_-]*")
#: A flag whose glued-on value rendered empty, e.g. ``--model=``.
_EMPTY_FLAG_VALUE = re.compile(r"--?[A-Za-z0-9][A-Za-z0-9_-]*=")
#: Placeholders that may legitimately render empty; a missing value is omitted
#: instead of being passed as an empty argument.
_OPTIONAL_PLACEHOLDERS = frozenset({"model"})


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
            "model": self._config.model_for(agent_key) or "",
            "branch": branch,
            "work_package_id": str(work_package_id),
        }
        argv = self._build_argv(runner, values)
        cwd = Path(
            self._fill(runner.cwd, values, where=f"runners.{agent.runner}.cwd")
        )
        env = self._build_env(agent_key, agent, runner, values)
        self._configure_llama_cpp(agent_key, run_dir, env)
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

    def _build_argv(
        self, runner: RunnerConfig, values: Mapping[str, str]
    ) -> tuple[str, ...]:
        """Fill the argv template, omitting optional values that are unset.

        ``--model {model}`` with no model configured must not reach the agent
        CLI: the placeholder element and the flag in front of it are dropped, so
        the run is identical to an argv without ``{model}`` at all.
        """
        argv: list[str] = []
        for part in runner.argv:
            filled = self._fill(part, values, where="runners.argv")
            if not filled and _is_optional_placeholder(part):
                if argv and _FLAG.fullmatch(argv[-1]):
                    argv.pop()
                continue
            if _mentions_optional(part) and _EMPTY_FLAG_VALUE.fullmatch(filled):
                continue
            argv.append(filled)
        return tuple(argv)

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

    def _configure_llama_cpp(
        self, agent_key: str, run_dir: Path, env: dict[str, str]
    ) -> None:
        """Give pi a models.json for the configured llama.cpp model.

        ``pi -p`` (the engine's only mode) does not refresh the built-in
        llama.cpp router catalog over the network. Without a model entry Pi
        rejects ``llama.cpp/<id>`` before the request is made, even though
        ``LLAMA_BASE_URL`` is set. A per-run agent directory with a generated
        ``models.json`` supplies the configured model synchronously; the API
        key stays an environment reference, never a literal in the file.
        """
        model = self._config.model_for(agent_key)
        if provider_of(model) != "llama.cpp":
            return
        assert model is not None
        base_url = env.get("LLAMA_BASE_URL", "").strip()
        if not base_url:
            raise ConfigError(
                "LLAMA_BASE_URL is required for the llama.cpp provider"
            )
        model_id = model.split("/", 1)[1].strip()
        api_key = (
            "$LLAMA_API_KEY"
            if env.get("LLAMA_API_KEY", "").strip()
            else "local"
        )
        agent_dir = run_dir / "pi-agent"
        agent_dir.mkdir(parents=True, exist_ok=True)
        config = {
            "providers": {
                "llama.cpp": {
                    "baseUrl": _llama_inference_url(base_url),
                    "api": "openai-completions",
                    "apiKey": api_key,
                    "models": [{"id": model_id}],
                }
            }
        }
        (agent_dir / "models.json").write_text(
            json.dumps(config, indent=2) + "\n"
        )
        env["PI_CODING_AGENT_DIR"] = str(agent_dir)

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


def _llama_inference_url(base_url: str) -> str:
    """Normalize a llama.cpp server URL to the OpenAI-compatible API root."""
    url = base_url.strip().rstrip("/")
    if url.endswith("/v1"):
        return url
    return f"{url}/v1"


def _is_optional_placeholder(part: str) -> bool:
    match = _WHOLE_PLACEHOLDER.fullmatch(part)
    return match is not None and match.group(0)[1:-1] in _OPTIONAL_PLACEHOLDERS


def _mentions_optional(part: str) -> bool:
    return any("{" + name + "}" in part for name in _OPTIONAL_PLACEHOLDERS)
