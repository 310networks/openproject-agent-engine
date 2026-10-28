"""Configuration loading and validation.

Two configuration sources exist:

* ``workflow.yaml`` (this module's :class:`WorkflowConfig`) describes statuses,
  projects, agents and runners.
* Environment variables (see :class:`EngineSettings`) supply secrets and
  runtime limits.

``${VAR}`` placeholders in ``workflow.yaml`` are resolved from the process
environment before validation. A missing variable is a startup error.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    ValidationError,
    field_validator,
    model_validator,
)
from pydantic_settings import BaseSettings, SettingsConfigDict

# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class ConfigError(Exception):
    """Raised when configuration is missing, unreadable or invalid.

    The message is human-readable and may contain several lines, one per
    problem, so ``check-config`` can print it directly.
    """


# ---------------------------------------------------------------------------
# Placeholder handling
# ---------------------------------------------------------------------------

_ENV_REFERENCE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
_RUNNER_PLACEHOLDER = re.compile(r"\{([a-z_]+)\}")

ALLOWED_RUNNER_PLACEHOLDERS = frozenset(
    {
        "prompt_file",
        "repo_dir",
        "workspace_dir",
        "run_dir",
        "model",
        "branch",
        "work_package_id",
    }
)


def _interpolate(
    value: Any,
    environ: Mapping[str, str],
    path: str,
    missing: list[tuple[str, str]],
) -> Any:
    """Recursively replace ``${VAR}`` references in strings."""
    if isinstance(value, str):

        def replace(match: re.Match[str]) -> str:
            name = match.group(1)
            if name not in environ:
                missing.append((path, name))
                return ""
            return environ[name]

        return _ENV_REFERENCE.sub(replace, value)
    if isinstance(value, dict):
        return {
            key: _interpolate(item, environ, f"{path}.{key}" if path else str(key), missing)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [
            _interpolate(item, environ, f"{path}[{index}]", missing)
            for index, item in enumerate(value)
        ]
    return value


def _format_validation_error(error: ValidationError) -> str:
    lines = ["invalid configuration:"]
    for detail in error.errors(include_url=False):
        location = ".".join(str(part) for part in detail["loc"]) or "<root>"
        lines.append(f"  - {location}: {detail['msg']}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Workflow models
# ---------------------------------------------------------------------------


class StatusNames(BaseModel):
    """OpenProject status display names, keyed by engine role."""

    model_config = ConfigDict(extra="forbid")

    new: str = "New"
    in_progress: str = "In Progress"
    ready_for_review: str = "Ready for Review"
    in_testing: str = "In Testing"
    on_hold: str = "On Hold"
    rejected: str = "Rejected"
    closed: str = "Closed"

    @field_validator("*")
    @classmethod
    def _non_empty(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("status name must not be empty")
        return value

    @model_validator(mode="after")
    def _unique_names(self) -> StatusNames:
        names = [
            self.new,
            self.in_progress,
            self.ready_for_review,
            self.in_testing,
            self.on_hold,
            self.rejected,
            self.closed,
        ]
        duplicates = sorted({name for name in names if names.count(name) > 1})
        if duplicates:
            raise ValueError(f"status names must be unique; duplicated: {', '.join(duplicates)}")
        return self

    def by_key(self) -> dict[str, str]:
        return {
            "new": self.new,
            "in_progress": self.in_progress,
            "ready_for_review": self.ready_for_review,
            "in_testing": self.in_testing,
            "on_hold": self.on_hold,
            "rejected": self.rejected,
            "closed": self.closed,
        }


class OpenProjectConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    statuses: StatusNames = Field(default_factory=StatusNames)
    agent_field: str = "AGENT"
    engine_user: str = "agent-engine"
    dispatch_priority: list[str] = Field(
        default_factory=lambda: ["ready_for_review", "rejected", "new"]
    )
    eligible_types: list[str] = Field(default_factory=lambda: ["Task", "Bug", "Feature"])
    skip_work_packages_with_children: bool = True
    skip_if_assigned_to_human: bool = True

    @field_validator("agent_field", "engine_user")
    @classmethod
    def _non_empty(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be empty")
        return value

    @field_validator("eligible_types")
    @classmethod
    def _eligible_types(cls, value: list[str]) -> list[str]:
        if not value:
            raise ValueError("at least one eligible type is required")
        if any(not item.strip() for item in value):
            raise ValueError("eligible types must not be empty")
        return value

    @model_validator(mode="after")
    def _dispatch_priority(self) -> OpenProjectConfig:
        if not self.dispatch_priority:
            raise ValueError("dispatch_priority must not be empty")
        known = set(self.statuses.by_key())
        unknown = [item for item in self.dispatch_priority if item not in known]
        if unknown:
            raise ValueError(
                "dispatch_priority entries must be status keys "
                f"({', '.join(sorted(known))}); unknown: {', '.join(unknown)}"
            )
        if len(set(self.dispatch_priority)) != len(self.dispatch_priority):
            raise ValueError("dispatch_priority must not contain duplicates")
        return self


class RepoConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    github: str
    dir: str
    default_branch: str = "main"

    @field_validator("github")
    @classmethod
    def _github_repo(cls, value: str) -> str:
        if not re.fullmatch(r"[^/\s]+/[^/\s]+", value):
            raise ValueError("must be a GitHub repository in 'owner/name' form")
        return value

    @field_validator("dir")
    @classmethod
    def _clone_dir(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("clone directory name must not be empty")
        if "/" in value or "\\" in value or value in {".", ".."}:
            raise ValueError("clone directory must be a plain directory name")
        return value

    @field_validator("default_branch")
    @classmethod
    def _default_branch(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("default branch must not be empty")
        return value


class ProjectConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    identifier: str
    repo: RepoConfig
    enabled: bool = True

    @field_validator("identifier")
    @classmethod
    def _identifier(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("project identifier must not be empty")
        return value


class AgentConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    kind: Literal["implementer", "reviewer"]
    op_login: str
    workspace: str
    runner: str
    model: str | None = None
    timeout_seconds: int | None = Field(default=None, gt=0)
    env: dict[str, str] = Field(default_factory=dict)

    @field_validator("name", "op_login")
    @classmethod
    def _non_empty(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be empty")
        return value

    @field_validator("workspace")
    @classmethod
    def _workspace(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("workspace must not be empty")
        if "/" in value or "\\" in value or value in {".", ".."}:
            raise ValueError("workspace must be a single directory name under WORKSPACE_ROOT")
        return value

    @field_validator("runner")
    @classmethod
    def _runner_name(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("runner must not be empty")
        return value


class RunnerConfig(BaseModel):
    """A generic command template. The engine knows nothing about the agent itself."""

    model_config = ConfigDict(extra="forbid")

    argv: list[str]
    cwd: str = "{repo_dir}"
    env: dict[str, str] = Field(default_factory=dict)
    stdin_file: str | None = None
    inject_standard: bool = False

    @field_validator("argv")
    @classmethod
    def _argv(cls, value: list[str]) -> list[str]:
        if not value or any(not part.strip() for part in value):
            raise ValueError("argv must contain at least one non-empty argument")
        return value

    @field_validator("cwd")
    @classmethod
    def _cwd(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("cwd must not be empty")
        return value

    @model_validator(mode="after")
    def _known_placeholders(self) -> RunnerConfig:
        for field_name in ("cwd", "stdin_file"):
            value = getattr(self, field_name)
            if value is not None:
                _check_placeholders(value, f"runners.{field_name}")
        for index, argument in enumerate(self.argv):
            _check_placeholders(argument, f"runners.argv[{index}]")
        return self


def _check_placeholders(value: str, location: str) -> None:
    for match in _RUNNER_PLACEHOLDER.finditer(value):
        name = match.group(1)
        if name not in ALLOWED_RUNNER_PLACEHOLDERS:
            allowed = ", ".join(sorted(ALLOWED_RUNNER_PLACEHOLDERS))
            raise ValueError(
                f"{location}: unknown placeholder '{{{name}}}' "
                f"(allowed: {allowed})"
            )


class WorkflowConfig(BaseModel):
    """The parsed, validated contents of ``workflow.yaml``."""

    model_config = ConfigDict(extra="forbid")

    openproject: OpenProjectConfig = Field(default_factory=OpenProjectConfig)
    projects: list[ProjectConfig]
    agents: dict[str, AgentConfig]
    runners: dict[str, RunnerConfig]

    @field_validator("agents", "runners")
    @classmethod
    def _non_empty_map(cls, value: dict[str, Any]) -> dict[str, Any]:
        if not value:
            raise ValueError("must define at least one entry")
        for key in value:
            if not str(key).strip():
                raise ValueError("keys must not be empty")
        return value

    @model_validator(mode="after")
    def _cross_references(self) -> WorkflowConfig:
        if not self.projects:
            raise ValueError("at least one project must be configured")
        if not any(project.enabled for project in self.projects):
            raise ValueError("at least one project must be enabled")

        identifiers = [project.identifier for project in self.projects]
        if len(set(identifiers)) != len(identifiers):
            raise ValueError("project identifiers must be unique")

        repo_dirs = [project.repo.dir for project in self.projects]
        if len(set(repo_dirs)) != len(repo_dirs):
            raise ValueError("repo.dir values must be unique across projects")

        unknown_runners = sorted(
            {agent.runner for agent in self.agents.values()} - set(self.runners)
        )
        if unknown_runners:
            raise ValueError(
                f"agents reference undefined runners: {', '.join(unknown_runners)}"
            )

        for field_name in ("name", "op_login", "workspace"):
            values = [getattr(agent, field_name) for agent in self.agents.values()]
            duplicates = sorted({value for value in values if values.count(value) > 1})
            if duplicates:
                raise ValueError(
                    f"agent {field_name} values must be unique; duplicated: "
                    f"{', '.join(duplicates)}"
                )

        reviewers = [key for key, agent in self.agents.items() if agent.kind == "reviewer"]
        if len(reviewers) != 1:
            raise ValueError(
                "exactly one agent must have kind 'reviewer'; "
                f"found {len(reviewers)}"
                + (f" ({', '.join(reviewers)})" if reviewers else "")
            )
        return self

    # -- helpers ---------------------------------------------------------

    @property
    def reviewer(self) -> AgentConfig:
        return next(agent for agent in self.agents.values() if agent.kind == "reviewer")

    @property
    def implementers(self) -> list[AgentConfig]:
        return [agent for agent in self.agents.values() if agent.kind == "implementer"]

    def enabled_projects(self) -> list[ProjectConfig]:
        return [project for project in self.projects if project.enabled]

    def summary(self) -> list[str]:
        """Human-readable summary used by ``check-config``."""
        statuses = ", ".join(
            f"{key}={name}" for key, name in self.openproject.statuses.by_key().items()
        )
        lines = [
            f"statuses: {statuses}",
            f"agent field: {self.openproject.agent_field}",
            f"engine user: {self.openproject.engine_user}",
            f"dispatch priority: {', '.join(self.openproject.dispatch_priority)}",
            f"eligible types: {', '.join(self.openproject.eligible_types)}",
            f"projects: {len(self.projects)} ({len(self.enabled_projects())} enabled)",
        ]
        for project in self.projects:
            state = "enabled" if project.enabled else "disabled"
            lines.append(
                f"  - {project.identifier}: {project.repo.github} "
                f"-> {project.repo.dir} [{state}]"
            )
        lines.append(f"agents: {len(self.agents)}")
        for key, agent in self.agents.items():
            lines.append(
                f"  - {key}: {agent.name} ({agent.kind}), "
                f"op_login={agent.op_login}, workspace={agent.workspace}, "
                f"runner={agent.runner}"
            )
        lines.append(f"runners: {len(self.runners)}")
        for key, runner in self.runners.items():
            lines.append(f"  - {key}: {' '.join(runner.argv)}")
        return lines

    # -- loading ---------------------------------------------------------

    @classmethod
    def from_mapping(
        cls,
        raw: Any,
        *,
        environ: Mapping[str, str] | None = None,
        source: str = "<mapping>",
    ) -> WorkflowConfig:
        environ = os.environ if environ is None else environ
        if not isinstance(raw, dict):
            raise ConfigError(
                f"{source}: invalid configuration: top level must be a mapping"
            )

        missing: list[tuple[str, str]] = []
        resolved = _interpolate(raw, environ, "", missing)
        if missing:
            lines = [f"{source}: unresolved environment variables:"]
            for path, name in missing:
                location = path or "<root>"
                lines.append(f"  - {location}: ${name} is not set")
            raise ConfigError("\n".join(lines))

        try:
            return cls.model_validate(resolved)
        except ValidationError as error:
            raise ConfigError(f"{source}: {_format_validation_error(error)}") from error

    @classmethod
    def load(
        cls,
        path: str | Path,
        *,
        environ: Mapping[str, str] | None = None,
    ) -> WorkflowConfig:
        config_path = Path(path).expanduser()
        try:
            text = config_path.read_text(encoding="utf-8")
        except FileNotFoundError as error:
            raise ConfigError(f"configuration file not found: {config_path}") from error
        except OSError as error:
            raise ConfigError(f"cannot read configuration file {config_path}: {error}") from error

        try:
            raw = yaml.safe_load(text)
        except yaml.YAMLError as error:
            raise ConfigError(f"{config_path}: invalid YAML: {error}") from error

        if raw is None:
            raise ConfigError(f"{config_path}: configuration file is empty")

        return cls.from_mapping(raw, environ=environ, source=str(config_path))


# ---------------------------------------------------------------------------
# Process environment (secrets, limits, paths)
# ---------------------------------------------------------------------------

DEFAULT_ENGINE_CONFIG = "~/.config/op-agent-engine/workflow.yaml"
DEFAULT_DATA_DIR = "~/.local/share/op-agent-engine"


class EngineSettings(BaseSettings):
    """Runtime settings supplied through the systemd ``EnvironmentFile``."""

    model_config = SettingsConfigDict(
        env_file=None,
        extra="ignore",
        case_sensitive=False,
    )

    op_base_url: str | None = None
    op_engine_token: SecretStr | None = None
    poll_interval_seconds: int = Field(default=60, gt=0)
    max_concurrent_runs: int = Field(default=1, gt=0)
    max_rejections: int = Field(default=3, ge=0)
    max_crash_retries: int = Field(default=2, ge=0)
    run_timeout_seconds: int = Field(default=3600, gt=0)
    stop_grace_seconds: int = Field(default=60, ge=0)
    engine_config: Path = Field(default_factory=lambda: Path(DEFAULT_ENGINE_CONFIG).expanduser())
    data_dir: Path = Field(default_factory=lambda: Path(DEFAULT_DATA_DIR).expanduser())
    workspace_root: Path | None = None
    gh_token: SecretStr | None = None
    engine_paused: bool = False
    log_level: str = "INFO"

    def missing_api_values(self) -> list[str]:
        """Required variables for talking to OpenProject."""
        missing: list[str] = []
        if not self.op_base_url:
            missing.append("OP_BASE_URL")
        if not self.op_engine_token or not self.op_engine_token.get_secret_value():
            missing.append("OP_ENGINE_TOKEN")
        return missing

    def require_api_values(self) -> None:
        missing = self.missing_api_values()
        if missing:
            raise ConfigError(
                "missing required environment variables: " + ", ".join(missing)
            )

    def missing_runtime_values(self) -> list[str]:
        """Names of required variables that are absent or empty."""
        missing = self.missing_api_values()
        if self.workspace_root is None or not str(self.workspace_root):
            missing.append("WORKSPACE_ROOT")
        return missing

    def require_runtime_values(self) -> None:
        missing = self.missing_runtime_values()
        if missing:
            raise ConfigError(
                "missing required environment variables: " + ", ".join(missing)
            )
