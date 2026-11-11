"""Trusted provider-specific workflow instructions, separate from ticket data."""

from urllib.parse import quote


def gitlab_instructions(host: str, repo: str) -> str:
    api = f"https://{host}/api/v4/projects/{quote(repo, safe='')}"
    return f"""# Hosting provider: GitLab

GIT_HOST={host}; repository={repo}. GitHub-specific CLI/identity directions
in the standard do not apply to this deployment. Use git and the GitLab REST
API via curl (already installed), not gh or a GitHub skill. Keep the same
workflow, tests, ticket reporting and RESULT tokens. A PR means a GitLab merge
request (MR); RESULT: PR_OPENED must contain its web_url. APPROVED/REJECTED
also contain the MR web_url. Do not invent a new result token.

Authenticate requests with `-H "PRIVATE-TOKEN: $GITLAB_TOKEN"`; never print
or store the token in reports. Use only this environment identity. The reviewer
may have a dedicated token. Do not disable TLS validation or bypass protection.
Use `curl --fail-with-body --silent --show-error`, inspect HTTP/JSON errors,
and parse JSON using Python. Project API base: {api}

Implement/fix/resume: push the task branch with git; GET
`{api}/merge_requests?state=opened&source_branch=<URL-encoded-branch>` to
reuse its existing MR. Create only if none exists: POST `{api}/merge_requests`
with `--data-urlencode source_branch=<branch>`,
`--data-urlencode target_branch=<default-branch>`,
`--data-urlencode title=<OP#ticket-title>` and
`--data-urlencode description@/tmp/mr-description.md`. Update an existing MR
with PUT `{api}/merge_requests/<iid>` using the same description format as a PR.
The MR must not be a draft. Use its returned web_url in the ticket and result.

Review: GET `https://{host}/api/v4/user` and GET
`{api}/merge_requests/<iid>` to compare your user id to author.id. Inspect
`.../merge_requests/<iid>/diffs` (paginate), changes, pipelines, approvals and
mergeability; run the repository checks locally. If a different author, POST
`.../merge_requests/<iid>/approve` before merging when approving. Never approve
your own MR or bypass required approvals/checks. Rejections remain authoritative
OpenProject comments, exactly as in the standard. Merge with PUT
`.../merge_requests/<iid>/merge`, pinning `sha` to the reviewed head SHA and
using the repository's configured squash policy. An asynchronous/scheduled merge
is NOT a success: GET the MR again and require state=merged before reporting
APPROVED. Protection, conflicts, failed checks or missing permissions are not
permission to force a merge. All list endpoints must be paginated as needed.
"""
