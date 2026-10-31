"""Command line interface.

Subcommands follow the plan: ``run``, ``once``, ``check-config``, ``status``
and ``dry-run``. ``check-config`` validates the workflow file and resolves all
OpenProject IDs. ``once`` performs a single poll and claims what the slot
rules allow. ``dry-run`` performs a single poll and logs what would be
claimed without writing anything. ``run`` starts the engine loop (poll,
schedule, execute, sleep). ``status`` prints the heartbeat age, active runs
and the last finished runs from the local store (M7).
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
from op_agent_engine.account_setup import AccountSetupError, run_provision_users
from op_agent_engine.config import (
    DEFAULT_ENGINE_CONFIG,
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

CONFIG_ERROR_EXIT = 1
OK_EXIT = 0


def default_config_path() -> Path:
    return Path(os.environ.get("ENGINE_CONFIG", DEFAULT_ENGINE_CONFIG)).expanduser()


def _add_common_options(
    parser: argparse.ArgumentParser, *, suppress_defaults: bool = False
) -> None:
    """Options accepted both before and after the subcommand.

    The container command is ``run`` (``ENGINE_CONFIG`` names the file); users also type
    ``--config PATH check-config``. Defining the same destination on both
    parsers keeps both forms working. ``SUPPRESS`` on the subparser avoids
    overwriting a value parsed before the subcommand.
    """
    default: object = argparse.SUPPRESS if suppress_defaults else None
    parser.add_argument(
        "--config",
        type=Path,
        default=default,
        metavar="PATH",
        help=f"workflow configuration file (default: $ENGINE_CONFIG or {DEFAULT_ENGINE_CONFIG})",
    )
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

    provision_users = subparsers.add_parser(
        "provision-users",
        help=(
            "container install step: create the OpenProject accounts and "
            "collect one API token per account"
        ),
    )
    provision_users.add_argument(
        "--config-dir",
        type=Path,
        default=None,
        metavar="DIR",
        help="directory holding workflow.yaml and engine.env (default: the "
        "directory of $ENGINE_CONFIG, i.e. /config in the container)",
    )
    provision_users.add_argument(
        "--reprompt-tokens",
        action="store_true",
        help=(
            "ask for every API token again instead of reusing the valid "
            "tokens already stored in engine.env"
        ),
    )


    return parser


def _subcommand_options() -> argparse.ArgumentParser:
    parent = argparse.ArgumentParser(add_help=False)
    _add_common_options(parent, suppress_defaults=True)
    return parent


def _load_config(path: Path) -> WorkflowConfig:
    return WorkflowConfig.load(path)


def _config_summary(config: WorkflowConfig, path: Path) -> dict[str, object]:
    return {
        "path": str(path),
        "statuses": config.openproject.statuses.by_key(),
        "agent_field": config.openproject.agent_field,
        "engine_user": config.openproject.engine_user,
        "dispatch_priority": config.openproject.dispatch_priority,
        "eligible_types": config.openproject.eligible_types,
        "projects": [
            {
                "identifier": project.identifier,
                "github": project.repo.github,
                "dir": project.repo.dir,
                "default_branch": project.repo.default_branch,
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


def _poll_once(config_path: Path, *, dry_run: bool) -> int:
    try:
        config = _load_config(config_path)
    except ConfigError as error:
        print(f"op-agent-engine: error: {error}", file=sys.stderr)
        return CONFIG_ERROR_EXIT
    try:
        settings = EngineSettings()
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


def _run(config_path: Path, *, dry_run: bool) -> int:
    try:
        config = _load_config(config_path)
    except ConfigError as error:
        print(f"op-agent-engine: error: {error}", file=sys.stderr)
        return CONFIG_ERROR_EXIT
    try:
        settings = EngineSettings()
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


def _check_config(path: Path, *, as_json: bool, offline: bool) -> int:
    try:
        config = _load_config(path)
    except ConfigError as error:
        print(f"op-agent-engine: error: {error}", file=sys.stderr)
        return CONFIG_ERROR_EXIT

    resolution: OpenProjectResolution | None = None
    provision_report: ProvisionReport | None = None
    if not offline:
        try:
            settings = EngineSettings()
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
        payload = _config_summary(config, path)
        payload["resolved"] = resolution.as_dict() if resolution is not None else None
        payload["provisioning"] = (
            provision_report.as_dict() if provision_report is not None else None
        )
        print(json.dumps(payload, indent=2))
    else:
        print(f"Configuration OK: {path}")
        for line in config.summary():
            print(line)
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
    config_path: Path,
    *,
    host: str | None,
    port: int | None,
    limit: int,
    refresh_seconds: int,
) -> int:
    """Serve the read-only dashboard (no OpenProject token required)."""
    try:
        config = _load_config(config_path)
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
    settings = EngineSettings()
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


def _provision(path: Path, *, apply: bool, as_json: bool) -> int:
    try:
        config = _load_config(path)
    except ConfigError as error:
        print(f"op-agent-engine: error: {error}", file=sys.stderr)
        return CONFIG_ERROR_EXIT
    try:
        settings = EngineSettings()
        if not settings.op_base_url:
            raise ConfigError("missing required environment variables: OP_BASE_URL")
        provisioning_token(settings)
    except (ConfigError, ProvisioningError) as error:
        print(f"op-agent-engine: error: {error}", file=sys.stderr)
        return CONFIG_ERROR_EXIT
    # ``--json`` is machine-readable output, so keep stdout pure JSON: route
    # log records (for example httpx's INFO request lines) to stderr instead.
    configure_logging(
        os.environ.get("LOG_LEVEL", "INFO"),
        stream=sys.stderr if as_json else None,
    )
    try:
        report = asyncio.run(_provision_async(config, settings, apply=apply))
    except (ConfigError, ValidationError, OpenProjectError) as error:
        print(f"op-agent-engine: error: {error}", file=sys.stderr)
        return CONFIG_ERROR_EXIT

    if as_json:
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


def _cleanup(*, days: int | None, as_json: bool, dry_run: bool) -> int:
    """Log retention job: remove old ``DATA_DIR/runs/<runId>`` directories."""
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
    if as_json:
        print(json.dumps(report.as_dict(), indent=2))
    else:
        for line in report.summary():
            print(line)
    return OK_EXIT if not report.errors else CONFIG_ERROR_EXIT


def _provision_users(args: argparse.Namespace) -> int:
    config_dir = (
        Path(args.config_dir).expanduser()
        if args.config_dir is not None
        else default_config_path().expanduser().parent
    )
    try:
        return run_provision_users(
            config_dir=config_dir,
            reprompt_tokens=bool(args.reprompt_tokens),
        )
    except AccountSetupError as error:
        print(f"op-agent-engine: error: {error}", file=sys.stderr)
        return CONFIG_ERROR_EXIT


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    # Every subcommand configures logging from ``LOG_LEVEL``. Propagate the
    # ``--log-level`` flag so it is not silently ignored (scripts rely on
    # ``--log-level CRITICAL`` to keep ``--json`` output clean).
    log_level = getattr(args, "log_level", None)
    if log_level:
        os.environ["LOG_LEVEL"] = str(log_level)

    if args.command == "provision-users":
        return _provision_users(args)

    config_path = (getattr(args, "config", None) or default_config_path()).expanduser()
    if args.command == "dashboard":
        return _dashboard(
            config_path,
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
        return _check_config(config_path, as_json=args.json, offline=args.offline)

    if args.command == "provision":
        return _provision(config_path, apply=args.apply, as_json=args.json)

    if args.command in ("once", "dry-run"):
        return _poll_once(
            config_path, dry_run=(args.command == "dry-run") or bool(args.dry_run)
        )

    return _run(config_path, dry_run=bool(args.dry_run))


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
