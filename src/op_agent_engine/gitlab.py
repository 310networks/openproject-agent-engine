"""GitLab REST adapter for GitLab.com and HTTPS self-managed instances.

The existing dispatcher protocol/result model is shared with GitHub so draft,
recovery and merged-result checks follow exactly the same workflow.
"""

from __future__ import annotations

from urllib.parse import quote

import httpx
from pydantic import BaseModel, ConfigDict, ValidationError

from op_agent_engine.config import validate_git_host
from op_agent_engine.github import GitHubError, PullRequest


class _MergeRequest(BaseModel):
    model_config = ConfigDict(strict=True)

    iid: int
    web_url: str
    source_branch: str
    state: str
    source_project_id: int
    target_project_id: int
    draft: bool | None = None
    work_in_progress: bool | None = None


class GitLab:
    """Provider-neutral ``find_pr`` implementation; failures use the shared error."""

    def __init__(self, *, host: str, token: str | None, timeout: float = 30) -> None:
        self.host = validate_git_host(host)
        self._token = token
        self._timeout = timeout

    async def find_pr(
        self, repo: str, branch: str, *, state: str = "open"
    ) -> PullRequest | None:
        states = {"open": "opened", "closed": "closed", "merged": "merged", "all": "all"}
        if state not in states:
            raise GitHubError(f"unsupported merge request state: {state}")
        if not self._token:
            raise GitHubError("GITLAB_TOKEN is required for GitLab lookups")
        url = f"https://{self.host}/api/v4/projects/{quote(repo, safe='')}/merge_requests"
        headers = {"PRIVATE-TOKEN": self._token}
        page = 1
        async with httpx.AsyncClient(timeout=self._timeout, headers=headers) as client:
            while True:
                try:
                    response = await client.get(url, params={
                        "source_branch": branch, "state": states[state], "scope": "all",
                        "order_by": "created_at", "sort": "desc", "per_page": 100,
                        "page": page,
                    })
                    response.raise_for_status()
                except httpx.HTTPError as error:
                    # Never echo provider response bodies, headers, tokens or URLs.
                    status = (
                        str(error.response.status_code)
                        if isinstance(error, httpx.HTTPStatusError) else type(error).__name__
                    )
                    raise GitHubError(f"GitLab merge request lookup failed ({status})") from None
                try:
                    entries = response.json()
                    if not isinstance(entries, list):
                        raise ValueError("expected a list")
                    for entry in entries:
                        mr = _MergeRequest.model_validate(entry)
                        if mr.state not in {"opened", "closed", "merged"}:
                            raise ValueError("unknown MR state")
                        if mr.draft is None and mr.work_in_progress is None:
                            raise ValueError("missing draft status")
                        # A same-named branch from a fork is not this engine's work.
                        if (mr.source_branch != branch
                            or mr.source_project_id != mr.target_project_id
                            or (state != "all" and mr.state != states[state])):
                            continue
                        expected_url = f"https://{self.host}/{repo}/-/merge_requests/{mr.iid}"
                        if mr.iid <= 0 or mr.web_url != expected_url:
                            raise ValueError("invalid MR URL")
                        return PullRequest(
                            number=mr.iid, url=mr.web_url, head_branch=mr.source_branch,
                            state="open" if mr.state == "opened" else mr.state,
                            is_draft=bool(mr.draft or mr.work_in_progress),
                        )
                    next_page = response.headers.get("x-next-page", "")
                    if not next_page:
                        return None
                    next_number = int(next_page)
                    if not page < next_number <= 1000:
                        raise ValueError("invalid pagination")
                    page = next_number
                except (ValueError, TypeError, ValidationError):
                    raise GitHubError("GitLab returned an invalid merge request response") from None
