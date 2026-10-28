"""Tests for prompt rendering (PLAN section 10)."""

from pathlib import Path

import pytest

from op_agent_engine.config import ConfigError, EngineSettings
from op_agent_engine.prompts import (
    PromptRenderer,
    resolve_template_dir,
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
