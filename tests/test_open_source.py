"""Repository files a public GitHub project is expected to have.

These are not runtime behaviour: they decide who reviews what and where a
security report goes. Both are silent when wrong, so they are asserted here.
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
CODEOWNERS = REPO_ROOT / ".github" / "CODEOWNERS"
SECURITY = REPO_ROOT / "SECURITY.md"


def _codeowners_rules() -> list[tuple[str, list[str]]]:
    """Return ``(pattern, owners)`` for every rule, skipping comments."""

    rules = []
    for line in CODEOWNERS.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        pattern, *owners = line.split()
        rules.append((pattern, owners))
    return rules


def test_codeowners_has_no_catch_all_rule() -> None:
    # Agents open and merge their own pull requests. A `*` rule would require
    # the named owners on every one of them, which is not how this works.
    patterns = [pattern for pattern, _ in _codeowners_rules()]

    assert patterns, "CODEOWNERS has no rules"
    assert "*" not in patterns
    assert "**" not in patterns


def test_codeowners_covers_the_paths_that_can_change_credentials_or_the_image() -> None:
    patterns = {pattern for pattern, _ in _codeowners_rules()}

    assert "/.github/" in patterns
    assert "/docker/" in patterns
    assert "/pyproject.toml" in patterns


def test_codeowners_owners_are_users_not_teams() -> None:
    owners = {owner for _, rule_owners in _codeowners_rules() for owner in rule_owners}

    # Both have write access, so both can approve and merge a PR the other
    # opened. The reviewer account must stay listed: the author cannot approve
    # its own pull request.
    assert owners == {"@Kookster310", "@KookyAgentReviewer"}


def test_the_agent_author_is_never_a_code_owner() -> None:
    # @KookyAgent opens the engine's pull requests. An owner that is also the
    # author can never satisfy the code-owner requirement.
    owners = {owner for _, rule_owners in _codeowners_rules() for owner in rule_owners}

    assert "@KookyAgent" not in owners


def test_every_rule_lists_the_reviewer_account() -> None:
    for pattern, owners in _codeowners_rules():
        assert "@KookyAgentReviewer" in owners, f"{pattern} would block the reviewer"


def test_security_policy_points_at_private_advisories() -> None:
    text = SECURITY.read_text(encoding="utf-8")

    assert "security/advisories/new" in text
    # A report must not be invited into a public issue.
    assert "do **not** open a public issue" in text.lower()
    assert "## Supported versions" in text
