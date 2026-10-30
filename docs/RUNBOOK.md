# Runbook

Commands that need API access assume `engine.env` is sourced:
`set -a; . ~/.config/op-agent-engine/engine.env; set +a`.

## Status and heartbeat

```bash
op-agent-engine status            # or --json
op-agent-engine dashboard         # read-only web UI on 127.0.0.1:8765
op-agent-engine dashboard --port 9000   # or DASHBOARD_PORT in engine.env
```

The heartbeat (`DATA_DIR/heartbeat`) is written after every poll; a stale one
means the service is stopped or stuck — check
`journalctl -u op-agent-engine --since '10 min ago'`.

## Pause and resume

- Touch `DATA_DIR/PAUSE`: the next poll stops claiming; running runs finish.
  Delete the file to resume. No restart needed.
- `ENGINE_PAUSED=true` in `engine.env` plus restart for a persistent pause.
- Per project: `enabled: false`. Per ticket: move it to `On Hold`.

## Inspect a run

Every run has `DATA_DIR/runs/<runId>/` with `prompt.txt`, `stdout.log`,
`stderr.log` and `meta.json` (plan, state, outcome, transition, exit code,
**redacted** environment).

```bash
op-agent-engine status --json     # find the run id
RID=42
less ~/.local/share/op-agent-engine/runs/$RID/meta.json
tail -n 80 ~/.local/share/op-agent-engine/runs/$RID/stdout.log
journalctl -u op-agent-engine --since today | grep "\"run_id\": $RID"
```

Run history lives in `DATA_DIR/engine.db` (SQLite). The database is
disposable: deleting it loses history and crash counters, never workflow
integrity.

## Handle `On Hold`

The engine comment names the reason; moving a ticket out of `On Hold` is
always an explicit human decision.

| Comment | Meaning | Human action |
|---|---|---|
| `ENGINE: blocked` | agent reported a blocker | remove it, move to `Rejected` or `New` |
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

## Retention

`op-agent-engine cleanup` removes `DATA_DIR/runs/<id>` older than
`RUN_RETENTION_DAYS` (`--dry-run`, `--days`, `--json`). The daily
`op-agent-engine-cleanup.timer` runs it automatically. Without systemd, add a
cron entry calling `cleanup`.

## Security

- Ticket and PR content is untrusted input; the engine never executes it.
- Agents run with the service user's privileges; a dedicated Unix user is the
  recommended hardening step (strict systemd sandboxing can break toolchains).
- If branch protection is enabled later, give the reviewer a dedicated
  `GH_TOKEN`; otherwise the reviewer's merge is blocked by its own review.
- `OP_*`/`GH_TOKEN` values are redacted to `***` in logs and `meta.json`.
  Keep `engine.env` at mode `0600`.
