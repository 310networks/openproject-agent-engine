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

## Pause and resume

- Touch `DATA_DIR/PAUSE`: the next poll stops claiming; running runs finish.
  Delete the file to resume. No restart needed.
- `ENGINE_PAUSED=true` in `.env` plus `docker compose up -d` for a persistent
  pause.
- Per ticket: move it to `On Hold`.

## Inspect a run

Every run has `DATA_DIR/runs/<runId>/` with `prompt.txt`, `stdout.log`,
`stderr.log` and `meta.json` (plan, state, outcome, transition, exit code,
**redacted** environment).

```bash
docker compose run --rm engine status --json   # find the run id
RID=42
docker compose exec engine cat runs/$RID/meta.json
docker compose exec engine tail -n 80 runs/$RID/stdout.log
docker compose logs --since 24h engine | grep "\"run_id\": $RID"
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

`cleanup` removes `DATA_DIR/runs/<id>` older than `RUN_RETENTION_DAYS`
(`--dry-run`, `--days`, `--json`). Nothing runs it on a schedule by itself, so
add one cron entry on the host:

```cron
0 4 * * * cd /opt/op-agent-engine && docker compose run --rm cleanup
```

## Security

- Ticket and PR content is untrusted input; the engine never executes it.
- Agents run as the image's own unprivileged user (`uid 10001`), never as the
  host user; nothing is mounted from the host, only named volumes.
- If branch protection is enabled later, give the reviewer a dedicated
  `GH_TOKEN`; otherwise the reviewer's merge is blocked by its own review.
- `OP_*`/`GH_TOKEN` values are redacted to `***` in logs and `meta.json`.
  Keep `docker/.env` at mode `0600`; it is read by compose on the host and is
  never mounted.
