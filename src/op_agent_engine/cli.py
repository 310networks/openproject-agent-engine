"""Command line interface.

Subcommands: ``run``, ``once``, ``check-config``, ``status`` and ``dry-run``.
``check-config`` validates the environment and resolves all OpenProject IDs.
``once`` performs a single poll and claims what the slot rules allow.
``dry-run`` performs a single poll and logs what would be claimed without
writing anything. ``run`` starts the engine loop (poll, schedule, execute,
sleep). ``status`` prints the heartbeat age, active runs and the last finished
runs from the local store. ``provision`` and ``verify-token`` support the
interactive setup script, ``dashboard`` serves the read-only UI and ``cleanup``
is the retention job.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from collections.abc import Sequence
from pathlib import Path

from pydantic import ValidationError

from op_agent_engine import __version__
from op_agent_engine.config import (
    GIT_REPO_FIELD,
    ConfigError,
    EngineSettings,
    WorkflowConfig,
)
from op_agent_engine.dashboard import (
    DEFAULT_LIMIT,
    DEFAULT_REFRESH_SECONDS,
    MAX_LIMIT,
    serve_dashboard,
)
from op_agent_engine.engine import (
    HEARTBEAT_FILE_NAME,
    PAUSE_FILE_NAME,
    STORE_FILE_NAME,
    read_heartbeat,
    run_engine,
    run_engine_once,
)
from op_agent_engine.logging import configure_logging
from op_agent_engine.openproject.client import OpenProjectClient, OpenProjectError
from op_agent_engine.openproject.resolve import OpenProjectResolution, resolve_workflow
from op_agent_engine.providers import (
    credential_status,
    provider_of,
    stored_credential_providers,
)
from op_agent_engine.provision import (
    ProvisioningError,
    ProvisionReport,
    audit_workflow,
    missing_tokens,
    provision_workflow,
    provisioning_token,
    require_admin,
)
from op_agent_engine.retention import CleanupReport, cleanup_run_dirs
from op_agent_engine.store import RUNS_DIR_NAME, Run, Store
from op_agent_engine.workspace import (
    WorktreeCleanupReport,
    cleanup_stale_worktrees,
)

CONFIG_ERROR_EXIT = 1
OK_EXIT = 0


def _add_common_options(
    parser: argparse.ArgumentParser, *, suppress_defaults: bool = False
) -> None:
    """Options accepted both before and after the subcommand.

    ``SUPPRESS`` on the subparser avoids overwriting a value parsed before the
    subcommand.
    """
    default: object = argparse.SUPPRESS if suppress_defaults else None
    parser.add_argument(
        "--log-level",
        default=default,
        metavar="LEVEL",
        help="log level (default: $LOG_LEVEL or INFO)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=argparse.SUPPRESS if suppress_defaults else False,
        help="log claims, transitions and spawns without performing them",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="op-agent-engine",
        description=(
            "Poll OpenProject work packages and dispatch coding agents to work on them."
        ),
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )
    _add_common_options(parser)

    subparsers = parser.add_subparsers(dest="command", required=True, metavar="command")

    subparsers.add_parser(
        "run",
        help="start the polling engine loop",
        parents=[_subcommand_options()],
    )
    subparsers.add_parser(
        "once",
        help="run a single poll and exit",
        parents=[_subcommand_options()],
    )
    subparsers.add_parser(
        "dry-run",
        help="run a single poll and log the dispatch plan without writing anything",
        parents=[_subcommand_options()],
    )

    check = subparsers.add_parser(
        "check-config",
        help="validate the workflow configuration file",
        parents=[_subcommand_options()],
    )
    check.add_argument(
        "--json",
        action="store_true",
        help="print the summary as JSON",
    )
    check.add_argument(
        "--offline",
        action="store_true",
        help="validate the file only; do not resolve OpenProject IDs",
    )

    status = subparsers.add_parser(
        "status",
        help="print heartbeat age, active runs and recent runs",
        parents=[_subcommand_options()],
    )
    status.add_argument(
        "--json",
        action="store_true",
        help="print the status as JSON",
    )

    dashboard = subparsers.add_parser(
        "dashboard",
        help="serve the read-only web dashboard",
        parents=[_subcommand_options()],
    )
    dashboard.add_argument(
        "--host",
        default=None,
        metavar="HOST",
        help="bind address (default: $DASHBOARD_HOST or 127.0.0.1)",
    )
    dashboard.add_argument(
        "--port",
        type=int,
        default=None,
        metavar="PORT",
        help="bind port (default: $DASHBOARD_PORT or 8765)",
    )
    dashboard.add_argument(
        "--limit",
        type=int,
        default=DEFAULT_LIMIT,
        metavar="N",
        help=f"recent sessions in the snapshot (1-{MAX_LIMIT}, default: {DEFAULT_LIMIT})",
    )
    dashboard.add_argument(
        "--refresh",
        type=int,
        default=DEFAULT_REFRESH_SECONDS,
        metavar="SECONDS",
        help=f"browser refresh interval (default: {DEFAULT_REFRESH_SECONDS})",
    )

    provision = subparsers.add_parser(
        "provision",
        help="reconcile agent users, roles and scoped project memberships",
        parents=[_subcommand_options()],
    )
    provision.add_argument(
        "--apply",
        action="store_true",
        help="perform writes (default: dry-run)",
    )
    provision.add_argument(
        "--json",
        action="store_true",
        help="print the provisioning report as JSON",
    )
    provision.add_argument(
        "--passwords",
        action="store_true",
        help=(
            "print only the generated passwords as login<TAB>password lines "
            "(used by docker/agent-init.sh)"
        ),
    )

    cleanup = subparsers.add_parser(
        "cleanup",
        help="remove run directories older than the retention period",
        parents=[_subcommand_options()],
    )
    cleanup.add_argument(
        "--days",
        type=int,
        default=None,
        metavar="N",
        help="retention in days (default: $RUN_RETENTION_DAYS or 30)",
    )
    cleanup.add_argument(
        "--json",
        action="store_true",
        help="print the cleanup report as JSON",
    )

    verify = subparsers.add_parser(
        "verify-token",
        help="verify that OP_VERIFY_TOKEN authenticates as --login",
    )
    verify.add_argument(
        "--login",
        required=True,
        metavar="LOGIN",
        help="expected OpenProject login for the token",
    )
    verify.add_argument(
        "--admin",
        action="store_true",
        help="also require the account to be an OpenProject administrator",
    )

    return parser


def _subcommand_options() -> argparse.ArgumentParser:
    parent = argparse.ArgumentParser(add_help=False)
    _add_common_options(parent, suppress_defaults=True)
    return parent


def _load_config(settings: EngineSettings | None = None) -> WorkflowConfig:
    return WorkflowConfig.from_settings(settings or EngineSettings())


def _config_summary(config: WorkflowConfig) -> dict[str, object]:
    return {
        "source": "environment",
        "statuses": config.openproject.statuses.by_key(),
        "agent_field": config.openproject.agent_field,
        "repo_field": GIT_REPO_FIELD,
        "default_branch": config.repo_discovery.default_branch,
        "engine_user": config.openproject.engine_user,
        "dispatch_priority": config.openproject.dispatch_priority,
        "eligible_types": config.openproject.eligible_types,
        "projects": [
            {
                "identifier": project.identifier,
                "github": project.repo.github if project.repo else None,
                "dir": project.repo.dir if project.repo else None,
                "default_branch": (
                    project.repo.default_branch if project.repo else None
                ),
                "enabled": project.enabled,
            }
            for project in config.projects
        ],
        "provisioning": {
            "enabled": config.openproject.provisioning.enabled,
            "agent_role": config.openproject.provisioning.agent_role,
            "engine_role": config.openproject.provisioning.engine_role,
        },
        "agents": {
            key: {
                "name": agent.name,
                "kind": agent.kind,
                "op_login": agent.op_login,
                "workspace": agent.workspace,
                "runner": agent.runner,
                "model": config.model_for(key),
                "provider": provider_of(config.model_for(key)),
                "projects": agent.projects,
                "scoped_projects": [
                    project.identifier
                    for project in config.projects_for_agent(agent)
                ],
            }
            for key, agent in config.agents.items()
        },
        "runners": {
            key: {
                "argv": runner.argv,
                "cwd": runner.cwd,
                "stdin_file": runner.stdin_file,
                "inject_standard": runner.inject_standard,
            }
            for key, runner in config.runners.items()
        },
    }


async def _resolve_against_api(
    config: WorkflowConfig, settings: EngineSettings
) -> OpenProjectResolution:
    if settings.op_base_url is None or settings.op_engine_token is None:
        raise ConfigError(
            "missing required environment variables: OP_BASE_URL, OP_ENGINE_TOKEN"
        )
    async with OpenProjectClient(
        settings.op_base_url,
        settings.op_engine_token.get_secret_value(),
    ) as client:
        return await resolve_workflow(client, config)


async def _audit_against_api(
    config: WorkflowConfig, settings: EngineSettings
) -> ProvisionReport:
    """Read-only provisioning audit (M8), using the admin-capable token."""
    if settings.op_base_url is None:
        raise ConfigError("missing required environment variables: OP_BASE_URL")
    token = provisioning_token(settings)
    async with OpenProjectClient(settings.op_base_url, token) as client:
        await require_admin(client)
        return await audit_workflow(
            client,
            config,
            environ=os.environ,
            base_url=settings.op_base_url,
        )


def _poll_once(*, dry_run: bool) -> int:
    try:
        settings = EngineSettings()
        config = _load_config(settings)
    except ConfigError as error:
        print(f"op-agent-engine: error: {error}", file=sys.stderr)
        return CONFIG_ERROR_EXIT
    try:
        settings.require_api_values()
    except ConfigError as error:
        print(f"op-agent-engine: error: {error}", file=sys.stderr)
        return CONFIG_ERROR_EXIT
    configure_logging(os.environ.get("LOG_LEVEL", "INFO"))
    try:
        return run_engine_once(config, settings, dry_run=dry_run)
    except (ConfigError, ValidationError) as error:
        print(f"op-agent-engine: error: {error}", file=sys.stderr)
        return CONFIG_ERROR_EXIT
    except OpenProjectError as error:
        print(f"op-agent-engine: error: {error}", file=sys.stderr)
        return CONFIG_ERROR_EXIT


def _run(*, dry_run: bool) -> int:
    try:
        settings = EngineSettings()
        config = _load_config(settings)
    except ConfigError as error:
        print(f"op-agent-engine: error: {error}", file=sys.stderr)
        return CONFIG_ERROR_EXIT
    try:
        settings.require_api_values()
    except ConfigError as error:
        print(f"op-agent-engine: error: {error}", file=sys.stderr)
        return CONFIG_ERROR_EXIT
    configure_logging(os.environ.get("LOG_LEVEL", "INFO"))
    try:
        return run_engine(config, settings, dry_run=dry_run)
    except (ConfigError, ValidationError) as error:
        print(f"op-agent-engine: error: {error}", file=sys.stderr)
        return CONFIG_ERROR_EXIT
    except OpenProjectError as error:
        print(f"op-agent-engine: error: {error}", file=sys.stderr)
        return CONFIG_ERROR_EXIT


def _agent_dir() -> Path:
    """The pi agent directory that a run uses (``PI_CODING_AGENT_DIR``/HOME)."""
    configured = os.environ.get("PI_CODING_AGENT_DIR", "").strip()
    return Path(configured) if configured else Path.home() / ".pi" / "agent"


def _check_config(*, as_json: bool, offline: bool) -> int:
    try:
        settings = EngineSettings()
        config = _load_config(settings)
    except ConfigError as error:
        print(f"op-agent-engine: error: {error}", file=sys.stderr)
        return CONFIG_ERROR_EXIT

    # pi reads its provider key from the environment, so a model whose provider
    # has no credentials would only fail inside the first run. A one-time
    # ``pi /login`` credential in the persistent agent directory (auth.json) is
    # also accepted, which is the only way to use a ChatGPT subscription.
    credential_level, credential_message = credential_status(
        settings.configured_models(),
        os.environ,
        stored_providers=stored_credential_providers(_agent_dir()),
    )
    if credential_level == "error":
        print(f"op-agent-engine: error: {credential_message}", file=sys.stderr)
        return CONFIG_ERROR_EXIT
    if credential_level == "warn":
        print(f"op-agent-engine: warning: {credential_message}", file=sys.stderr)

    resolution: OpenProjectResolution | None = None
    provision_report: ProvisionReport | None = None
    if not offline:
        try:
            settings.require_api_values()
            resolution = asyncio.run(_resolve_against_api(config, settings))
            if config.openproject.provisioning.enabled:
                provision_report = asyncio.run(
                    _audit_against_api(config, settings)
                )
        except (ConfigError, ValidationError) as error:
            print(f"op-agent-engine: error: {error}", file=sys.stderr)
            return CONFIG_ERROR_EXIT
        except OpenProjectError as error:
            print(f"op-agent-engine: error: {error}", file=sys.stderr)
            return CONFIG_ERROR_EXIT

    if provision_report is not None and provision_report.tokens.mismatches:
        for status in provision_report.tokens.mismatches:
            print(
                f"op-agent-engine: error: agent token/login mismatch: "
                f"{status.message}",
                file=sys.stderr,
            )
        return CONFIG_ERROR_EXIT

    if as_json:
        payload = _config_summary(config)
        payload["resolved"] = resolution.as_dict() if resolution is not None else None
        payload["provisioning"] = (
            provision_report.as_dict() if provision_report is not None else None
        )
        payload["provider_credentials"] = {
            "level": credential_level,
            "message": credential_message,
        }
        print(json.dumps(payload, indent=2))
    else:
        print("Configuration OK (environment)")
        for line in config.summary():
            print(line)
        print(f"model credentials: {credential_message}")
        if resolution is None:
            print("OpenProject resolution skipped (--offline).")
        else:
            print("Resolved OpenProject IDs:")
            for line in resolution.summary():
                print(f"  {line}")
        if provision_report is not None:
            print("Provisioning audit:")
            for identifier, agent in config.agents.items():
                scoped = ", ".join(
                    project.identifier
                    for project in config.projects_for_agent(agent)
                )
                print(f"  {identifier}: projects={','.join(agent.projects)} -> {scoped}")
            for line in provision_report.summary():
                print(line)
    return OK_EXIT if (provision_report is None or provision_report.ok) else CONFIG_ERROR_EXIT


def _run_line(run: Run) -> str:
    return (
        f"run {run.id}: wp {run.work_package_id} agent={run.agent} "
        f"kind={run.kind} state={run.state} outcome={run.outcome or '-'} "
        f"started={run.started_at} finished={run.finished_at or '-'}"
    )


def _dashboard(
    *,
    host: str | None,
    port: int | None,
    limit: int,
    refresh_seconds: int,
) -> int:
    """Serve the read-only dashboard (no OpenProject token required)."""
    try:
        settings = EngineSettings()
        config = _load_config(settings)
    except ConfigError as error:
        print(f"op-agent-engine: error: {error}", file=sys.stderr)
        return CONFIG_ERROR_EXIT
    if not 1 <= limit <= MAX_LIMIT:
        print(
            f"op-agent-engine: error: --limit must be between 1 and {MAX_LIMIT}",
            file=sys.stderr,
        )
        return CONFIG_ERROR_EXIT
    if refresh_seconds < 1:
        print("op-agent-engine: error: --refresh must be at least 1", file=sys.stderr)
        return CONFIG_ERROR_EXIT
    bind_port = settings.dashboard_port if port is None else port
    if not 1 <= bind_port <= 65535:
        print("op-agent-engine: error: port must be between 1 and 65535", file=sys.stderr)
        return CONFIG_ERROR_EXIT
    configure_logging(os.environ.get("LOG_LEVEL", "INFO"))
    return serve_dashboard(
        config,
        settings,
        host=host,
        port=bind_port,
        limit=limit,
        refresh_seconds=refresh_seconds,
    )


def _status(*, as_json: bool) -> int:
    """Print heartbeat age, active runs and the last 10 finished runs."""
    settings = EngineSettings()
    data_dir = settings.data_dir
    heartbeat_raw, heartbeat_age = read_heartbeat(settings)
    paused = settings.engine_paused or (data_dir / PAUSE_FILE_NAME).exists()

    active: list[Run] = []
    finished: list[Run] = []
    store_path = data_dir / STORE_FILE_NAME
    if store_path.exists():
        store = Store(store_path)
        try:
            active = store.active_runs()
            finished = store.finished_runs(10)
        finally:
            store.close()

    if as_json:
        payload = {
            "data_dir": str(data_dir),
            "heartbeat": heartbeat_raw,
            "heartbeat_age_seconds": (
                round(heartbeat_age, 1) if heartbeat_age is not None else None
            ),
            "paused": paused,
            "active_runs": [
                {
                    "id": run.id,
                    "work_package_id": run.work_package_id,
                    "agent": run.agent,
                    "kind": run.kind,
                    "branch": run.branch,
                    "started_at": run.started_at,
                }
                for run in active
            ],
            "finished_runs": [
                {
                    "id": run.id,
                    "work_package_id": run.work_package_id,
                    "agent": run.agent,
                    "kind": run.kind,
                    "state": run.state,
                    "outcome": run.outcome,
                    "started_at": run.started_at,
                    "finished_at": run.finished_at,
                }
                for run in finished
            ],
        }
        print(json.dumps(payload, indent=2))
        return OK_EXIT

    if heartbeat_raw is None:
        print(f"heartbeat: missing ({data_dir / HEARTBEAT_FILE_NAME})")
    elif heartbeat_age is None:
        print(f"heartbeat: {heartbeat_raw}")
    else:
        print(f"heartbeat: {heartbeat_age:.1f}s ago ({heartbeat_raw})")
    print(f"paused: {'yes' if paused else 'no'}")
    print(f"active runs: {len(active)}")
    for run in active:
        print(f"  {_run_line(run)}")
    print(f"last {len(finished)} finished run(s) (max 10):")
    if not finished:
        print("  (none)")
    for run in finished:
        print(f"  {_run_line(run)}")
    return OK_EXIT


async def _provision_async(
    config: WorkflowConfig, settings: EngineSettings, *, apply: bool
) -> ProvisionReport:
    assert settings.op_base_url is not None
    token = provisioning_token(settings)
    async with OpenProjectClient(settings.op_base_url, token) as client:
        await require_admin(client)
        return await provision_workflow(
            client,
            config,
            apply=apply,
            environ=os.environ,
            base_url=settings.op_base_url,
        )


def _provision(*, apply: bool, as_json: bool, passwords: bool = False) -> int:
    try:
        settings = EngineSettings()
        config = _load_config(settings)
        if not settings.op_base_url:
            raise ConfigError("missing required environment variables: OP_BASE_URL")
        provisioning_token(settings)
    except (ConfigError, ProvisioningError) as error:
        print(f"op-agent-engine: error: {error}", file=sys.stderr)
        return CONFIG_ERROR_EXIT
    # Machine-readable output must keep stdout clean: route log records (for
    # example httpx's INFO request lines) to stderr instead.
    configure_logging(
        os.environ.get("LOG_LEVEL", "INFO"),
        stream=sys.stderr if (as_json or passwords) else None,
    )
    try:
        report = asyncio.run(_provision_async(config, settings, apply=apply))
    except (ConfigError, ValidationError, OpenProjectError) as error:
        print(f"op-agent-engine: error: {error}", file=sys.stderr)
        return CONFIG_ERROR_EXIT

    if passwords:
        for login, password in report.generated_passwords.items():
            print(f"{login}\t{password}")
    elif as_json:
        print(json.dumps(report.as_dict(), indent=2))
    else:
        for line in report.summary():
            print(line)
        if report.tokens.missing:
            print(
                "Missing agent tokens (create them in the OpenProject UI): "
                + ", ".join(missing_tokens(report.tokens))
            )
    return OK_EXIT if report.ok else CONFIG_ERROR_EXIT


def _active_worktree_branches(store_path: Path) -> frozenset[str]:
    """Branches with a running row; never remove their worktrees."""
    if not store_path.exists():
        return frozenset()
    try:
        store = Store(store_path, read_only=True)
    except (OSError, ValueError):
        return frozenset()
    try:
        return frozenset(run.branch for run in store.active_runs())
    except Exception:
        return frozenset()
    finally:
        store.close()


def _cleanup(*, days: int | None, as_json: bool, dry_run: bool) -> int:
    """Retention job: remove old run directories and stale worktrees."""
    try:
        settings = EngineSettings()
    except ValidationError as error:
        print(f"op-agent-engine: error: {error}", file=sys.stderr)
        return CONFIG_ERROR_EXIT
    retention_days = settings.run_retention_days if days is None else days
    if retention_days < 0:
        print(
            "op-agent-engine: error: --days must not be negative",
            file=sys.stderr,
        )
        return CONFIG_ERROR_EXIT

    report: CleanupReport = cleanup_run_dirs(
        settings.data_dir / RUNS_DIR_NAME,
        older_than_days=retention_days,
        dry_run=dry_run,
    )
    worktrees: WorktreeCleanupReport | None = None
    if settings.workspace_root is not None:
        worktrees = asyncio.run(
            cleanup_stale_worktrees(
                settings.workspace_root,
                older_than_days=retention_days,
                dry_run=dry_run,
                active_branches=_active_worktree_branches(
                    settings.data_dir / STORE_FILE_NAME
                ),
            )
        )

    if as_json:
        payload = report.as_dict()
        payload["worktrees"] = (
            worktrees.as_dict() if worktrees is not None else None
        )
        print(json.dumps(payload, indent=2))
    else:
        for line in report.summary():
            print(line)
        if worktrees is None:
            print("worktree root: not configured; skipped worktree cleanup")
        else:
            for line in worktrees.summary():
                print(line)
    errors = list(report.errors)
    if worktrees is not None:
        errors.extend(worktrees.errors)
    return OK_EXIT if not errors else CONFIG_ERROR_EXIT


async def _verify_token_async(
    base_url: str, token: str, login: str, *, admin: bool
) -> str:
    async with OpenProjectClient(base_url, token) as client:
        payload = await client.get_json("/api/v3/users/me")
    actual = payload.get("login") if isinstance(payload, dict) else None
    if actual != login:
        raise OpenProjectError(
            f"token belongs to {actual!r}, expected {login!r}"
        )
    if admin and payload.get("admin") is not True:
        raise OpenProjectError(f"{login} is not an OpenProject administrator")
    return login


def _verify_token(login: str, *, admin: bool) -> int:
    """Check ``OP_VERIFY_TOKEN`` through ``/api/v3/users/me``."""
    settings = EngineSettings()
    if not settings.op_base_url:
        print(
            "op-agent-engine: error: missing required environment variables: OP_BASE_URL",
            file=sys.stderr,
        )
        return CONFIG_ERROR_EXIT
    token = (os.environ.get("OP_VERIFY_TOKEN") or "").strip()
    if not token:
        print("op-agent-engine: error: OP_VERIFY_TOKEN is empty", file=sys.stderr)
        return CONFIG_ERROR_EXIT
    try:
        asyncio.run(_verify_token_async(settings.op_base_url, token, login, admin=admin))
    except OpenProjectError as error:
        print(f"op-agent-engine: error: {error}", file=sys.stderr)
        return CONFIG_ERROR_EXIT
    print(f"ok: {login}")
    return OK_EXIT


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    # Every subcommand configures logging from ``LOG_LEVEL``. Propagate the
    # ``--log-level`` flag so it is not silently ignored (scripts rely on
    # ``--log-level CRITICAL`` to keep ``--json`` output clean).
    log_level = getattr(args, "log_level", None)
    if log_level:
        os.environ["LOG_LEVEL"] = str(log_level)

    if args.command == "verify-token":
        return _verify_token(args.login, admin=bool(args.admin))

    if args.command == "dashboard":
        return _dashboard(
            host=args.host,
            port=args.port,
            limit=args.limit,
            refresh_seconds=args.refresh,
        )

    if args.command == "status":
        return _status(as_json=args.json)

    if args.command == "cleanup":
        return _cleanup(
            days=args.days, as_json=args.json, dry_run=bool(args.dry_run)
        )

    if args.command == "check-config":
        return _check_config(as_json=args.json, offline=args.offline)

    if args.command == "provision":
        return _provision(
            apply=args.apply, as_json=args.json, passwords=bool(args.passwords)
        )

    if args.command in ("once", "dry-run"):
        return _poll_once(dry_run=(args.command == "dry-run") or bool(args.dry_run))

    return _run(dry_run=bool(args.dry_run))


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
