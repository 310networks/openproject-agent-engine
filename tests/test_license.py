"""License and package metadata invariants.

The project is distributed both as a Python package and as a container image.
Both distributions must carry the MIT license, and the repository metadata must
advertise it.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
LICENSE = REPO_ROOT / "LICENSE"
PYPROJECT = REPO_ROOT / "pyproject.toml"
README = REPO_ROOT / "README.md"
DOCKERFILE = REPO_ROOT / "docker" / "Dockerfile"


def test_mit_license_is_present_and_complete() -> None:
    text = LICENSE.read_text(encoding="utf-8")

    assert text.startswith("MIT License\n")
    assert re.search(r"Copyright \(c\) \d{4} 310 Networks", text)
    for phrase in (
        "Permission is hereby granted, free of charge",
        "The above copyright notice and this permission notice shall be included in all",
        'THE SOFTWARE IS PROVIDED "AS IS"',
    ):
        assert phrase in text


def test_package_metadata_declares_mit() -> None:
    project = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))["project"]

    assert project["license"] == {"text": "MIT"}


def test_readme_links_to_the_license() -> None:
    assert "[MIT License](LICENSE)" in README.read_text(encoding="utf-8")


def test_container_image_ships_the_license() -> None:
    dockerfile = DOCKERFILE.read_text(encoding="utf-8")

    assert "COPY LICENSE /opt/op-agent-engine/LICENSE" in dockerfile
