"""Tests for the ``engine.env`` reader/writer used by ``provision-users``."""

from __future__ import annotations

from pathlib import Path

import pytest

from op_agent_engine.envfile import EnvFileError, load_env_file, update_env_file

ENV_TEXT = """\
# operator environment, kept by hand
OP_BASE_URL=https://openproject.example.com
export OP_ENGINE_TOKEN="quoted-token"
OP_TOKEN_FRONTEND='single-quoted'
not a key value line

AGENT=pi
"""


def test_load_env_file_accepts_comments_export_and_quotes(tmp_path: Path) -> None:
    path = tmp_path / "engine.env"
    path.write_text(ENV_TEXT, encoding="utf-8")

    values = load_env_file(path)

    assert values["OP_BASE_URL"] == "https://openproject.example.com"
    assert values["OP_ENGINE_TOKEN"] == "quoted-token"
    assert values["OP_TOKEN_FRONTEND"] == "single-quoted"
    assert values["AGENT"] == "pi"
    assert "# operator environment, kept by hand" not in values


def test_load_env_file_reports_unreadable_file(tmp_path: Path) -> None:
    with pytest.raises(EnvFileError, match="cannot read"):
        load_env_file(tmp_path / "missing.env")


def test_update_env_file_keeps_comments_and_order(tmp_path: Path) -> None:
    path = tmp_path / "engine.env"
    path.write_text(ENV_TEXT, encoding="utf-8")
    path.chmod(0o644)

    update_env_file(path, {"OP_ENGINE_TOKEN": "new-token", "OP_TOKEN_BACKEND": "backend"})

    text = path.read_text(encoding="utf-8")
    assert text.startswith("# operator environment, kept by hand\n")
    assert "OP_ENGINE_TOKEN=new-token" in text
    # The quoted original is replaced in place, not duplicated.
    assert "quoted-token" not in text
    assert text.index("OP_BASE_URL") < text.index("OP_ENGINE_TOKEN")
    # A new key is appended rather than inserted in the middle.
    assert text.index("OP_TOKEN_BACKEND=backend") > text.index("AGENT=pi")
    # Tokens live in this file: it must never be group/world readable.
    assert path.stat().st_mode & 0o077 == 0


def test_update_env_file_creates_a_missing_file(tmp_path: Path) -> None:
    path = tmp_path / "engine.env"

    update_env_file(path, {"OP_ENGINE_TOKEN": "token"})

    assert load_env_file(path) == {"OP_ENGINE_TOKEN": "token"}
    assert path.stat().st_mode & 0o077 == 0
