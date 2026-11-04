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
| `ENGINE: blocked by dependency` | unfinished OpenProject blocker relation; ticket is in `Blocked` | finish/close the blocker; the engine unblocks and assigns automatically |
| `ENGINE: no PR found` | `Ready for Review` without a PR for `op-<project>-<wp>` | push the PR, move back to `Ready for Review` |
| `ENGINE: escalated` | crash/timeout retry cap; comment has exit code + last 40 log lines | fix the cause, move back to `In Progress`/`New` |
| `ENGINE: rejection cap reached` | next rejection would exceed `MAX_REJECTIONS` | review the feedback, move to `Rejected`/`New` when ready |

Crash counters persist in `engine.db`; after fixing a failure, delete the
ticket's row from `crash_counts` to restore the full retry budget.

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
