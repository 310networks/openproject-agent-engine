# Acceptance testing

## Automated end-to-end (no OpenProject or GitHub needed)

```bash
.venv/bin/pytest tests/test_e2e.py
```

Drives one ticket from `New` to `Ready for Review` to `Closed` in two polls
using the real runner to spawn `tests/fakes/fake_agent.sh` with fake
OpenProject and GitHub backends. Also asserts that a configured `GH_TOKEN`
never appears in the run directory or SQLite history.

## Smoke-test with the fake agent

The fake agent (`tests/fakes/fake_agent.sh`) is a test fixture, not a
configuration option: the deployment's runners are fixed to `pi` and
`opencode` (selected by `AGENT`). `tests/test_runner.py` and
`tests/test_e2e.py` build a `WorkflowConfig` in memory and point its runner at
the script, which covers prompt rendering, process handling and result
parsing without a real agent.

The script does not push commits, open PRs or post comments, so the external
verifier rejects an unbacked success. Use the automated test for a full
cycle, or back the run with a real branch/PR when testing live.

## Manual acceptance checklist (real instance, throwaway repo)

- [ ] One-time OpenProject setup done ([OPENPROJECT_SETUP.md](OPENPROJECT_SETUP.md)).
- [ ] `./docker/provision-users.sh` asks for the deployment values and an
      administrator token, provisions the users, prints each generated password
      at that account's token prompt, prompts for one token per account, verifies
      each token, writes `docker/.env` and runs `check-config`.
- [ ] `docker compose up -d` starts `engine` and `dashboard`; the dashboard
      answers on `http://127.0.0.1:8765`.
- [ ] `docker compose down -v` stops the stack and deletes the volumes; the
      OpenProject accounts are removed or locked by hand in Administration.
- [ ] `check-config` resolves every status, user, project, type, role and
      `AGENT` option.
- [ ] `provision --apply` creates the five users and the four agent
      memberships; a second run makes no changes.
- [ ] `dry-run` logs a plausible plan and writes nothing.
- [ ] A `New` ticket with `AGENT=FRONTEND`: `once` claims it
      (`In Progress`), the agent pushes `op-<project>-<wp>`, and the ticket
      moves to `Ready for Review` only after an open PR exists.
- [ ] The reviewer claims it (`In Testing`); a merged review moves it to
      `Closed`.
- [ ] A rejecting review moves it to `Rejected`; the fix pass updates the
      same branch/PR. The fourth rejection moves it to `On Hold` with
      `ENGINE: rejection cap reached`.
- [ ] A missing PR on `Ready for Review` produces `On Hold` and
      `ENGINE: no PR found`.
- [ ] Kill the engine mid-run and restart: the ticket resumes; the
      interrupted run does not consume a crash retry.
- [ ] `PAUSE` stops new claims; `status` shows heartbeat, active runs and
      recent outcomes.
- [ ] `docker compose logs engine` and `DATA_DIR/runs/*` contain no token
      value.
