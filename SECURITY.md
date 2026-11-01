# Security Policy

## Supported versions

The engine ships as a container image, `kookster310/op-agent-engine`. Only the
most recent release is supported: fixes go into a new tag, never into an older
one. The version in `docker/version.json` is the current release.

## Reporting a vulnerability

Do **not** open a public issue for a security problem.

Use GitHub's private reporting instead:
<https://github.com/310networks/openproject-agent-engine/security/advisories/new>

That opens a private advisory visible only to the maintainers. Please include:

- affected version (`docker/version.json` or the image tag),
- what an attacker can do and what they need to do it,
- reproduction steps or a proof of concept,
- any suggested fix.

If you cannot use the advisory form, open a public issue that says only that you
have a security report and ask for a private channel. Do not include details.

## What to expect

This is a small project maintained in spare time. Best effort, no SLA:

- acknowledgement, usually within a week,
- an assessment and a fix or mitigation plan once the report is triaged,
- credit in the advisory and release notes unless you ask otherwise.

Please give us a reasonable window to ship a fix before publishing details.

## Scope

In scope:

- the engine, dashboard and CLI in `src/`,
- the image, entrypoint and setup script in `docker/`,
- the workflows in `.github/workflows/`,
- prompt and agent templates that could be used to execute unintended commands.

A deployment is a privileged system by design: the engine holds an OpenProject
token, a GitHub token that can push and merge, and model provider keys, and it
runs coding agents that execute commands. Reports that assume an operator has
already exposed `.env`, shell access or the dashboard port are out of scope
unless the engine itself caused the exposure.

Out of scope:

- vulnerabilities in OpenProject, pi, `gh`, `op` or Docker themselves — report
  those upstream,
- the dashboard being reachable when an operator binds it beyond loopback or
  publishes it through a proxy; it has no authentication by design and
  `docs/DOCKER.md` says so,
- anything that requires an operator to add a malicious prompt or agent
  template to their own deployment.
