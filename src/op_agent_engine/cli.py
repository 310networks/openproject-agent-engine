"""Command line interface.

Subcommands follow the plan: ``run``, ``once``, ``check-config``, ``status``
and ``dry-run``. ``check-config`` validates the workflow file and resolves all
OpenProject IDs. ``once`` and ``dry-run`` perform a single poll and log the
dispatch plan (M2 is read-only: nothing is claimed or executed). ``run`` and
``status`` parse arguments and fail fast with a clear message until their
engine behaviour lands.
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
    DEFAULT_ENGINE_CONFIG,
    ConfigError,
    EngineSettings,
    WorkflowConfig,
)
from op_agent_engine.logging import configure_logging, get_logger
from op_agent_engine.openproject.client import OpenProjectClient, OpenProjectError
from op_agent_engine.openproject.resolve import OpenProjectResolution, resolve_workflow
from op_agent_engine.poller import Poller

NOT_IMPLEMENTED_EXIT = 2
CONFIG_ERROR_EXIT = 1
OK_EXIT = 0

_NOT_IMPLEMENTED_IN = {
    "run": "M1+ (engine loop)",
    "status": "M7+ (status command)",
}


def default_config_path() -> Path:
    return Path(os.environ.get("ENGINE_CONFIG", DEFAULT_ENGINE_CONFIG)).expanduser()


def _add_common_options(
    parser: argparse.ArgumentParser, *, suppress_defaults: bool = False
) -> None:
    """Options accepted both before and after the subcommand.

    The systemd unit uses ``run --config PATH``; users also type
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

    subparsers.add_parser(
        "status",
        help="print heartbeat age, active runs and recent runs",
        parents=[_subcommand_options()],
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
        "agents": {
            key: {
                "name": agent.name,
                "kind": agent.kind,
                "op_login": agent.op_login,
                "workspace": agent.workspace,
                "runner": agent.runner,
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


async def _poll_once_async(
    config: WorkflowConfig, settings: EngineSettings, *, dry_run: bool
) -> int:
    log = get_logger("op_agent_engine.cli")
    assert settings.op_base_url is not None
    assert settings.op_engine_token is not None
    async with OpenProjectClient(
        settings.op_base_url,
        settings.op_engine_token.get_secret_value(),
    ) as client:
        resolution = await resolve_workflow(client, config)
        poller = Poller(
            client,
            config,
            resolution,
            max_concurrent_runs=settings.max_concurrent_runs,
        )
        plan = await poller.poll()
    log.info(
        "%s: %d item(s), %d skipped",
        "dry-run complete" if dry_run else "poll complete",
        len(plan.items),
        len(plan.skipped),
        extra={"items": list(plan.work_package_ids), "dry_run": dry_run},
    )
    return OK_EXIT


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
        return asyncio.run(_poll_once_async(config, settings, dry_run=dry_run))
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
    if not offline:
        try:
            settings = EngineSettings()
            settings.require_api_values()
            resolution = asyncio.run(_resolve_against_api(config, settings))
        except (ConfigError, ValidationError) as error:
            print(f"op-agent-engine: error: {error}", file=sys.stderr)
            return CONFIG_ERROR_EXIT
        except OpenProjectError as error:
            print(f"op-agent-engine: error: {error}", file=sys.stderr)
            return CONFIG_ERROR_EXIT

    if as_json:
        payload = _config_summary(config, path)
        payload["resolved"] = resolution.as_dict() if resolution is not None else None
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
    return OK_EXIT


def _not_implemented(command: str) -> int:
    print(
        f"op-agent-engine: '{command}' is not implemented yet "
        f"(target: {_NOT_IMPLEMENTED_IN[command]})",
        file=sys.stderr,
    )
    return NOT_IMPLEMENTED_EXIT


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    config_path = (args.config or default_config_path()).expanduser()

    if args.command == "check-config":
        return _check_config(config_path, as_json=args.json, offline=args.offline)

    if args.command in ("once", "dry-run"):
        return _poll_once(
            config_path, dry_run=(args.command == "dry-run") or bool(args.dry_run)
        )

    # The remaining commands need a valid configuration before they can do
    # anything, even while their engine behaviour is a stub.
    try:
        _load_config(config_path)
    except ConfigError as error:
        print(f"op-agent-engine: error: {error}", file=sys.stderr)
        return CONFIG_ERROR_EXIT

    configure_logging(args.log_level or os.environ.get("LOG_LEVEL", "INFO"))
    log = get_logger("op_agent_engine.cli")
    log.info(
        "command not implemented",
        extra={"command": args.command, "config": str(config_path)},
    )
    return _not_implemented(args.command)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
