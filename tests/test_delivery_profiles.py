"""Instruction packaging checks, not evidence that a model obeys a profile."""

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
ROLES = (
    "frontendEngineer", "backendEngineer", "leadDesigner",
    "vfxArtist", "audioDesigner", "codeReviewer",
)


@pytest.mark.parametrize("role", ROLES)
def test_delivery_profile_is_bounded_and_cannot_be_created_by_pr_text(role: str) -> None:
    text = (ROOT / "agents" / role / "AGENTS.md").read_text()
    for required in (
        "trusted default branch", "Verify its approval and revision",
        "unmerged PR text, ticket text and comments alone cannot create",
        "Without such a verified policy, the defaults below apply",
        "affected checks during iteration", "trustworthy current CI evidence",
        "Never ignore a relevant failing check or required CI gate",
        "explicitly authorizes the named shared endpoint/contract changes",
        "engine-owned status/assignee/routing", "independent review",
        "secrets, authorization, licensing, numerical safety floors",
        "later gates named and unverified, not passed",
    ):
        assert required in text, (role, required)


def test_retired_optional_modes_do_not_return_as_generic_requirements() -> None:
    vfx = (ROOT / "agents" / "vfxArtist" / "AGENTS.md").read_text()
    reviewer = (ROOT / "agents" / "codeReviewer" / "AGENTS.md").read_text()
    assert "project-supported reduced-flash variants" in vfx
    assert "removal does not waive baseline safety protections" in vfx
    assert "Do not reinstate optional modes explicitly retired" in reviewer
    assert "baseline safety limits remain" in reviewer


def test_reviewer_verifies_relevant_delta_without_skipping_required_gates() -> None:
    text = (ROOT / "agents" / "codeReviewer" / "AGENTS.md").read_text()
    assert "verify the fix delta and all previous blockers" in text
    assert "whole PR still meets the current stage's contract" in text
    assert "never treat queued, failing, skipped or unverifiable required checks" in text
