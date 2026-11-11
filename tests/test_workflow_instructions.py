"""Shipped workflows must work with the tools actually baked into the image.

These packaging checks do not prove model obedience or remote write permissions.
No network calls, credentials or optional skill files are used by these tests.
"""

import json
import re
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

from op_agent_engine.prompts import PromptRenderer, with_standard

ROOT = Path(__file__).resolve().parents[1]
ROLES = (
    "frontendEngineer",
    "backendEngineer",
    "leadDesigner",
    "audioDesigner",
    "vfxArtist",
    "codeReviewer",
)
RUNS = [
    (role, kind)
    for role in ROLES
    for kind in (("review",) if role == "codeReviewer" else ("implement", "fix", "resume"))
]


def _standard(role: str) -> Path:
    return ROOT / "agents" / role / "AGENTS.md"


def _assert_self_contained(text: str) -> None:
    assert "External skill files are not required." in text
    assert "OpenProject skill" not in text
    assert "global GitHub PR skill" not in text
    assert "A missing optional skill is not an access failure" in text
    assert "op work-package inspect <id> --format json" in text
    assert "GET /api/v3/work_packages/<id>/activities" in text
    assert "GET /api/v3/relations?filters=" in text
    assert "`gh`" in text
    assert "`git`" in text
    assert "`jq`" not in text
    assert "jq -Rs" not in text
    assert "Owned by the engine. **Never change it.**" in text
    assert "Do not change the assignee or other ticket fields either." in text
    assert "Never read a credential file" in text
    assert "never use another account's token" in text
    assert "if a tool or API actually fails, report the failing command and error" in text
    assert "with secrets redacted" in text


@pytest.mark.parametrize("role", ROLES)
def test_all_roles_have_supported_self_contained_workflows(role: str) -> None:
    text = _standard(role).read_text(encoding="utf-8")
    _assert_self_contained(text)
    filters = re.search(r"GET /api/v3/relations\?filters=(.*?)`", text)
    assert filters is not None
    assert json.loads(filters[1]) == [
        {"involved": {"operator": "=", "values": ["<id>"]}}
    ]
    if role == "codeReviewer":
        assert "Never edit, commit to, or push to the PR branch." in text
        assert "do not work around it" in text  # Branch protection remains authoritative.
    else:
        assert "Do not force-push." in text
        assert "Do not open a second PR." in text


@pytest.mark.parametrize(("role", "kind"), RUNS)
def test_every_real_run_kind_injects_complete_workflow(role: str, kind: str) -> None:
    standard = _standard(role)
    context = {
        "agent": {"name": role},
        "standard_path": str(standard),
        "wp": {
            "id": 449,
            "subject": "Implement source proofs",
            "type": "Task",
            "url": "https://op.example/work_packages/449",
            "description": "Build against existing source contracts.",
        },
        "project": {"identifier": "game"},
        "repo": {"github": "acme/game", "default_branch": "main"},
        "repo_dir": "/workspace/game",
        "branch": "op-8-449",
        "attempt": 1,
        "max_rejections": 3,
        "feedback": {"author": "Reviewer", "created_at": "2026-11-10", "text": "Fix."},
        "later_human_comments": [],
        "pr": {"url": "https://github.com/acme/game/pull/1"},
        "review_number": 1,
    }
    prompt = with_standard(PromptRenderer(ROOT / "prompts").render(kind, context), standard)
    _assert_self_contained(prompt)
    assert "Finish with the RESULT line required by the standard." in prompt


@pytest.mark.parametrize("role", ROLES)
def test_comment_payload_example_uses_stdlib_and_preserves_exact_report(role: str) -> None:
    text = _standard(role).read_text(encoding="utf-8")
    line = next(line for line in text.splitlines() if line.startswith("python -c "))
    argv = shlex.split(line.split(" < /tmp/", 1)[0])
    assert argv[:2] == ["python", "-c"]
    report = 'Report: "quoted" \\ newline\nUnicode: café ✅\n{"raw": false}\n'
    result = subprocess.run(
        [sys.executable, *argv[1:]], input=report, text=True,
        capture_output=True, check=True,
    )
    assert json.loads(result.stdout) == {"comment": {"raw": report}}
    assert "curl -fsS -X POST" in text  # HTTP errors must fail, not look successful.
    assert '-u "apikey:$OP_CLI_TOKEN"' in text
