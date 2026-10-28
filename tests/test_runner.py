"""Tests for the runner abstraction (PLAN section 6)."""

import dataclasses
from pathlib import Path

import pytest

from op_agent_engine.config import ConfigError, WorkflowConfig
from op_agent_engine.runners import (
    GenericRunner,
    RunnerError,
    RunRequest,
    RunResult,
    extract_result_line,
    run_command,
)


def base_mapping() -> dict[str, object]:
    return {
        "projects": [
            {
                "identifier": "acme",
                "repo": {
                    "github": "acme/thing",
                    "dir": "thing",
                    "default_branch": "main",
                },
                "enabled": True,
            }
        ],
        "agents": {
            "FE": {
                "name": "frontendEngineer",
                "kind": "implementer",
                "op_login": "fe-bot",
                "workspace": "fe",
                "runner": "generic",
                "model": "some/model",
                "env": {
                    "EXTRA_VAR": "from-agent",
                    "PROMPT_REF": "{prompt_file}",
                },
            },
            "REVIEW": {
                "name": "codeReviewer",
                "kind": "reviewer",
                "op_login": "review-bot",
                "workspace": "review",
                "runner": "generic",
            },
        },
        "runners": {
            "generic": {
                "argv": [
                    "run-agent",
                    "--prompt",
                    "{prompt_file}",
                    "--branch",
                    "{branch}",
                ],
                "cwd": "{repo_dir}",
                "env": {
                    "RUNNER_VAR": "from-runner",
                    "SHARED_VAR": "runner-value",
                },
                "stdin_file": "{prompt_file}",
                "inject_standard": True,
            }
        },
    }


def _config() -> WorkflowConfig:
    return WorkflowConfig.from_mapping(base_mapping(), environ={})


def test_build_request_fills_placeholders(tmp_path: Path) -> None:
    runner = GenericRunner(_config())
    request = runner.build_request(
        "FE",
        run_id=11,
        work_package_id=100,
        run_dir=tmp_path / "runs" / "11",
        repo_dir=tmp_path / "ws" / "fe" / "repo",
        workspace_dir=tmp_path / "ws" / "fe",
        branch="op-4-100",
        prompt_file=tmp_path / "runs" / "11" / "prompt.txt",
        timeout_seconds=120,
    )
    prompt = str(tmp_path / "runs" / "11" / "prompt.txt")
    assert request.argv == (
        "run-agent",
        "--prompt",
        prompt,
        "--branch",
        "op-4-100",
    )
    assert request.cwd == tmp_path / "ws" / "fe" / "repo"
    assert request.stdin_file == Path(prompt)
    assert request.timeout_seconds == 120
    assert request.run_id == 11
    assert request.work_package_id == 100
    assert request.agent == "FE"


def test_build_request_env_layers(tmp_path: Path) -> None:
    runner = GenericRunner(_config())
    request = runner.build_request(
        "FE",
        run_id=1,
        work_package_id=100,
        run_dir=tmp_path / "run",
        repo_dir=tmp_path / "repo",
        workspace_dir=tmp_path / "ws",
        branch="op-4-100",
        prompt_file=tmp_path / "prompt.txt",
        timeout_seconds=120,
    )
    # Runner env, then agent env overrides.
    assert request.env["RUNNER_VAR"] == "from-runner"
    assert request.env["EXTRA_VAR"] == "from-agent"
    assert request.env["SHARED_VAR"] == "runner-value"
    assert request.env["PROMPT_REF"] == str(tmp_path / "prompt.txt")
    # Inherited minimum.
    assert request.env.get("PATH")
    assert "HOME" in request.env


def test_build_request_unknown_placeholder_raises(tmp_path: Path) -> None:
    mapping = base_mapping()
    runners = mapping["runners"]
    assert isinstance(runners, dict)
    generic = runners["generic"]
    assert isinstance(generic, dict)
    generic["argv"] = ["x", "{nope}"]
    with pytest.raises(ConfigError, match=r"unknown placeholder '\{nope\}'"):
        WorkflowConfig.from_mapping(mapping, environ={})


def test_extract_result_line() -> None:
    assert extract_result_line(["a", "RESULT: BLOCKED x", "RESULT: ok"]) == (
        "RESULT: ok"
    )
    assert extract_result_line(["  RESULT: spaced  "]) == "RESULT: spaced"
    assert extract_result_line(["nothing here"]) is None
    assert extract_result_line([]) is None


def _request(tmp_path: Path, argv: tuple[str, ...]) -> RunRequest:
    return RunRequest(
        argv=argv,
        cwd=tmp_path,
        env={"PATH": "/usr/bin:/bin"},
        stdin_file=None,
        timeout_seconds=30,
        run_dir=tmp_path / "run",
        run_id=1,
        work_package_id=100,
        agent="a",
    )


async def test_run_command_captures_output_and_result(tmp_path: Path) -> None:
    request = _request(
        tmp_path,
        (
            "sh",
            "-c",
            "echo out1; echo 'RESULT: PR opened'; echo err 1>&2; exit 0",
        ),
    )
    result = await run_command(request)
    assert result.exit_code == 0
    assert result.timed_out is False
    assert result.result_line == "RESULT: PR opened"
    stdout = result.stdout_path.read_text()
    stderr = result.stderr_path.read_text()
    assert "out1" in stdout
    assert "RESULT: PR opened" in stdout
    assert "err" in stderr
    assert result.duration_seconds >= 0


async def test_run_command_result_line_from_tail(tmp_path: Path) -> None:
    # More than TAIL_LINES lines before the RESULT line: it must still be
    # found (the tail deque holds the last 500 lines).
    script = (
        "i=0; while [ $i -lt 700 ]; do echo line $i; i=$((i+1)); done; "
        "echo 'RESULT: done'"
    )
    request = _request(tmp_path, ("sh", "-c", script))
    result = await run_command(request)
    assert result.result_line == "RESULT: done"


async def test_run_command_nonzero_exit(tmp_path: Path) -> None:
    request = _request(tmp_path, ("sh", "-c", "exit 3"))
    result = await run_command(request)
    assert result.exit_code == 3
    assert result.result_line is None


async def test_run_command_timeout_kills_process_group(tmp_path: Path) -> None:
    # A child sleep in the same group must die with the run.
    request = _request(tmp_path, ("sh", "-c", "sleep 30 & wait"))
    request = RunRequest(
        argv=request.argv,
        cwd=request.cwd,
        env=request.env,
        stdin_file=None,
        timeout_seconds=0.5,
        run_dir=request.run_dir,
        run_id=request.run_id,
        work_package_id=request.work_package_id,
        agent=request.agent,
    )
    result = await run_command(request)
    assert result.timed_out is True
    assert result.exit_code != 0


async def test_run_command_on_spawned_called(tmp_path: Path) -> None:
    spawned: list[tuple[int, int, str]] = []

    def on_spawned(pid: int, pgid: int, proc_start: str) -> None:
        spawned.append((pid, pgid, proc_start))

    request = _request(tmp_path, ("sh", "-c", "true"))
    await run_command(request, on_spawned=on_spawned)
    assert len(spawned) == 1
    pid, pgid, proc_start = spawned[0]
    assert pid > 0
    assert pid == pgid  # own process group (start_new_session)
    assert proc_start


async def test_run_command_missing_binary_raises(tmp_path: Path) -> None:
    request = _request(tmp_path, ("definitely-not-a-binary-xyz",))
    with pytest.raises(RunnerError, match="failed to start runner"):
        await run_command(request)


async def test_run_command_stdin_file(tmp_path: Path) -> None:
    prompt = tmp_path / "prompt.txt"
    prompt.write_text("the prompt text", encoding="utf-8")
    request = _request(tmp_path, ("sh", "-c", "cat > out.txt"))
    request = RunRequest(
        argv=request.argv,
        cwd=request.cwd,
        env=request.env,
        stdin_file=prompt,
        timeout_seconds=request.timeout_seconds,
        run_dir=request.run_dir,
        work_package_id=request.work_package_id,
        run_id=request.run_id,
        agent=request.agent,
    )
    result = await run_command(request)
    assert result.exit_code == 0
    assert (tmp_path / "out.txt").read_text() == "the prompt text"


async def test_generic_runner_run_end_to_end(tmp_path: Path) -> None:
    mapping = base_mapping()
    runners = mapping["runners"]
    assert isinstance(runners, dict)
    generic = runners["generic"]
    assert isinstance(generic, dict)
    generic["argv"] = [
        "sh",
        "-c",
        "head -c 20 {prompt_file}; echo; echo 'RESULT: ok'",
    ]
    generic["cwd"] = "{repo_dir}"
    generic["stdin_file"] = None
    runner = GenericRunner(WorkflowConfig.from_mapping(mapping, environ={}))
    prompt_file = tmp_path / "prompt.txt"
    prompt_file.write_text("implement the thing", encoding="utf-8")
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    request = runner.build_request(
        "FE",
        run_id=1,
        work_package_id=100,
        run_dir=tmp_path / "run",
        repo_dir=repo_dir,
        workspace_dir=tmp_path / "ws",
        branch="op-4-100",
        prompt_file=prompt_file,
        timeout_seconds=30,
    )
    result = await runner.run(request)
    assert result.exit_code == 0
    assert result.result_line == "RESULT: ok"
    assert "implement the thing" in result.stdout_path.read_text()


def test_run_result_is_frozen_dataclass() -> None:
    result = RunResult(
        exit_code=0,
        timed_out=False,
        duration_seconds=1.0,
        stdout_path=Path("/x"),
        stderr_path=Path("/y"),
        result_line=None,
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        result.exit_code = 1  # type: ignore[misc]
