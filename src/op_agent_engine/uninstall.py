"""Uninstall helpers: local files, systemd units and OpenProject users.

``op-agent-engine uninstall`` is behind ``uninstall.sh``. It is intentionally
conservative:

* dry-run is the default;
* service units are stopped before any OpenProject user is touched;
* OpenProject users are deleted when the instance allows it, otherwise locked;
* administrator accounts and the token owner are never removed;
* local configuration is kept when user cleanup fails, so the operator can
  retry with the same credentials.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from op_agent_engine import install
from op_agent_engine.config import ConfigError, WorkflowConfig
from op_agent_engine.install import SetupError
from op_agent_engine.openproject.client import (
    OpenProjectAuthError,
    OpenProjectClient,
    OpenProjectError,
    OpenProjectNotFoundError,
)
from op_agent_engine.provision import (
    ProvisioningError,
    _find_membership,
    _find_user,
    _resolve_project_ids,
    require_admin,
)
from op_agent_engine.wizard import UI, load_workflow_raw

OK_EXIT = 0
UNINSTALL_ERROR_EXIT = 1
ABORT_EXIT = 130


@dataclass(frozen=True)
class UninstallOptions:
    """Everything the uninstaller needs to plan and perform its work."""

    source_dir: Path
    config_dir: Path
    data_dir: Path
    keep_config: bool = False
    keep_data: bool = False
    keep_users: bool = False
    purge_agent_repos: bool = False
    apply: bool = False
    assume_yes: bool = False


@dataclass(frozen=True)
class UserAction:
    login: str
    action: str
    detail: str


@dataclass
class UserRemovalReport:
    actions: list[UserAction] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors

    def summary(self) -> list[str]:
        lines = [
            f"  {action.login}: {action.action} ({action.detail})"
            for action in self.actions
        ]
        lines.extend(f"  warning: {warning}" for warning in self.warnings)
        lines.extend(f"  error: {error}" for error in self.errors)
        return lines


@dataclass
class LoadedRuntime:
    config: WorkflowConfig
    env: dict[str, str]
    workflow_path: Path
    env_path: Path


# ---------------------------------------------------------------------------
# Configuration loading
# ---------------------------------------------------------------------------


def _first_existing(*paths: Path) -> Path | None:
    for path in paths:
        if path.is_file():
            return path
    return None


def load_runtime(source_dir: Path, config_dir: Path) -> LoadedRuntime:
    workflow_path = _first_existing(
        config_dir / "workflow.yaml", source_dir / "workflow.yaml"
    )
    env_path = _first_existing(config_dir / "engine.env", source_dir / "engine.env")
    if workflow_path is None or env_path is None:
        raise SetupError(
            "cannot find workflow.yaml and engine.env; keep the configuration "
            "or pass --keep-users to skip OpenProject cleanup"
        )
    file_env = install.load_env_file(env_path)
    environ = dict(os.environ)
    environ.update(file_env)
    config = WorkflowConfig.load(workflow_path, environ=environ)
    return LoadedRuntime(
        config=config,
        env=file_env,
        workflow_path=workflow_path,
        env_path=env_path,
    )


def _api_credentials(env: Mapping[str, str]) -> tuple[str, str]:
    base_url = (env.get("OP_BASE_URL") or "").strip().rstrip("/")
    token = (env.get("OP_PROVISION_TOKEN") or env.get("OP_ENGINE_TOKEN") or "").strip()
    if not base_url:
        raise SetupError("engine.env has no OP_BASE_URL")
    if not token:
        raise SetupError(
            "engine.env has no OP_PROVISION_TOKEN or OP_ENGINE_TOKEN; an "
            "administrator token is required to remove OpenProject users"
        )
    return base_url, token


def _planned_logins(workflow_path: Path) -> list[str]:
    raw = load_workflow_raw(workflow_path)
    openproject = raw.get("openproject")
    engine_user = (
        openproject.get("engine_user")
        if isinstance(openproject, dict)
        else None
    ) or "agent-engine"
    logins = [str(engine_user)]
    agents = raw.get("agents")
    if isinstance(agents, dict):
        for key, agent in agents.items():
            if isinstance(agent, dict):
                login = agent.get("op_login") or key
                logins.append(str(login))
    return list(dict.fromkeys(logins))


# ---------------------------------------------------------------------------
# OpenProject users
# ---------------------------------------------------------------------------


async def remove_openproject_users(
    client: OpenProjectClient,
    config: WorkflowConfig,
    *,
    apply: bool,
    lock_fallback: bool = True,
) -> UserRemovalReport:
    """Remove the engine and agent users declared in ``workflow.yaml``.

    Memberships in the configured projects are removed first. User deletion is
    attempted; when the instance forbids it (HTTP 403), the account is locked
    instead. Administrators and the account that owns the API token are always
    skipped.
    """
    report = UserRemovalReport()
    try:
        admin_login = await require_admin(client)
    except OpenProjectError as error:
        raise SetupError(str(error)) from error

    logins = [config.openproject.engine_user]
    logins.extend(agent.op_login for agent in config.agents.values())
    logins = list(dict.fromkeys(logins))

    project_ids: dict[str, int] = {}
    try:
        project_ids = await _resolve_project_ids(client, config)
    except ProvisioningError as error:
        report.warnings.append(
            f"memberships were not resolved: {error}; user deletion may still "
            "remove them"
        )

    for login in logins:
        if login == admin_login:
            report.actions.append(
                UserAction(login, "skipped", "token owner")
            )
            continue
        try:
            user = await _find_user(client, login)
        except OpenProjectError as error:
            report.errors.append(f"{login}: lookup failed: {error}")
            continue
        if user is None:
            report.actions.append(UserAction(login, "not-found", "no such user"))
            continue
        if user.get("admin") is True:
            report.actions.append(
                UserAction(login, "skipped", "administrator account")
            )
            continue
        user_id = user.get("id")
        if not isinstance(user_id, int):
            report.errors.append(f"{login}: user has no integer id")
            continue
        if not apply:
            report.actions.append(UserAction(login, "would-delete", f"id {user_id}"))
            continue

        for project_id in project_ids.values():
            try:
                membership = await _find_membership(client, user_id, project_id)
                if membership is not None:
                    membership_id = membership.get("id")
                    if isinstance(membership_id, int):
                        await client.delete(f"/api/v3/memberships/{membership_id}")
            except OpenProjectNotFoundError:
                continue
            except OpenProjectError as error:
                report.errors.append(
                    f"{login}: membership cleanup failed: {error}"
                )

        try:
            await client.delete(f"/api/v3/users/{user_id}")
            report.actions.append(UserAction(login, "deleted", f"id {user_id}"))
            continue
        except OpenProjectNotFoundError:
            report.actions.append(UserAction(login, "not-found", f"id {user_id}"))
            continue
        except OpenProjectAuthError as error:
            if error.status_code != 403 or not lock_fallback:
                report.errors.append(f"{login}: delete failed: {error}")
                continue
            try:
                # OpenProject rejects an empty POST with 406; send JSON {}
                # so the request carries a Content-Type header.
                await client.post_json(
                    f"/api/v3/users/{user_id}/lock", json_body={}
                )
                report.actions.append(
                    UserAction(login, "locked", "delete forbidden by instance")
                )
            except OpenProjectError as lock_error:
                report.errors.append(
                    f"{login}: delete forbidden and lock failed: {lock_error}"
                )
            continue
        except OpenProjectError as error:
            report.errors.append(f"{login}: delete failed: {error}")

    return report


# ---------------------------------------------------------------------------
# Local cleanup
# ---------------------------------------------------------------------------


def _sudo_prefix() -> list[str]:
    return [] if os.geteuid() == 0 else ["sudo"]


def _run(command: Sequence[str]) -> tuple[int, str]:
    try:
        completed = subprocess.run(
            list(command),
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError as error:
        return 1, str(error)
    return completed.returncode, (completed.stderr or completed.stdout or "").strip()


def _systemctl_available() -> bool:
    return shutil.which("systemctl") is not None


def _sudo_available() -> bool:
    return os.geteuid() == 0 or shutil.which("sudo") is not None


def service_plan() -> list[list[str]]:
    sudo = _sudo_prefix()
    commands = [
        [*sudo, "systemctl", "disable", "--now", "op-agent-engine.service"],
        [*sudo, "systemctl", "disable", "--now", "op-agent-engine-cleanup.timer"],
    ]
    commands.extend(
        [*sudo, "rm", "-f", f"/etc/systemd/system/{name}"]
        for name in install.UNIT_NAMES
    )
    commands.append([*sudo, "systemctl", "daemon-reload"])
    return commands


def stop_service(ui: UI) -> None:
    """Stop and disable the engine and retention timer, best effort."""
    if not _systemctl_available() or not _sudo_available():
        ui.warn("systemctl/sudo not available; stop the service manually")
        return
    for unit in ("op-agent-engine.service", "op-agent-engine-cleanup.timer"):
        code, detail = _run([*_sudo_prefix(), "systemctl", "disable", "--now", unit])
        if code == 0:
            continue
        lowered = detail.lower()
        if "does not exist" in lowered or "not loaded" in lowered:
            continue
        ui.warn(f"{unit}: {detail or 'not loaded'}")


def remove_service_files(ui: UI) -> None:
    """Remove the unit files and reload systemd."""
    if not _sudo_available():
        ui.warn("sudo not available; remove /etc/systemd/system units manually")
        return
    for name in install.UNIT_NAMES:
        path = f"/etc/systemd/system/{name}"
        code, detail = _run([*_sudo_prefix(), "rm", "-f", path])
        if code != 0:
            ui.error(f"cannot remove {path}: {detail}")
    if _systemctl_available():
        code, detail = _run([*_sudo_prefix(), "systemctl", "daemon-reload"])
        if code != 0:
            ui.warn(f"systemctl daemon-reload: {detail}")


def local_targets(options: UninstallOptions, *, workspace_root: Path | None) -> list[Path]:
    targets: list[Path] = []
    if not options.keep_config:
        targets.append(options.config_dir)
        for name in ("workflow.yaml", "engine.env"):
            targets.append(options.source_dir / name)
        for pattern in (
            "workflow.yaml.bak.*",
            "engine.env.bak.*",
            ".workflow.yaml.tmp",
            ".engine.env.tmp",
        ):
            targets.extend(options.source_dir.glob(pattern))
    if not options.keep_data:
        targets.append(options.data_dir)
    if (
        options.purge_agent_repos
        and workspace_root is not None
        and workspace_root.is_dir()
    ):
        for child in workspace_root.iterdir():
            repository = child / "repositories"
            if repository.is_dir():
                targets.append(repository)
    return _unique_paths(targets)


def _unique_paths(paths: Sequence[Path]) -> list[Path]:
    seen: set[str] = set()
    unique: list[Path] = []
    for path in paths:
        key = str(path.expanduser().resolve(strict=False))
        if key in seen:
            continue
        seen.add(key)
        unique.append(path.expanduser())
    return unique


def remove_local_paths(paths: Sequence[Path], *, apply: bool) -> list[str]:
    actions: list[str] = []
    for path in paths:
        if not path.exists() and not path.is_symlink():
            continue
        actions.append(f"remove {path}")
        if not apply:
            continue
        try:
            if path.is_dir() and not path.is_symlink():
                shutil.rmtree(path)
            else:
                path.unlink()
        except OSError as error:
            raise SetupError(f"cannot remove {path}: {error}") from error
    return actions


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def _confirm(ui: UI) -> bool:
    if not sys.stdin.isatty():
        raise SetupError(
            "refusing to remove data non-interactively; re-run with --yes"
        )
    answer = input("    Type 'yes' to continue: ").strip().lower()
    return answer in {"y", "yes"}


def run_uninstall(options: UninstallOptions, *, ui: UI | None = None) -> int:
    """Plan or perform an uninstall; return a process exit code."""
    ui = ui or UI()
    try:
        return _run_uninstall(options, ui)
    except KeyboardInterrupt:
        ui.warn("uninstall aborted")
        return ABORT_EXIT
    except (SetupError, ConfigError) as error:
        ui.error(str(error))
        return UNINSTALL_ERROR_EXIT


def _run_uninstall(options: UninstallOptions, ui: UI) -> int:
    workflow_path = _first_existing(
        options.config_dir / "workflow.yaml",
        options.source_dir / "workflow.yaml",
    )
    logins: list[str] = []
    if workflow_path is not None:
        try:
            logins = _planned_logins(workflow_path)
        except SetupError as error:
            ui.warn(f"cannot read the user list: {error}")

    ui.title("OpenProject Agent Engine uninstall")
    if options.apply:
        ui.warn("this permanently removes local files and OpenProject users")
    else:
        ui.info("dry run; nothing will be changed")

    ui.info("service: op-agent-engine + cleanup timer")
    if not options.keep_config:
        ui.info(f"config:  {options.config_dir}")
        for name in ("workflow.yaml", "engine.env"):
            path = options.source_dir / name
            if path.exists():
                ui.info(f"checkout: {path}")
    if not options.keep_data:
        ui.info(f"data:    {options.data_dir}")
    if not options.keep_users:
        ui.info("users:   " + (", ".join(logins) if logins else "(config not found)"))
    if options.purge_agent_repos:
        ui.info("agent repositories under WORKSPACE_ROOT")

    if not options.apply:
        ui.info("run ./uninstall.sh --apply to perform these steps")
        return OK_EXIT
    if not options.assume_yes and not _confirm(ui):
        ui.warn("uninstall cancelled")
        return ABORT_EXIT

    runtime: LoadedRuntime | None = None
    if not options.keep_users or options.purge_agent_repos:
        runtime = load_runtime(options.source_dir, options.config_dir)

    stop_service(ui)

    if not options.keep_users:
        assert runtime is not None
        base_url, token = _api_credentials(runtime.env)
        report = asyncio.run(
            _remove_users_async(base_url, token, runtime.config)
        )
        ui.info("OpenProject users:")
        for line in report.summary():
            ui.info(line)
        if report.errors:
            ui.error("OpenProject cleanup failed; local configuration was kept")
            return UNINSTALL_ERROR_EXIT
        ui.ok("OpenProject users removed or locked")

    remove_service_files(ui)
    workspace_root = None
    if runtime is not None:
        raw_root = runtime.env.get("WORKSPACE_ROOT")
        if raw_root:
            workspace_root = Path(raw_root).expanduser()
    targets = local_targets(options, workspace_root=workspace_root)
    for action in remove_local_paths(targets, apply=True):
        ui.info(action)
    ui.ok("local files removed")
    ui.title("Uninstall complete")
    return OK_EXIT


async def _remove_users_async(
    base_url: str, token: str, config: WorkflowConfig
) -> UserRemovalReport:
    async with OpenProjectClient(base_url, token) as client:
        return await remove_openproject_users(client, config, apply=True)
