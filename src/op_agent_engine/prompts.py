"""Prompt rendering.

Templates live in a ``prompts/`` directory (``<kind>.txt.j2``) and are
rendered with Jinja2 in ``StrictUndefined`` mode into
``DATA_DIR/runs/<runId>/prompt.txt``.

The prompt is self-contained: it references the repository, branch, ticket
and standard by absolute path and does not assume the agent has memory of
previous runs.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
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
from op_agent_engine.openproject.activities import ActivityComment, comment_from_activity

PROMPTS_DIR_NAME = "prompts"

#: Kinds the engine can render (implement/fix/resume/review).
KINDS = ("implement", "fix", "resume", "review")

#:: cap on later human comments in a fix prompt.
MAX_LATER_HUMAN_COMMENTS = 5


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
    """Locate the prompts directory.

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


@dataclass(frozen=True)
class PromptComment:
    """A comment rendered into the fix prompt (author labelled with its role)."""

    author: str
    created_at: str
    text: str


@dataclass(frozen=True)
class FixContext:
    """Reviewer feedback and later human comments for a fix run."""

    feedback: PromptComment | None
    later_human_comments: tuple[PromptComment, ...] = ()


def select_fix_context(
    activities: Iterable[dict[str, Any]],
    *,
    reviewer_id: int,
    agent_user_ids: frozenset[int],
    engine_user_id: int,
    author_names: Mapping[int, str] | None = None,
    max_human_comments: int = MAX_LATER_HUMAN_COMMENTS,
) -> FixContext:
    """Select the comments a fix pass needs.

    * ``feedback``: the latest comment authored by the reviewer user.
    * ``later_human_comments``: comments authored by humans (not an agent user
      and not ``agent-engine``) posted after the feedback, newest last, at
      most ``max_human_comments``.

    All other agent and engine comments are excluded. Authors are labelled
    with their name and role; unknown human authors fall back to the name the
    API includes on the activity link, or ``user <id>``.
    """
    names = author_names or {}
    comments = [
        comment
        for comment in (comment_from_activity(activity) for activity in activities)
        if comment is not None
    ]
    comments.sort(
        key=lambda comment: (
            comment.created_at or datetime.min.replace(tzinfo=UTC),
            comment.activity_id or 0,
        )
    )

    feedback_index: int | None = None
    for index, comment in enumerate(comments):
        if comment.author_id == reviewer_id:
            feedback_index = index
    if feedback_index is None:
        return FixContext(feedback=None)

    feedback_comment = comments[feedback_index]
    human_comments: list[PromptComment] = []
    for comment in comments[feedback_index + 1:]:
        if comment.author_id is None:
            continue
        if comment.author_id in agent_user_ids or comment.author_id == engine_user_id:
            continue
        human_comments.append(
            PromptComment(
                author=_author_label(comment, "human", names),
                created_at=comment.created_at_raw or "unknown",
                text=comment.text,
            )
        )

    human_comments = (
        human_comments[-max_human_comments:] if max_human_comments >= 1 else []
    )

    return FixContext(
        feedback=PromptComment(
            author=_author_label(feedback_comment, "reviewer", names),
            created_at=feedback_comment.created_at_raw or "unknown",
            text=feedback_comment.text,
        ),
        later_human_comments=tuple(human_comments),
    )


def _author_label(
    comment: ActivityComment, role: str, names: Mapping[int, str]
) -> str:
    name = names.get(comment.author_id) if comment.author_id is not None else None
    if name is None:
        name = comment.author_title
    if name is None and comment.author_id is not None:
        name = f"user {comment.author_id}"
    if name is None:
        name = "unknown author"
    return f"{name} ({role})"


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
