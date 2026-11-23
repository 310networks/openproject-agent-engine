"""Hosting-provider client construction.

The dispatcher and the resume gate both talk to the hosting provider (GitHub or
GitLab). They share one factory so both always use the same token and host:
a PR lookup that the dispatcher accepts must be the one the gate polls.
"""

from __future__ import annotations

from op_agent_engine.config import EngineSettings
from op_agent_engine.github import GitHub
from op_agent_engine.gitlab import GitLab


def build_hosting_client(settings: EngineSettings) -> GitHub | GitLab:
    """The hosting client for the configured ``GIT_HOST``."""
    if settings.git_host == "github.com":
        return GitHub(actionable_checks=settings.ci_actionable_checks)
    token = (
        settings.gitlab_token.get_secret_value()
        if settings.gitlab_token is not None
        else None
    )
    return GitLab(host=settings.git_host, token=token)
