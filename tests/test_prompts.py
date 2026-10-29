"""Tests for prompt rendering (PLAN section 10)."""

from pathlib import Path

import pytest

from op_agent_engine.config import ConfigError, EngineSettings
from op_agent_engine.prompts import (
    PromptRenderer,
    resolve_template_dir,
    select_fix_context,
    with_standard,
)


@pytest.fixture()
def template_dir(tmp_path: Path) -> Path:
    directory = tmp_path / "prompts"
    directory.mkdir()
    (directory / "implement.txt.j2").write_text(
        "Implement {{ wp.subject }} ({{ wp.id }}) on {{ branch }}.\n"
        "Repo: {{ repo.github }}\n"
        "Standard: {{ standard_path }}\n",
        encoding="utf-8",
    )
    (directory / "review.txt.j2").write_text("Review {{ pr_url }}.\n", encoding="utf-8")
    return directory


def _context() -> dict:
    return {
        "wp": {
            "id": 100,
            "subject": "Add a thing",
            "type": "Feature",
            "url": "https://op.example/projects/4/work_packages/100",
            "description": "make it so",
        },
        "branch": "op-4-100",
        "repo": {"github": "acme/thing", "dir": "thing", "default_branch": "main"},
        "standard_path": "/ws/acme/thing/AGENTS.md",
        "agent": {"name": "frontendEngineer"},
    }


def test_render_implement(template_dir: Path) -> None:
    renderer = PromptRenderer(template_dir)
    prompt = renderer.render("implement", _context())
    assert "Implement Add a thing (100) on op-4-100." in prompt
    assert "Repo: acme/thing" in prompt
    assert "Standard: /ws/acme/thing/AGENTS.md" in prompt


def test_render_review(template_dir: Path) -> None:
    renderer = PromptRenderer(template_dir)
    assert renderer.render("review", {"pr_url": "https://pr"}) == (
        "Review https://pr.\n"
    )


def test_missing_template_raises_config_error(template_dir: Path) -> None:
    renderer = PromptRenderer(template_dir)
    with pytest.raises(ConfigError, match="no prompt template for kind 'fix'"):
        renderer.render("fix", {})


def test_undefined_variable_raises_config_error(template_dir: Path) -> None:
    renderer = PromptRenderer(template_dir)
    context = _context()
    del context["branch"]
    with pytest.raises(ConfigError, match="undefined"):
        renderer.render("implement", context)


def test_resolve_template_dir_explicit(tmp_path: Path) -> None:
    directory = tmp_path / "custom"
    directory.mkdir()
    (directory / "implement.txt.j2").write_text("x", encoding="utf-8")
    settings = EngineSettings(prompts_dir=directory)
    assert resolve_template_dir(settings) == directory


def test_resolve_template_dir_next_to_workspace_root(tmp_path: Path) -> None:
    prompts = tmp_path / "checkout" / "prompts"
    prompts.mkdir(parents=True)
    (prompts / "implement.txt.j2").write_text("x", encoding="utf-8")
    settings = EngineSettings(
        workspace_root=tmp_path / "checkout" / "agents", prompts_dir=None
    )
    assert resolve_template_dir(settings) == prompts


def test_resolve_template_dir_package_relative() -> None:
    """The repository ships prompts/ next to the source tree."""
    settings = EngineSettings(prompts_dir=None, workspace_root=None)
    resolved = resolve_template_dir(settings)
    assert (resolved / "implement.txt.j2").is_file()
    assert (resolved / "fix.txt.j2").is_file()
    assert (resolved / "resume.txt.j2").is_file()
    assert (resolved / "review.txt.j2").is_file()


def test_resolve_template_dir_not_found(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Point the package-relative candidate at a directory without prompts/.
    monkeypatch.setattr(
        "op_agent_engine.prompts.__file__",
        str(tmp_path / "somewhere" / "prompts.py"),
    )
    settings = EngineSettings(
        prompts_dir=tmp_path / "nope",
        workspace_root=tmp_path / "agents",
    )
    with pytest.raises(ConfigError, match="no prompts directory found"):
        resolve_template_dir(settings)


def test_with_standard_prepends_standard(tmp_path: Path) -> None:
    standard = tmp_path / "AGENTS.md"
    standard.write_text("# Standard\n\nBe excellent.\n", encoding="utf-8")
    result = with_standard("Do the task.", standard)
    assert result.startswith("# Engineering standard\n")
    assert f"Source: {standard}" in result
    assert "Be excellent." in result
    assert result.rstrip().endswith("Do the task.")


# -- fix-pass comment selection (M6) -----------------------------------------


def _comment(
    activity_id: int,
    author_id: int,
    text: str,
    *,
    minutes: int,
    title: str | None = None,
) -> dict:
    user: dict = {"href": f"/api/v3/users/{author_id}"}
    if title is not None:
        user["title"] = title
    return {
        "id": activity_id,
        "workPackageId": 100,
        "_type": "Activity::Comment",
        "comment": {"raw": text},
        "details": [],
        "createdAt": f"2026-02-10T12:{minutes:02d}:00Z",
        "_links": {"user": user},
    }


def _select(activities: list[dict]):
    return select_fix_context(
        activities,
        reviewer_id=12,
        agent_user_ids=frozenset({11}),
        engine_user_id=10,
        author_names={12: "Reviewer", 11: "Implementer", 10: "Agent Engine"},
    )


def test_fix_context_uses_latest_reviewer_comment() -> None:
    context = select_fix_context(
        [
            _comment(1, 12, "first rejection", minutes=1),
            _comment(2, 11, "agent note", minutes=2),
            _comment(3, 12, "latest rejection", minutes=3),
            _comment(4, 99, "human extra", minutes=4, title="Jane"),
        ],
        reviewer_id=12,
        agent_user_ids=frozenset({11}),
        engine_user_id=10,
        author_names={12: "Reviewer", 11: "Implementer", 10: "Agent Engine"},
    )

    assert context.feedback is not None
    assert context.feedback.text == "latest rejection"
    assert context.feedback.author == "Reviewer (reviewer)"
    assert [c.text for c in context.later_human_comments] == ["human extra"]
    assert context.later_human_comments[0].author == "Jane (human)"


def test_fix_context_excludes_agent_and_engine_comments() -> None:
    context = _select(
        [
            _comment(1, 12, "rejection", minutes=1),
            _comment(2, 11, "agent after", minutes=2),
            _comment(3, 10, "ENGINE: escalated", minutes=3),
            _comment(4, 99, "human after", minutes=4),
            _comment(5, 100, "unknown title fallback", minutes=5),
        ]
    )

    assert [c.text for c in context.later_human_comments] == [
        "human after",
        "unknown title fallback",
    ]
    assert context.later_human_comments[1].author == "user 100 (human)"


def test_fix_context_caps_later_human_comments_at_five() -> None:
    activities = [_comment(1, 12, "rejection", minutes=1)] + [
        _comment(10 + index, 99, f"human {index}", minutes=2 + index)
        for index in range(7)
    ]

    context = _select(activities)

    assert [c.text for c in context.later_human_comments] == [
        "human 2",
        "human 3",
        "human 4",
        "human 5",
        "human 6",
    ]


def test_fix_context_without_reviewer_comment_has_no_feedback() -> None:
    context = _select([_comment(1, 11, "agent note", minutes=1)])

    assert context.feedback is None
    assert context.later_human_comments == ()
