"""Configuration loading and validation.

There is no configuration file. Everything comes from environment variables:

* :class:`EngineSettings` (pydantic-settings) reads the deployment and runtime
  variables.
* :meth:`WorkflowConfig.from_settings` turns those variables into the workflow
  model the engine uses. The agent team, the runners and the status names have
  defaults, so a deployment only supplies its OpenProject URL, project,
  repository and tokens.

``${VAR}`` placeholders are still resolved by :meth:`WorkflowConfig.from_mapping`
(used by tests and by the env builder), so a missing variable is an error.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Literal

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


class ProvisioningConfig(BaseModel):
    """Settings used only by ``provision`` and ``check-config`` (M8).

    Roles cannot be created through the OpenProject API (docs/VERIFICATION.md
    item 9), so the role names identify roles that an administrator creates
    manually. The email domain is used only when ``provision`` has to create a
    missing user; no notification is sent for memberships.
    """

    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    agent_role: str = "Agent Worker"
    # ``None`` is the default because the engine user is an administrator: it
    # already has every project permission, so provisioning neither needs an
    # engine role nor creates its membership. Set a role name only for a
    # least-privilege, non-administrator engine.
    engine_role: str | None = None
    email_domain: str = "agent-engine.local"

    @field_validator("agent_role", "email_domain")
    @classmethod
    def _non_empty(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be empty")
        return value

    @field_validator("engine_role")
    @classmethod
    def _optional_role(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        return stripped or None


class OpenProjectConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    statuses: StatusNames = Field(default_factory=StatusNames)
    agent_field: str = "AGENT"
    engine_user: str = "agent-engine"
    provisioning: ProvisioningConfig = Field(default_factory=ProvisioningConfig)
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
    projects: list[str] = Field(default_factory=lambda: ["*"])
    env: dict[str, str] = Field(default_factory=dict)

    @field_validator("projects")
    @classmethod
    def _projects(cls, value: list[str]) -> list[str]:
        if not value:
            raise ValueError("projects must not be empty; use ['*'] for every enabled project")
        if any(not item.strip() for item in value):
            raise ValueError("project identifiers must not be empty")
        if len(set(value)) != len(value):
            raise ValueError("project identifiers must not contain duplicates")
        if "*" in value and len(value) > 1:
            raise ValueError("'*' cannot be combined with explicit project identifiers")
        return value

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

    @field_validator("model")
    @classmethod
    def _model(cls, value: str | None) -> str | None:
        return _model_name(value, "model")


class RunnerConfig(BaseModel):
    """A generic command template. The engine knows nothing about the agent itself."""

    model_config = ConfigDict(extra="forbid")

    argv: list[str]
    cwd: str = "{repo_dir}"
    env: dict[str, str] = Field(default_factory=dict)
    stdin_file: str | None = None
    inject_standard: bool = False
    #: Default model for the agents using this runner; an agent's own
    #: ``model`` wins. Substituted into argv as ``{model}`` and omitted from
    #: argv entirely when neither is set.
    model: str | None = None

    @field_validator("model")
    @classmethod
    def _model(cls, value: str | None) -> str | None:
        return _model_name(value, "model")

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


def _model_name(value: str | None, location: str) -> str | None:
    """Validate a model identifier.

    The value is substituted into a single argv element (``--model {model}``),
    so it must stay one token: no whitespace, no empty string.
    """
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{location} must be a string")
    name = value.strip()
    if not name:
        return None
    if any(character.isspace() for character in name):
        raise ValueError(
            f"{location} must be a single token such as 'anthropic/claude-sonnet-4'"
        )
    return name


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
    """The parsed, validated contents of the configuration."""

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

        declared = set(identifiers)
        for key, agent in self.agents.items():
            if "*" in agent.projects:
                continue
            unknown = sorted(set(agent.projects) - declared)
            if unknown:
                raise ValueError(
                    f"agent '{key}' projects reference identifier(s) not declared in "
                    f"projects: {', '.join(unknown)}"
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

    def projects_for_agent(self, agent: AgentConfig) -> list[ProjectConfig]:
        """Enabled projects inside an agent's configured scope.

        ``['*']`` expands to every enabled project; an explicit list never
        reaches a project that is not declared. Disabled projects are excluded
        because they are not monitored.
        """
        if "*" in agent.projects:
            return self.enabled_projects()
        wanted = set(agent.projects)
        return [
            project
            for project in self.projects
            if project.enabled and project.identifier in wanted
        ]

    def model_for(self, agent_key: str) -> str | None:
        """Return the model used by ``agent_key``.

        An agent's own ``model`` wins over the default declared on its runner.
        ``None`` means the agent CLI's own default is used and ``{model}`` (and
        its flag) is dropped from the runner argv.
        """
        agent = self.agents[agent_key]
        return agent.model or self.runners[agent.runner].model

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
            model = agent.model or self.runners[agent.runner].model
            model_text = f", model={model}" if model else ""
            lines.append(
                f"  - {key}: {agent.name} ({agent.kind}), "
                f"op_login={agent.op_login}, workspace={agent.workspace}, "
                f"runner={agent.runner}, projects={','.join(agent.projects)}"
                f"{model_text}"
            )
        provisioning = self.openproject.provisioning
        engine_role = (
            repr(provisioning.engine_role)
            if provisioning.engine_role
            else "none (administrator engine user)"
        )
        lines.append(
            f"provisioning: {'enabled' if provisioning.enabled else 'disabled'}, "
            f"agent role={provisioning.agent_role!r}, "
            f"engine role={engine_role}"
        )
        lines.append(f"runners: {len(self.runners)}")
        for key, runner in self.runners.items():
            model_text = f" [model={runner.model}]" if runner.model else ""
            lines.append(f"  - {key}: {' '.join(runner.argv)}{model_text}")
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
    def from_settings(
        cls,
        settings: EngineSettings,
        *,
        environ: Mapping[str, str] | None = None,
    ) -> WorkflowConfig:
        """Build the workflow from environment variables.

        The team, the runners and the status defaults are baked in; the
        deployment supplies its OpenProject URL, project, repository, agent CLI
        and one token per agent.
        """
        env = os.environ if environ is None else environ
        missing = [
            name
            for name, value in (
                ("OP_BASE_URL", settings.op_base_url),
                ("OP_PROJECT", settings.op_project),
                ("GITHUB_REPO", settings.github_repo),
                ("AGENT", settings.agent),
            )
            if not value
        ]
        if missing:
            raise ConfigError(
                "missing required environment variables: " + ", ".join(missing)
            )

        assert settings.op_base_url is not None
        assert settings.op_project is not None
        assert settings.github_repo is not None
        assert settings.agent is not None

        repo_dir = settings.github_repo_dir or settings.github_repo.rsplit("/", 1)[-1]
        statuses = {
            "new": settings.op_status_new,
            "in_progress": settings.op_status_in_progress,
            "ready_for_review": settings.op_status_ready_for_review,
            "in_testing": settings.op_status_in_testing,
            "on_hold": settings.op_status_on_hold,
            "rejected": settings.op_status_rejected,
            "closed": settings.op_status_closed,
        }
        agents: dict[str, Any] = {}
        for key, login, kind, token_var in TEAM:
            agents[key] = {
                "name": login,
                "kind": kind,
                "op_login": login,
                "workspace": login,
                "runner": settings.agent,
                "timeout_seconds": settings.agent_timeout_seconds,
                "env": {
                    "OP_CLI_HOST": settings.op_base_url,
                    "OP_CLI_TOKEN": (env.get(token_var) or "").strip(),
                },
            }

        mapping: dict[str, Any] = {
            "openproject": {
                "statuses": statuses,
                "agent_field": settings.op_agent_field,
                "engine_user": settings.op_engine_user,
                "provisioning": {
                    "enabled": True,
                    "agent_role": settings.op_agent_role,
                    "email_domain": settings.op_email_domain,
                },
            },
            "projects": [
                {
                    "identifier": settings.op_project,
                    "repo": {
                        "github": settings.github_repo,
                        "dir": repo_dir,
                        "default_branch": settings.github_default_branch,
                    },
                    "enabled": True,
                }
            ],
            "agents": agents,
            "runners": _runner_mapping(settings.agent_model or None),
        }
        return cls.from_mapping(mapping, environ=env, source="environment")


# ---------------------------------------------------------------------------
# The agent team
# ---------------------------------------------------------------------------

#: ``(AGENT field option, OpenProject login, kind, token variable)``.
#: The team is baked into the image (``agents/*/AGENTS.md``); only the tokens
#: differ between deployments.
TEAM: tuple[tuple[str, str, str, str], ...] = (
    ("FRONTEND", "frontendEngineer", "implementer", "OP_TOKEN_FRONTEND"),
    ("BACKEND", "backendEngineer", "implementer", "OP_TOKEN_BACKEND"),
    ("LEAD_DESIGNER", "leadDesigner", "implementer", "OP_TOKEN_DESIGNER"),
    ("REVIEWER", "codeReviewer", "reviewer", "OP_TOKEN_REVIEWER"),
)

#: One token per agent, required before the engine can run a session.
AGENT_TOKEN_VARS: tuple[str, ...] = tuple(entry[3] for entry in TEAM)

_PI_ARGV = [
    "pi",
    "-p",
    "--approve",
    "--no-extensions",
    "--no-context-files",
    "--no-session",
    "--model",
    "{model}",
    "@{prompt_file}",
    "Follow the instructions in the attached prompt file.",
]
_OPENCODE_ARGV = [
    "opencode",
    "run",
    "--auto",
    "--model",
    "{model}",
    "--file",
    "{prompt_file}",
    "Follow the instructions in the attached prompt file.",
]


def _runner_mapping(model: str | None) -> dict[str, Any]:
    return {
        "pi": {
            "argv": list(_PI_ARGV),
            "model": model,
            "cwd": "{repo_dir}",
            "inject_standard": True,
        },
        "opencode": {
            "argv": list(_OPENCODE_ARGV),
            "model": model,
            "cwd": "{repo_dir}",
            "inject_standard": True,
        },
    }


DEFAULT_DATA_DIR = "~/.local/share/op-agent-engine"


class EngineSettings(BaseSettings):
    """Every deployment and runtime setting, supplied as environment variables."""

    model_config = SettingsConfigDict(
        env_file=None,
        extra="ignore",
        case_sensitive=False,
    )

    # OpenProject and the project the engine works on.
    op_base_url: str | None = None
    op_engine_token: SecretStr | None = None
    op_provision_token: SecretStr | None = None
    op_project: str | None = None
    # Which agent CLI to run: ``pi`` or ``opencode``.
    agent: Literal["pi", "opencode"] | None = None
    agent_model: str | None = None
    agent_timeout_seconds: int = Field(default=3600, gt=0)

    # The repository each work package is implemented in.
    github_repo: str | None = None
    github_repo_dir: str | None = None
    github_default_branch: str = "main"

    # OpenProject object names (defaults match a stock OpenProject instance).
    op_agent_field: str = "AGENT"
    op_engine_user: str = "agent-engine"
    op_agent_role: str = "Agent Worker"
    op_email_domain: str = "agent-engine.local"
    op_status_new: str = "New"
    op_status_in_progress: str = "In Progress"
    op_status_ready_for_review: str = "Ready for Review"
    op_status_in_testing: str = "In Testing"
    op_status_on_hold: str = "On Hold"
    op_status_rejected: str = "Rejected"
    op_status_closed: str = "Closed"

    poll_interval_seconds: int = Field(default=60, gt=0)
    max_concurrent_runs: int = Field(default=1, gt=0)
    max_runs_per_agent: int = Field(default=1, gt=0)
    max_rejections: int = Field(default=3, ge=0)
    max_crash_retries: int = Field(default=2, ge=0)
    run_timeout_seconds: int = Field(default=3600, gt=0)
    stop_grace_seconds: int = Field(default=60, ge=0)
    run_retention_days: int = Field(default=30, ge=0)
    data_dir: Path = Field(default_factory=lambda: Path(DEFAULT_DATA_DIR).expanduser())
    workspace_root: Path | None = None
    prompts_dir: Path | None = None
    gh_token: SecretStr | None = None
    engine_paused: bool = False
    log_level: str = "INFO"
    dashboard_host: str = "127.0.0.1"
    dashboard_port: int = Field(default=8765, ge=1, le=65535)

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
        if not self.op_project:
            missing.append("OP_PROJECT")
        if not self.github_repo:
            missing.append("GITHUB_REPO")
        if not self.agent:
            missing.append("AGENT")
        if self.workspace_root is None or not str(self.workspace_root):
            missing.append("WORKSPACE_ROOT")
        missing.extend(self.missing_agent_tokens())
        return missing

    def missing_agent_tokens(
        self, environ: Mapping[str, str] | None = None
    ) -> list[str]:
        """Token variables that are absent or empty, one per agent."""
        env = os.environ if environ is None else environ
        return [name for name in AGENT_TOKEN_VARS if not (env.get(name) or "").strip()]

    def require_runtime_values(self) -> None:
        missing = self.missing_runtime_values()
        if missing:
            raise ConfigError(
                "missing required environment variables: " + ", ".join(missing)
            )
