# Dashboard

A read-only web UI for watching the engine. It answers four questions at a
glance:

- **Is the engine alive and polling?** The status pill pulses green while the
  heartbeat is fresh, turns amber when the engine is paused, and red when the
  heartbeat is stale or missing. During graceful shutdown, `/api/status`
  reports engine status `draining` while the heartbeat stays fresh; the UI
  displays the humanized “Draining” label. A stale heartbeat still takes
  precedence over a leftover `DRAINING` marker after a hard kill.
- **Which agents are available?** Every agent in the fixed team, its
  runner/model, project scope, every current session (an agent can run more
  than one ticket at once) and last session.
- **What is running, and what ran before?** Active sessions plus the most
  recent finished sessions grouped into one row per OpenProject ticket, with
  ticket id, agent, kind, duration, outcome and PR link. Each row shows the
  newest started session (run ID breaks ties). The ↶ history button reveals
  earlier sessions for that ticket, newest first, and works with keyboard
  navigation. Expanded history stays open across automatic refreshes.
  Active sessions and all-run totals are unchanged.
- **How are the totals trending?** Total, running, completed, declined,
  blocked and failed counts, plus a breakdown of recorded outcomes (excluding
  aborted claim attempts).

The outcome breakdown distinguishes **No progress** (stored legacy outcome
`no_pr`) from the checkpoints: **Still In Progress** (`progress_not_finished`),
**Waiting On CI** (`waiting_ci`) and **Checkpointed**
(`checkpoint_no_progress`). The dashboard uses these labels in run history,
agent session summaries and the outcome breakdown; checkpoint outcome pills
are pink rather than failure red. A checkpoint is not a delivered result or a
failure: the JSON headline counts include all three in the separate
`progress_not_finished` bucket, excluded from both `completed` and `failed`.
Stored run history and JSON API outcome keys keep their original names.
`checkpoint_wait_cap_reached` (the ticket stopped converging) is shown with the
warning tone next to `rejection_cap_reached`.

The browser omits outcomes beginning with `claim aborted:` (case-insensitive)
from the outcome breakdown, including existing records and new poll results.
These cancelled claim attempts can occur during status-transition races before
an agent starts; their per-ticket messages otherwise flood the breakdown.
This is presentation-only: stored records, JSON API data, headline totals,
agent cards and session history remain unchanged for diagnosis. Other errors,
including `transition_aborted:`, remain visible in the breakdown.

## Run it

The `dashboard` compose service runs it, so `docker compose up -d` serves the
dashboard at <http://127.0.0.1:8765> with no extra step. It reads the same
`.env` and the same volumes as the engine:

```bash
docker compose up -d dashboard
docker compose logs -f dashboard
```

To run it once in the foreground instead:

```bash
docker compose run --rm --service-ports dashboard
# -> op-agent-engine dashboard: http://127.0.0.1:8765
```

Use `--host`, `--port`, `--limit` (recent sessions in the snapshot, max 500) or
`--refresh` (browser refresh in seconds) for one-off overrides; the same
defaults can be set with `DASHBOARD_HOST` and `DASHBOARD_PORT` in `docker/.env`.

## Changing the port

The port is a plain setting — no rebuild or code change is needed. Pick one
of these:

**Persistent (recommended):** edit `docker/.env`

```ini
DASHBOARD_PORT=9000
```

and publish that port instead of the default in `docker-compose.yml`, then
recreate the container (a changed `.env` needs more than `restart`):

```bash
docker compose up -d dashboard
# it is now at http://127.0.0.1:9000
```

**One-off / manual run:**

```bash
docker compose run --rm engine dashboard --port 9000
# or bind another interface as well
docker compose run --rm engine dashboard --host 0.0.0.0 --port 9000
```

`DASHBOARD_HOST` works the same way. Keep it on loopback unless a reverse proxy
needs it: the dashboard has no authentication.

**Publish a different host port instead** (keeps `.env` untouched): set
`DASHBOARD_BIND`/the left-hand side of the port mapping in
`docker-compose.yml`, e.g. `127.0.0.1:9000:8765`, and recreate the service.

If the port is already in use the dashboard exits with a clear bind error;
check `docker compose logs dashboard`. Remember to update the reverse-proxy
upstream as well if the dashboard is published through it.

The dashboard reads `DATA_DIR/engine.db`, `DATA_DIR/heartbeat` and the
`meta.json` of each run. It never writes to the store, OpenProject or a
workspace, so it is safe to start, stop or refresh while a run is in flight.
If the database does not exist yet it renders an empty state.

## Dashboard presentation tests

Run `node --test tests/dashboard.test.cjs` for dependency-free JavaScript tests
of outcome labels, tones, ticket grouping and history interactions, alongside the Python dashboard/API tests
(`pytest tests/test_dashboard.py`).

## Security

There is no login. The default bind address is loopback, which is enough for
an SSH tunnel:

```bash
ssh -L 8765:127.0.0.1:8765 <host>
# then open http://127.0.0.1:8765 locally
```

If it must be reachable on the LAN, bind it to the loopback interface and put
the authenticated reverse proxy in front of it. The API exposes ticket ids,
subjects, agent names, branches and PR URLs — no tokens, environment
variables or prompts.

## Data notes

- Run rows created before the dashboard milestone have no stored subject;
  they show the work-package id only. New rows carry the subject.
- Totals are computed from the SQLite run history, so they survive engine
  restarts but are lost if `engine.db` is deleted (the database is
  deliberately disposable).
- An agent card lists every running session for that agent. The JSON API
  exposes them as `agents[].current_sessions` (oldest first) and keeps
  `agents[].current_session` as the latest one for compatibility.
- Poll-time stale-run reconciliation removes orphaned rows from
  `engine.active_runs` and `active_sessions`. Their `recent_sessions[].state`
  is `interrupted`; `outcome` records `reconciled_dead_pid`,
  `reconciled_pid_reused` or `reconciled_finished_meta`. These are also included
  in `counts.outcomes`. No snapshot fields or endpoints have changed.
- Recent ticket rows and their history use only the finished sessions in the
  current snapshot, not the complete stored ticket history. `--limit` still
  limits sessions, not distinct tickets; repeated CI checkpoints can therefore
  reduce the number of tickets shown. The displayed counts distinguish tickets
  from sessions, and expanded history labels this limit explicitly. Increase
  `--limit` (up to 500) to include more sessions. Grouping is browser-only;
  the JSON API and stored run history are unchanged.
- The JSON API is `GET /api/status?limit=N`; `GET /healthz` returns
  `{"ok": true}` for uptime checks.
