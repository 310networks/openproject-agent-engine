# Contributing

OpenProject Agent Engine is a public, reusable, project-agnostic orchestration
engine. Contributions must work without knowledge of a particular consumer's
project, organization or infrastructure.

## Public-content boundary

- Do not commit or publish information about other projects, private tickets,
  source code, specifications, internal URLs, operational logs or conversations.
- Do not include personal information, credentials, real user records, local
  account paths, private hostnames or network addresses in code, fixtures,
  screenshots, documentation, commit messages or pull-request discussions.
- Use minimal synthetic fixtures, fictional repository identifiers and reserved
  example domains. Verify that copied logs and generated files are sanitized;
  removing a name alone does not make private project material publishable.
- Review the complete outgoing diff and newly reachable commit history before
  pushing. Use an appropriate public or automation identity and a privacy-safe
  noreply address for commit metadata; do not expose a private email address.
- Keep consumer-specific behavior in configuration or repository-owned policy,
  not hard-coded repository names, workflow labels, domains or delivery rules.
  Optional integrations must preserve safe defaults for other consumers.

These rules apply to human and automated contributors. Public reviews must
contain only engine-relevant context. Use private reporting for vulnerabilities
as described in [SECURITY.md](SECURITY.md).

## Verification

Use the development dependencies declared in `pyproject.toml`, then run:

```sh
ruff check src tests
mypy
pytest
node --test tests/dashboard.test.cjs
```

Include exact results, migration implications and unverified items in the pull
request. Do not claim live deployment or behavior in a consumer environment
from unit-test results alone. Changes to CI, container files or dependencies
also require the review specified by `.github/CODEOWNERS`.
