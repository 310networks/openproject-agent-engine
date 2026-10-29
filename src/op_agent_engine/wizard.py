"""Interactive end-to-end installer.

``op-agent-engine setup`` is the engine behind ``install.sh``. It scaffolds the
configuration directory, copies the files the operator edited in the checkout,
provisions the OpenProject users, interviews the operator for one API token per
account, verifies every token through ``/api/v3/users/me``, and then runs the
configuration check, the systemd installation and a dry run.

Terminal I/O is isolated in small wrappers (``_prompt``, ``_secret_prompt``,
``_run_capture`` and ``_verify_token``) so the orchestration is testable
without a TTY or a live OpenProject instance.
"""

from __future__ import annotations

import asyncio
import getpass
import json
import os
import re
import subprocess
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TextIO

import yaml

from op_agent_engine import install
from op_agent_engine.install import SetupError
from op_agent_engine.openproject.client import (
    OpenProjectAuthError,
    OpenProjectClient,
    OpenProjectError,
)
from op_agent_engine.provision import TOKEN_ENV_PREFIX

SETUP_ERROR_EXIT = 1
OK_EXIT = 0
ABORT_EXIT = 130

ENGINE_TOKEN_VAR = "OP_ENGINE_TOKEN"
_TOKEN_REFERENCE = re.compile(
    rf"\$\{{({re.escape(TOKEN_ENV_PREFIX)}[A-Za-z0-9_]+)\}}"
)
_PLACEHOLDER_PREFIXES = (
    "replace-with",
    "change-me",
    "changeme",
    "your-",
    "placeholder",
)
MAX_TOKEN_ATTEMPTS = 3


@dataclass(frozen=True)
class TokenRequest:
    """One API token the operator must mint in the OpenProject UI."""

    env_var: str
    login: str
    label: str
    kind: str  # "engine" or "agent"


class UI:
    """Small terminal helper with optional ANSI colour."""

    def __init__(self, stream: TextIO | None = None) -> None:
        self.stream = stream or sys.stdout
        self.color = self.stream.isatty() and os.environ.get("NO_COLOR") is None

    def _write(self, text: str = "") -> None:
        print(text, file=self.stream, flush=True)

    def _style(self, code: str, text: str) -> str:
        if not self.color:
            return text
        return f"\033[{code}m{text}\033[0m"

    def title(self, text: str) -> None:
        self._write()
        self._write(self._style("1;36", text))
        self._write(self._style("36", "-" * len(text)))

    def step(self, number: int, text: str) -> None:
        self._write()
        self._write(self._style("1;36", f"[{number}] {text}"))

    def info(self, text: str) -> None:
        self._write(f"    {text}")

    def ok(self, text: str) -> None:
        self._write(self._style("32", f"    ok  {text}"))

    def warn(self, text: str) -> None:
        self._write(self._style("33", f"    !!  {text}"))

    def error(self, text: str) -> None:
        self._write(self._style("31", f"    error: {text}"))

    def credential(self, label: str, login: str, password: str) -> None:
        self._write(
            self._style(
                "1;33", f"    {label}: login {login} / password {password}"
            )
        )


# ---------------------------------------------------------------------------
# Injectable terminal and process helpers
# ---------------------------------------------------------------------------


def _prompt(text: str) -> str:
    return input(text)


def _secret_prompt(text: str) -> str:
    return getpass.getpass(text)


def _run_capture(
    command: Sequence[str], *, env: Mapping[str, str], cwd: Path
) -> tuple[int, str, str]:
    try:
        completed = subprocess.run(
            list(command),
            env=dict(env),
            cwd=cwd,
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError as error:
        raise SetupError(
            f"cannot run {' '.join(command)}: {error}"
        ) from error
    return completed.returncode, completed.stdout or "", completed.stderr or ""


def _verify_token(
    base_url: str, token: str, expected_login: str
) -> tuple[bool | None, str]:
    """Return ``(True, ...)`` for a match, ``(False, ...)`` for a mismatch.

    ``None`` means the token could not be checked (for example a network
    failure); the caller warns and continues so a transient outage does not
    abort the installation.
    """
    try:
        return asyncio.run(
            _verify_token_async(base_url, token, expected_login)
        )
    except Exception as error:
        return None, f"could not verify token: {error}"


async def _verify_token_async(
    base_url: str, token: str, expected_login: str
) -> tuple[bool | None, str]:
    try:
        async with OpenProjectClient(base_url, token) as client:
            payload = await client.get_json("/api/v3/users/me")
    except OpenProjectAuthError as error:
        return False, f"token rejected (HTTP {error.status_code})"
    except OpenProjectError as error:
        return None, f"could not verify token: {error}"

    login = payload.get("login") if isinstance(payload, dict) else None
    if not isinstance(login, str) or not login:
        return None, "the token did not return a login"
    if login != expected_login:
        return False, f"token belongs to {login!r}, expected {expected_login!r}"
    return True, f"verified as {login}"


# ---------------------------------------------------------------------------
# Configuration inspection
# ---------------------------------------------------------------------------


def is_placeholder(value: str | None) -> bool:
    """Return whether a value is empty or an example placeholder."""
    if value is None:
        return True
    normalized = value.strip().lower()
    if not normalized:
        return True
    if normalized in {"todo", "tbd", "none", "null"}:
        return True
    return normalized.startswith(_PLACEHOLDER_PREFIXES)


def _usable(value: str | None) -> bool:
    return not is_placeholder(value)


def load_workflow_raw(path: Path) -> dict[str, Any]:
    """Read ``workflow.yaml`` without interpolating missing secrets."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as error:
        raise SetupError(f"cannot read {path}: {error}") from error
    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as error:
        raise SetupError(f"{path}: invalid YAML: {error}") from error
    if not isinstance(raw, dict):
        raise SetupError(f"{path}: the top level must be a mapping")
    return raw


def token_requests(workflow: Mapping[str, Any]) -> list[TokenRequest]:
    """Return the engine token and one token per agent, in config order."""
    openproject = workflow.get("openproject")
    if not isinstance(openproject, dict):
        openproject = {}
    engine_user = openproject.get("engine_user") or "agent-engine"

    agents = workflow.get("agents")
    if not isinstance(agents, dict):
        raise SetupError("workflow.yaml: 'agents' must be a mapping")

    requests = [
        TokenRequest(
            env_var=ENGINE_TOKEN_VAR,
            login=str(engine_user),
            label="agent-engine (runtime)",
            kind="engine",
        )
    ]
    owners: dict[str, str] = {ENGINE_TOKEN_VAR: "agent-engine"}

    for key, agent in agents.items():
        if not isinstance(agent, dict):
            raise SetupError(f"workflow.yaml: agent {key!r} must be a mapping")
        login = agent.get("op_login") or key
        env = agent.get("env") or {}
        if not isinstance(env, dict):
            raise SetupError(
                f"workflow.yaml: agent {key!r} env must be a mapping"
            )
        names = sorted(
            {
                match
                for value in env.values()
                if isinstance(value, str)
                for match in _TOKEN_REFERENCE.findall(value)
            }
        )
        if not names:
            names = [f"{TOKEN_ENV_PREFIX}{str(key).upper()}"]

        for env_var in names:
            owner = owners.get(env_var)
            if owner is not None and owner != str(key):
                raise SetupError(
                    f"workflow.yaml: token variable {env_var} is used by both "
                    f"{owner} and {key}; each agent needs its own token"
                )
            if owner is None:
                requests.append(
                    TokenRequest(
                        env_var=env_var,
                        login=str(login),
                        label=f"{key} ({login})",
                        kind="agent",
                    )
                )
                owners[env_var] = str(key)
    return requests


def _require_provisioning_values(env: Mapping[str, str]) -> str:
    base_url = (env.get("OP_BASE_URL") or "").strip()
    if not base_url.startswith(("http://", "https://")) or "example.com" in base_url:
        raise SetupError(
            "engine.env: set OP_BASE_URL to your OpenProject URL "
            "(the example value is not usable)"
        )
    if not _usable(env.get("OP_PROVISION_TOKEN")) and not _usable(
        env.get("OP_ENGINE_TOKEN")
    ):
        raise SetupError(
            "engine.env: set OP_PROVISION_TOKEN to an OpenProject "
            "administrator API token before running the installer"
        )
    return base_url.rstrip("/")


def _build_env(
    file_env: Mapping[str, str], config_dir: Path
) -> dict[str, str]:
    env = dict(os.environ)
    env.update(file_env)
    # A placeholder admin token would shadow a usable OP_ENGINE_TOKEN in
    # ``provisioning_token``; remove it so the fallback can work.
    if is_placeholder(env.get("OP_PROVISION_TOKEN")):
        env.pop("OP_PROVISION_TOKEN", None)
    env["ENGINE_CONFIG"] = str(config_dir / "workflow.yaml")
    env.setdefault("LOG_LEVEL", "INFO")
    # ``install-service`` resolves the console script through PATH. Make sure
    # the interpreter's bin directory and the uv shim directory are present
    # even when the caller used an absolute path.
    bin_dirs = [str(Path(sys.executable).parent), str(Path.home() / ".local" / "bin")]
    current_path = env.get("PATH", "")
    env["PATH"] = os.pathsep.join([*bin_dirs, current_path]) if current_path else (
        os.pathsep.join(bin_dirs)
    )
    return env


def _provision_env(
    file_env: Mapping[str, str],
    config_dir: Path,
    requests: Sequence[TokenRequest],
) -> dict[str, str]:
    """Environment for ``provision``: unresolved tokens are empty, not fake.

    ``workflow.yaml`` interpolates every ``${VAR}`` reference, so the variables
    must be present. Empty values make ``check_agent_tokens`` report "no token
    configured" instead of attempting an authentication with a placeholder.
    """
    env = _build_env(file_env, config_dir)
    for request in requests:
        if is_placeholder(env.get(request.env_var)):
            env[request.env_var] = ""
    return env


# ---------------------------------------------------------------------------
# Prompting
# ---------------------------------------------------------------------------


def _ask_yes_no(text: str, *, default: bool, assume_yes: bool) -> bool:
    if assume_yes:
        return True
    answer = _prompt(text).strip().lower()
    if not answer:
        return default
    return answer in {"y", "yes"}


def _reuse_token(
    request: TokenRequest,
    existing: str,
    base_url: str,
    ui: UI,
) -> bool:
    """Keep an already stored token without prompting.

    Re-running ``install.sh`` after a failed install must resume, not repeat the
    token interview. A stored token is reused when it still authenticates as the
    expected login; when it cannot be checked (network down, unroutable
    OpenProject) it is kept with a warning so the operator can still get to
    ``check-config``. A token that is actively rejected is dropped so a new one
    must be entered.
    """
    verified, message = _verify_token(base_url, existing, request.login)
    if verified is False:
        ui.warn(f"{request.env_var}: the existing token {message}")
        return False
    if verified is True:
        ui.ok(f"{request.env_var}: reusing the existing token ({message})")
    else:
        ui.warn(f"{request.env_var}: reusing the existing token ({message})")
    return True


def _prompt_token(
    request: TokenRequest,
    existing: str | None,
    base_url: str,
    ui: UI,
) -> str:
    keep = existing if _usable(existing) else None
    hint = " [press Enter to keep the existing token]" if keep else ""
    for attempt in range(1, MAX_TOKEN_ATTEMPTS + 1):
        raw = _secret_prompt(
            f"    {request.label} token ({request.env_var}){hint}: "
        )
        value = raw.strip()
        if not value:
            if keep is not None:
                value = keep
            else:
                ui.warn("a token is required")
                continue

        verified, message = _verify_token(base_url, value, request.login)
        if verified is True:
            ui.ok(f"{request.env_var}: {message}")
            return value
        if verified is None:
            ui.warn(f"{request.env_var}: {message}; continuing without verification")
            return value
        ui.warn(f"{request.env_var}: {message}")
        if attempt == MAX_TOKEN_ATTEMPTS:
            break
        hint = ""

    raise SetupError(
        f"could not verify a token for {request.label} after "
        f"{MAX_TOKEN_ATTEMPTS} attempts"
    )


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def _engine_command() -> list[str]:
    # ``sys.executable`` is the uv-tool virtualenv interpreter when the setup
    # command itself was installed by uv, so this does not depend on PATH.
    return [sys.executable, "-m", "op_agent_engine"]


def _failure_detail(stdout: str, stderr: str) -> str:
    detail = stderr.strip() or stdout.strip()
    return f"\n{detail}" if detail else ""


def _provision_failure_detail(stdout: str, stderr: str) -> str:
    """Describe a failed provision without echoing generated passwords."""
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError:
        return _failure_detail("", stderr)
    if not isinstance(payload, dict):
        return _failure_detail("", stderr)
    parts: list[str] = []
    errors = payload.get("errors")
    if isinstance(errors, list):
        parts.extend(str(error) for error in errors)
    missing = payload.get("missing_roles")
    if isinstance(missing, list) and missing:
        parts.append("missing roles: " + ", ".join(str(role) for role in missing))
    return ("\n" + "\n".join(parts)) if parts else _failure_detail("", stderr)


def _run_captured(
    args: Sequence[str],
    *,
    env: Mapping[str, str],
    cwd: Path,
    ui: UI,
    purpose: str,
) -> tuple[str, str]:
    command = [*_engine_command(), *args]
    code, stdout, stderr = _run_capture(command, env=env, cwd=cwd)
    if code != 0:
        raise SetupError(
            f"{purpose} failed with exit code {code}"
            + _failure_detail(stdout, stderr)
        )
    return stdout, stderr


def _provision_users(
    *,
    env: Mapping[str, str],
    cwd: Path,
    config_path: Path,
    ui: UI,
) -> dict[str, str]:
    command = [
        *_engine_command(),
        "provision",
        "--apply",
        "--json",
        "--log-level",
        "CRITICAL",
        "--config",
        str(config_path),
    ]
    code, stdout, stderr = _run_capture(command, env=env, cwd=cwd)
    if code != 0:
        raise SetupError(
            "op-agent-engine provision --apply failed with exit code "
            f"{code}{_provision_failure_detail(stdout, stderr)}"
        )
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError as error:
        raise SetupError(f"provision returned invalid JSON: {error}") from error
    if not isinstance(payload, dict):
        raise SetupError("provision returned an unexpected JSON payload")
    writes = payload.get("writes")
    if isinstance(writes, int):
        ui.ok(f"provisioned {writes} change(s)")
    passwords = payload.get("passwords")
    if not isinstance(passwords, dict):
        return {}
    return {
        str(login): str(password)
        for login, password in passwords.items()
        if isinstance(password, str)
    }


def run_setup(
    *,
    source_dir: Path,
    config_dir: Path,
    skip_service: bool = False,
    assume_yes: bool = False,
    reprompt_tokens: bool = False,
    ui: UI | None = None,
) -> int:
    """Run the interactive installer; return a process exit code."""
    ui = ui or UI()
    try:
        _run_setup(
            source_dir=source_dir.expanduser(),
            config_dir=config_dir.expanduser(),
            skip_service=skip_service,
            assume_yes=assume_yes,
            reprompt_tokens=reprompt_tokens,
            ui=ui,
        )
        return OK_EXIT
    except KeyboardInterrupt:
        ui.warn("installation aborted")
        return ABORT_EXIT
    except SetupError as error:
        ui.error(str(error))
        return SETUP_ERROR_EXIT


def _run_setup(
    *,
    source_dir: Path,
    config_dir: Path,
    skip_service: bool,
    assume_yes: bool,
    ui: UI,
    reprompt_tokens: bool = False,
) -> None:
    source_workflow = source_dir / "workflow.yaml"
    source_env = source_dir / "engine.env"
    if not source_workflow.is_file():
        raise SetupError(
            f"missing {source_workflow}; copy workflow.example.yaml "
            "to workflow.yaml in the checkout root and edit it"
        )
    if not source_env.is_file():
        raise SetupError(
            f"missing {source_env}; copy engine.env.example to "
            "engine.env in the checkout root and edit it"
        )
    try:
        os.chmod(source_env, 0o600)
    except OSError as error:
        raise SetupError(f"cannot secure {source_env}: {error}") from error

    workflow = load_workflow_raw(source_workflow)
    requests = token_requests(workflow)
    file_env = install.load_env_file(source_env)
    base_url = _require_provisioning_values(file_env)

    ui.title("OpenProject Agent Engine")
    ui.info(f"config: {config_dir}")

    # 1. Install the operator-edited files and scaffold the config directory.
    # Copy first so ``init`` sees existing files and keeps them; this avoids
    # pointless backups of the freshly generated examples.
    ui.step(1, "Installing configuration and scaffolding")
    # Snapshot the tokens of the previous run before the checkout's engine.env
    # (often still the example, with placeholders) replaces it. Without this a
    # re-run after a failed install would throw away tokens that were already
    # entered, which is exactly what resuming is supposed to avoid.
    config_env_path = config_dir / "engine.env"
    previous_tokens: dict[str, str] = {}
    if config_env_path.is_file():
        previous = install.load_env_file(config_env_path)
        wanted = {request.env_var for request in requests}
        previous_tokens = {
            name: value
            for name, value in previous.items()
            if name in wanted and _usable(value) and value is not None
        }

    report = install.install_source_config(source_dir, config_dir)
    for path in report.copied:
        ui.ok(f"installed {path}")
    for path in report.backups:
        ui.warn(f"backed up the previous file to {path}")
    for path in report.kept:
        ui.info(f"unchanged {path}")
    if not report.ok:
        raise SetupError("; ".join(report.errors))

    init_env = _build_env(file_env, config_dir)
    _run_captured(
        ["init", "--config-dir", str(config_dir)],
        env=init_env,
        cwd=source_dir,
        ui=ui,
        purpose="op-agent-engine init",
    )

    config_env_path = config_dir / "engine.env"
    file_env = install.load_env_file(config_env_path)

    restored = {
        name: value
        for name, value in previous_tokens.items()
        if not _usable(file_env.get(name))
    }
    if restored:
        install.update_env_file(config_env_path, restored)
        if source_env.resolve() != config_env_path.resolve():
            install.update_env_file(source_env, restored)
        file_env.update(restored)
        for name in sorted(restored):
            ui.info(
                f"{name}: restored from the previous install "
                "(the checkout's engine.env has a placeholder)"
            )

    # 2. Provision users and memberships.
    ui.step(2, "Provisioning OpenProject users and memberships")
    provision_env = _provision_env(file_env, config_dir, requests)
    passwords = _provision_users(
        env=provision_env,
        cwd=source_dir,
        config_path=config_dir / "workflow.yaml",
        ui=ui,
    )

    # 3. Collect one API token per account.
    ui.step(3, "Create one API token per account")
    ui.info(
        "OpenProject administrators cannot mint API tokens for other users."
    )
    ui.info("Log in as each account below and create a token under:")
    ui.info("  My account -> Access tokens")
    ui.info(
        "The agent-engine runtime user must be an administrator in "
        "OpenProject."
    )
    if passwords:
        ui.info(
            f"Generated passwords are shown once, at that account's token "
            f"prompt (sign in at {base_url})."
        )

    updates: dict[str, str] = {}
    warned_logins: set[str] = set()
    reused = 0
    for request in requests:
        existing = file_env.get(request.env_var)
        password = passwords.get(request.login)

        # Resuming a previous run: keep tokens that are already stored and
        # still valid instead of interviewing the operator again.
        if not reprompt_tokens and _usable(existing) and existing is not None:
            if _reuse_token(request, existing, base_url, ui):
                reused += 1
                continue
            existing = None

        if password:
            ui.credential(request.label, request.login, password)
        elif not _usable(existing) and request.login not in warned_logins:
            warned_logins.add(request.login)
            ui.warn(
                f"{request.login} already exists, so this installer cannot "
                "show its password. Reset it in OpenProject "
                f"(Administration -> Users -> {request.login}) or paste a "
                "token you already created for it."
            )
        if request.kind == "engine":
            ui.warn(
                f"make {request.login} an administrator before creating this "
                "token: Administration -> Users -> "
                f"{request.login} -> Administrator"
            )
        updates[request.env_var] = _prompt_token(
            request, existing, base_url, ui
        )

    if reused:
        ui.info(
            f"reused {reused} existing token(s); pass --reprompt-tokens to "
            "enter them again"
        )
    install.update_env_file(config_env_path, updates)
    if source_env.resolve() != config_env_path.resolve():
        install.update_env_file(source_env, updates)
    file_env.update(updates)
    ui.ok(f"tokens saved to {config_env_path} (mode 0600)")

    # 4. Validate the complete configuration against the live instance.
    ui.step(4, "Validating the configuration")
    verify_env = _build_env(file_env, config_dir)
    _run_captured(
        ["check-config", "--config", str(config_dir / "workflow.yaml")],
        env=verify_env,
        cwd=source_dir,
        ui=ui,
        purpose="op-agent-engine check-config",
    )
    ui.ok("configuration verified")

    # 5. Install the systemd service.
    ui.step(5, "Installing the systemd service")
    if skip_service:
        ui.info("skipped (--skip-service); run: op-agent-engine install-service --apply")
    elif _ask_yes_no(
        "    Install and start the systemd service now (needs sudo)? [Y/n] ",
        default=True,
        assume_yes=assume_yes,
    ):
        _run_captured(
            ["install-service", "--apply"],
            env=verify_env,
            cwd=source_dir,
            ui=ui,
            purpose="op-agent-engine install-service --apply",
        )
        ui.ok("service installed and enabled")
    else:
        ui.info("skipped; run: op-agent-engine install-service --apply")

    # 6. One poll, without writing anything.
    ui.step(6, "Running a dry poll")
    _run_captured(
        ["dry-run", "--config", str(config_dir / "workflow.yaml")],
        env=verify_env,
        cwd=source_dir,
        ui=ui,
        purpose="op-agent-engine dry-run",
    )
    ui.ok("dry run completed")

    ui.title("Installation complete")
    ui.info("Follow the service with: journalctl -u op-agent-engine -f")
    ui.info(f"Configuration: {config_dir}")
