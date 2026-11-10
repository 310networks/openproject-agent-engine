# Runbook

Commands that need API access assume `docker/.env` is sourced:
`set -a; . docker/.env; set +a` (or run them through `docker compose run`,
which passes `.env` to the container).

## Status and heartbeat

```bash
docker compose run --rm engine status          # or --json
docker compose logs -f engine                  # follow the engine
docker compose ps                              # what is up
```

The dashboard (read-only, <http://127.0.0.1:8765>) is part of the stack unless
the `dashboard` service is stopped; see [DASHBOARD.md](DASHBOARD.md).

The heartbeat (`DATA_DIR/heartbeat`) is written after every poll; a stale one
means the container is stopped or stuck — check
`docker compose logs --since 10m engine`.

## Configuration changes

- **Added or updated an OpenProject project** (`GIT_REPO`, `AGENT`, eligible
  types): restart the engine process so it resolves the project list again.

  ```bash
  docker compose restart engine
  ```

- **Changed `docker/.env`** (for example `OP_PROJECT`, `OP_AGENT_FIELD`,
  tokens): recreate the container; `restart` does **not** re-read `.env`.

  ```bash
  docker compose up -d engine
  ```

## Pause and resume

- Touch `DATA_DIR/PAUSE`: the next poll stops claiming; running runs finish.
  Delete the file to resume. No restart needed.
- `ENGINE_PAUSED=true` in `.env` plus `docker compose up -d` for a persistent
  pause.
- Per ticket: move it to `On Hold`.

## Inspect a run

Every run has `DATA_DIR/runs/<runId>/` with `prompt.txt`, `stdout.log`,
`stderr.log` and `meta.json` (plan, state, outcome, transition, exit code,
**redacted** environment). A **failed** run also has `worktree/` (see
[After a silent failure](#after-a-silent-failure)).

```bash
docker compose run --rm engine status --json   # find the run id
RID=42
docker compose exec engine cat runs/$RID/meta.json
docker compose exec engine tail -n 80 runs/$RID/stdout.log
docker compose logs --since 24h engine | grep "\"run_id\": $RID"
```

## After a silent failure

The expensive case is a run that exits normally (or times out) with an empty
`stdout.log`, no commit on `op-<project>-<wp>` and no PR: whatever the agent
wrote is the only evidence of what it was doing. The engine therefore captures
the failed worktree **before** it removes it, for every `nonzero_exit`,
`no_pr`, `timeout`, `pr_verification_error` and review failure.

Start with `meta.json`: `worktree_artefact` is the path of the capture (null
when nothing was captured) and `worktree_kept` says whether the real checkout
was left on disk.

```bash
RID=304
docker compose exec engine cat runs/$RID/meta.json | grep -E 'worktree|outcome'
docker compose exec engine ls -la runs/$RID/worktree
```

The artefact (`DATA_DIR/runs/<RID>/worktree/`) holds:

| File | Contents |
|---|---|
| `status.txt` | `git status --porcelain -uall`: every modified, staged, deleted and **untracked** path |
| `diff.patch` | `git diff --binary`: unstaged changes (binary changes included) |
| `diff_cached.patch` | `git diff --binary --cached`: staged changes |
| `commits.txt` / `commits.patch` | commits the agent made but never pushed, and their diff |
| `head.txt` | branch, worktree path, `HEAD` and the base the branch came from |
| `untracked/` | untracked files copied verbatim (a patch cannot express them) |
| `MANIFEST.txt` | index of the above, plus files skipped and why, plus capture errors |

Limits: untracked files up to 1 MiB each and 20 MiB in total are copied;
larger ones are listed in `MANIFEST.txt` instead. Capture never fails the run —
if git is unreadable, `MANIFEST.txt` records the error and the worktree is left
where it is.

While the retention window has not passed, the worktree itself is still there:

```bash
docker compose exec engine ls -la ws/<agent>/worktrees/   # op-8-144 may still be present
```

Apply the capture to a fresh checkout to resume the work by hand:

```bash
git checkout -b post-mortem origin/main
git apply --binary runs/$RID/worktree/diff_cached.patch
git apply --binary runs/$RID/worktree/diff.patch
tar -C runs/$RID/worktree/untracked -cf - . | tar -xf -
```

A worktree that was still dirty when a new run for the same branch started is
moved aside as `ws/<agent>/worktrees/op-<project>-<wp>.kept-<utc>` instead of
being deleted, so a resume run cannot destroy the previous run's state.

Retention: a kept worktree is expired by `cleanup` with the same
`RUN_RETENTION_DAYS` as its run directory, and a single kept worktree is
capped by `FAILED_WORKTREE_MAX_MB` (default 2048 MiB) so an installed
toolchain cannot fill the volume. `KEEP_FAILED_WORKTREES=false` removes the
worktree immediately after the capture; the artefact under `runs/<RID>/` is
always written. The `ENGINE: escalated` comment names the artefact path.

Run history lives in `DATA_DIR/engine.db` (SQLite). The database is
disposable: deleting it loses history and crash counters, never workflow
integrity.

## Handle `On Hold`

The engine comment names the reason; moving a ticket out of `On Hold` is
always an explicit human decision.

| Comment | Meaning | Human action |
|---|---|---|
| `ENGINE: blocked` | agent reported a blocker | remove it, move to `Rejected` or `New` |
| `ENGINE: blocked by dependency` | unfinished OpenProject blocker relation; ticket is in `Blocked` | finish/close the blocker; the engine unblocks and assigns automatically (within `BLOCKER_REFRESH_SECONDS`, default 5 min) |
| `ENGINE: no PR found` | `Ready for Review` without a PR for `op-<project>-<wp>` | push the PR, move back to `Ready for Review` |
| `ENGINE: escalated` | crash/timeout retry cap; comment has exit code + last 40 log lines | fix the cause, move back to `In Progress`/`New` |
| `ENGINE: escalated` with `(no_result_line)` | the run exited 0 and wrote nothing at all, so there is no RESULT line and no evidence | read `runs/<id>/meta.json`; the worktree is gone (#365), so re-run with a narrower scope |
| `ENGINE: rejection cap reached` | next rejection would exceed `MAX_REJECTIONS` | review the feedback, move to `Rejected`/`New` when ready |

Crash counters persist in `engine.db` and only ever incremented, so an
escalation used to consume the retry budget for good. The engine now restores
it by itself: when a ticket that has a counter is claimed and its newest status
change left `On Hold`, the counter is deleted before the run starts and the run
log records `reset the crash counter for <wp> (was N)`. The engine only ever
moves tickets *to* `On Hold`, so a move off it is a human saying the cause was
addressed; an engine escalation never resets itself.

The check runs at claim time, not while polling, so un-holding a ticket while
the engine was down works too. The manual `DELETE FROM crash_counts WHERE
work_package_id = <id>` is only needed when the history cannot be read (the
engine logs a warning and leaves the counter alone) or when the counter has to
be cleared without moving the ticket.

### Missing workflow skills (instruction/image mismatch)

A worker may report `BLOCKED` because its standard mandates an OpenProject or
GitHub PR skill that the container does not ship. This is not evidence of a
missing token: check the actual commands/results first. Successful ticket reads,
PR inspection or an own-user report show those particular operations worked;
they do not independently prove push/merge permission.

The standards and implement/resume templates now use the bundled `op`, `curl`,
`git`, `gh` and Python tools directly. They require no external skill files and
retain own-account credentials, engine-owned ticket fields, normal review and
branch-protection boundaries. Do not rotate tokens, grant administrator rights,
install host skills or repeatedly requeue to resolve an instruction-only problem.

Upgrade to the corrected released image and verify all six synced standards plus
the templates before returning the held ticket to `New` (or `Rejected` when an
existing PR needs a fix pass). If an authorized operator applies a temporary
instruction-only hotfix without a restart, back up every original, compare source
and destination hashes, update both the baked-source copy and synced workspace
standards, and verify the next run's rendered prompt. Record that this is **not**
a released image upgrade: recreation from an older image will revert it. Do not
interrupt active work or change tokens, branch state or crash history for this fix.

### `workspace_error` from an interrupted clone

A `workspace_error` before the agent starts means the clone under
`ws/<agent>/repositories/<repo>/` could not be prepared. Two shapes are worth
recognising:

| Escalation detail | Cause |
|---|---|
| `Unable to create '.../.git/index.lock': File exists` | a git command was killed while holding the index lock (engine restart, container stop, clone timeout) |
| checkout failures with `Updating files: N%` only, or a `.git` without `.git/index` | the clone was killed before its checkout finished; typical for the first clone of a repository that stores assets in Git LFS |

Both are handled by the engine now: a leftover `index.lock` is removed at the
start of every preparation, a clone without `.git/index` is deleted and cloned
from scratch, and a failed clone never leaves its half-written directory
behind. Cloning also gets its own timeout (`CLONE_TIMEOUT_SECONDS`, 30 min)
instead of the 300 s per-command timeout, because it downloads the whole
history and every LFS asset in one go.

If the same clone fails repeatedly, the cause is outside the ticket (network,
LFS 502s, repository permissions for the agent token). Read the run's
`stderr.log` and `meta.json` before requeueing: the error excerpt keeps the
`fatal:` line (progress output is dropped) and redacts the token, so it is safe
to paste into a comment. Requeueing without fixing the cause only consumes the
crash budget.

### Photosensitivity-policy conflicts

The VFX standard delegates effect-specific photosensitivity limits and approved
exceptions to the project's approved visual-accessibility specification. Without
a documented exception, the default remains three flashes in any one-second
period. This does not delegate general instruction precedence to ticket text.
Both VFX and reviewer standards require aggregate on-screen validation,
reduced-flash variants and the project's capture/certification gates. A permitted
small-area effect is not automatically safe in a composed scene.

When an old image has blocked a ticket on an unconditional flash cap:

1. Record the project approval and exact specification references in the ticket.
   Make standard/reduced behavior and tests explicit. For asset-only delivery,
   name the scene-integration owner and the pending safety acceptance gate;
   do not claim that a scene guard or certification already exists.
2. Leave the ticket `On Hold` while upgrading. Pause new claims and let active
   runs finish before recreating the engine (see [Pause and resume](#pause-and-resume)).
3. Pull the released image and recreate the stack: `docker compose pull &&
   docker compose up -d`. A mere process restart cannot update baked standards.
   The entrypoint refreshes both standards in the existing workspace volume.
4. Verify `docker compose exec engine op-agent-engine --version` and the synced
   `$WORKSPACE_ROOT/vfxArtist/AGENTS.md` and `codeReviewer/AGENTS.md` before
   resuming claims. If `AGENT_STANDARDS_DIR` is overridden, update that source too.
5. For a rejected PR that needs implementation fixes, move its ticket to
   `Rejected` to run the fix pass on the existing branch/PR. Do not send the
   unchanged PR straight to review, create a duplicate PR, or repeatedly requeue
   against the old policy.

The policy update resolves instruction conflicts; it does not waive independent
review, device testing or scene/shipping safety gates.

## Recovery and shutdown

On startup the engine takes the exclusive `DATA_DIR/engine.lock`, marks
`running` rows `interrupted` (not counted as crashes), kills leftover agent
process groups only when the recorded process start time still matches
(PID-reuse safe), and recovers `In Progress`/`In Testing` tickets as
`resume`/re-review runs. A clean `SIGTERM` stops polling, waits
`STOP_GRACE_SECONDS`, then terminates the remaining groups.

A resume run normally finishes without starting the agent when the branch
already has an open PR: the interrupted run had delivered its work. The
exception is an unanswered rejection. When the last review run rejected
*after* the last successful implement/fix run (a fix run was killed before it
pushed anything), the open PR predates the verdict, so the resume starts the
agent and the run log says so (`resuming <id> despite an open PR`). Rejection
timestamps come from `runs.finished_at`, not from comment text.

## Retention

`cleanup` removes `DATA_DIR/runs/<id>` and worktrees when they are older than
`RUN_RETENTION_DAYS` (`--dry-run`, `--days`, `--json`). Successful runs remove
their own worktree as soon as the outcome is verified; failed runs keep theirs
for the retention window (see [After a silent failure](#after-a-silent-failure)),
so `cleanup` expires both those and the leftovers of a crashed run. Worktrees
whose branch still has a `running` row in the store are never removed. Nothing runs `cleanup` on a schedule by itself,
so add one cron entry on the host:

```cron
0 4 * * * cd /opt/op-agent-engine && docker compose run --rm cleanup
```

## Security

- Ticket and PR content is untrusted input; the engine never executes it.
- Agents run as the image's own unprivileged user (`uid 10001`), never as the
  host user; nothing is mounted from the host, only named volumes.
- If branch protection requires a different approver, set
  `GH_TOKEN_REVIEWER` (and optionally `GIT_REVIEWER_NAME`/`_EMAIL`) to a second
  GitHub account. The reviewer then approves and merges as that account; with
  only `GH_TOKEN` its merge is blocked by its own review.
- `OP_*`/`GH_TOKEN*` values are redacted to `***` in logs and `meta.json`.
  Keep `docker/.env` at mode `0600`; it is read by compose on the host and is
  never mounted.
