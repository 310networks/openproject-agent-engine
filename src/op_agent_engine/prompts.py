"""Prompt rendering (PLAN section 10).

Templates live in a ``prompts/`` directory (``<kind>.txt.j2``) and are
rendered with Jinja2 in ``StrictUndefined`` mode into
``DATA_DIR/runs/<runId>/prompt.txt``.

The prompt is self-contained: it references the repository, branch, ticket
and standard by absolute path and does not assume the agent has memory of
previous runs.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from jinja2 import (
    Environment,
    FileSystemLoader,
    StrictUndefined,
    TemplateNotFound,
    UndefinedError,
)

from op_agent_engine.config import ConfigError, EngineSettings

PROMPTS_DIR_NAME = "prompts"

#: Kinds the engine can render (M4 implements implement; fix/resume are M5,
#: review is M6 — the templates ship with all of them).
KINDS = ("implement", "fix", "resume", "review")


class PromptRenderer:
    """Renders ``<kind>.txt.j2`` templates from one directory."""

    def __init__(self, template_dir: Path) -> None:
        self._template_dir = template_dir
        self._env = Environment(
            loader=FileSystemLoader(str(template_dir)),
            undefined=StrictUndefined,
            keep_trailing_newline=True,
            autoescape=False,
        )

    def render(self, kind: str, context: dict[str, Any]) -> str:
        """Render *kind* with *context*; raise :class:`ConfigError` on error."""
        try:
            template = self._env.get_template(f"{kind}.txt.j2")
        except TemplateNotFound as error:
            raise ConfigError(
                f"no prompt template for kind {kind!r} in {self._template_dir}"
            ) from error
        try:
            return template.render(**context)
        except UndefinedError as error:
            raise ConfigError(
                f"prompt template for kind {kind!r} references an undefined "
                f"variable: {error.message}"
            ) from error


def resolve_template_dir(settings: EngineSettings) -> Path:
    """Locate the prompts directory (PLAN section 17).

    Order: explicit ``PROMPTS_DIR``, the ``prompts/`` directory next to
    ``WORKSPACE_ROOT`` (the engine checkout when the workspaces live in
    ``<checkout>/agents/``), then the repository the package was installed
    from.
    """
    candidates: list[Path] = []
    if settings.prompts_dir is not None:
        candidates.append(settings.prompts_dir)
    if settings.workspace_root is not None:
        candidates.append(settings.workspace_root.parent / PROMPTS_DIR_NAME)
    candidates.append(Path(__file__).resolve().parents[2] / PROMPTS_DIR_NAME)
    for candidate in candidates:
        if (candidate / "implement.txt.j2").is_file():
            return candidate
    raise ConfigError(
        "no prompts directory found (looked for prompts/implement.txt.j2 in: "
        + ", ".join(str(candidate) for candidate in candidates)
        + "; set PROMPTS_DIR to override)"
    )


def with_standard(prompt: str, standard_path: Path) -> str:
    """Prepend the workspace AGENTS.md under a heading (section 10).

    Used when the runner does not discover the standard on its own (e.g.
    ``opencode``); ``pi`` discovers ``AGENTS.md`` by walking up from the
    working directory, so the prompt stays lean.
    """
    content = standard_path.read_text(encoding="utf-8").strip()
    return (
        f"# Engineering standard\n\n"
        f"Source: {standard_path}\n\n"
        f"{content}\n\n"
        f"---\n\n"
        f"{prompt}"
    )
