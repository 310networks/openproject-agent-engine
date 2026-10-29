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

Point a runner at the script to test prompt rendering, process handling and
result parsing without a real agent:

```yaml
runners:
  fake:
    argv: ["/bin/sh", "<checkout>/tests/fakes/fake_agent.sh", "{prompt_file}"]
    cwd: "{repo_dir}"
    env:
      FAKE_AGENT_BEHAVIOR: "blocked"   # or unset: prompt-based approve/pr_opened
    inject_standard: false
```

The script does not push commits, open PRs or post comments, so the external
verifier rejects an unbacked success. Use the automated test for a full
cycle, or back the run with a real branch/PR when testing live.

## Manual acceptance checklist (real instance, throwaway repo)

- [ ] One-time OpenProject setup done ([OPENPROJECT_SETUP.md](OPENPROJECT_SETUP.md)).
- [ ] `./install.sh` (with `workflow.yaml` and `engine.env` in the checkout
      root) installs the tool, provisions the users, prints each generated
      password, prompts for one token per account, verifies each token,
      installs the service and runs a dry poll.
- [ ] `./uninstall.sh --dry-run` lists the service, config/data paths and
      users; `./uninstall.sh` removes them and deletes or locks the users
      (the admin token owner is skipped).
- [ ] `check-config` resolves every status, user, project, type, role and
      `AGENT` option.
- [ ] `provision --apply` creates the five users and memberships; a second
      run makes no changes.
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
- [ ] `journalctl -u op-agent-engine` and `DATA_DIR/runs/*` contain no token
      value.
