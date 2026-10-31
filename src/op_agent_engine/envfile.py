"""Read and update ``engine.env``-style files.

The container deployment keeps the engine's settings in a plain ``KEY=VALUE``
file that compose reads on the host. ``provision-users`` writes the tokens it
collects back into that file, so two operations are needed: parse it (comments,
blank lines and ``export`` prefixes accepted, no shell expansion) and update
individual keys in place while keeping comments and ordering intact.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from pathlib import Path

_ENV_LINE = re.compile(r"^(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=")


class EnvFileError(RuntimeError):
    """The environment file cannot be read or written."""


def load_env_file(path: Path) -> dict[str, str]:
    """Read a simple ``KEY=VALUE`` environment file.

    Comments, blank lines and ``export`` prefixes are accepted. Values may be
    single- or double-quoted; no shell expansion is performed.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as error:
        raise EnvFileError(f"cannot read {path}: {error}") from error

    env: dict[str, str] = {}
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("export "):
            stripped = stripped[len("export ") :].lstrip()
        key, separator, value = stripped.partition("=")
        key = key.strip()
        if not separator or not key:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        env[key] = value
    return env


def update_env_file(path: Path, updates: Mapping[str, str]) -> None:
    """Update ``KEY=VALUE`` lines in place while preserving comments and order.

    Missing keys are appended under a short marker. The file is rewritten
    atomically and forced to mode ``0600`` because it contains API tokens.
    """
    try:
        original = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        original = "# op-agent-engine environment file\n"
    except OSError as error:
        raise EnvFileError(f"cannot read {path}: {error}") from error

    remaining = dict(updates)
    lines: list[str] = []
    for line in original.splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            match = _ENV_LINE.match(stripped)
            if match is not None:
                key = match.group(1)
                if key in remaining:
                    lines.append(f"{key}={remaining.pop(key)}")
                    continue
        lines.append(line)

    if remaining:
        if lines and lines[-1].strip():
            lines.append("")
        lines.append("# Added by op-agent-engine provision-users")
        for key, value in remaining.items():
            lines.append(f"{key}={value}")

    text = "\n".join(lines).rstrip("\n") + "\n"
    try:
        _write_private(path, text, 0o600)
    except OSError as error:
        raise EnvFileError(f"cannot write {path}: {error}") from error


def _write_private(path: Path, text: str, mode: int) -> None:
    """Atomically write ``text`` and enforce ``mode``."""
    temp = path.with_name(f".{path.name}.tmp")
    temp.write_text(text, encoding="utf-8")
    os.chmod(temp, mode)
    os.replace(temp, path)
    os.chmod(path, mode)
