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
  ticket id, agent, kind, duration, outcome and PR link. The section lists
  only the **newest 8 tickets** in the snapshot (not the newest 8 sessions),
  so a ticket with many CI checkpoints cannot crowd newer tickets out; each
  listed ticket keeps every session the snapshot holds for it. Each row shows
  the newest started session (run ID breaks ties). The ↶ history button reveals
  earlier sessions for that ticket, newest first, and works with keyboard
  navigation. Expanded history stays open across automatic refreshes.
  A ticket with a live session is listed **only under Active sessions**; its
  finished runs are not repeated in Recent sessions. When the snapshot holds
  more than 8 finished tickets, the count reads
  `8 of N tickets · M sessions shown` and the tooltip explains the window and
  any tickets hidden because they are running. Active sessions and all-run
  totals are otherwise unchanged.
- **How are the totals trending?** Historical total, running, completed,
  declined and failed session counts, plus a breakdown of recorded outcomes (excluding
  aborted claim attempts and work-package transition races).

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

The browser omits outcomes beginning with `claim aborted:` or
`transition_aborted: Work package` (also `Transition Aborted: Work Package`,
case-insensitive, ignoring leading whitespace) from the outcome breakdown,
including existing records and new poll results. These aborted claims and
work-package transition races produce per-ticket messages that otherwise flood
the breakdown. No database cleanup is needed: existing rows disappear from the
breakdown when the updated dashboard loads.
This is presentation-only: stored records, JSON API data, headline totals,
agent cards and session history remain unchanged for diagnosis. Other errors,
including transition aborts without the `Work package` message prefix, remain
visible in the breakdown. This filter does not change engine transitions or
classify every transition abort as harmless.

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
`meta.json` of each run, plus the current work-package observation described
below. It never writes to the store, OpenProject or a
workspace, so it is safe to start, stop or refresh while a run is in flight.
If the database does not exist yet it renders an empty state.

## Recent tickets API

`GET /api/status` returns `recent_tickets`, an array of up to ten distinct
work packages not listed in `active_sessions`. Each entry has `work_package_id`
(the numeric OpenProject ID) and `sessions` (the newest ten finished runs for
that ticket, using the same fields as `recent_sessions`). Tickets are ranked
by their newest session's start time, with higher run ID breaking ties; each
history uses that same newest-first order. `sessions[0]` represents the ticket.
With no eligible history the array is empty. The `limit` parameter still bounds
`recent_sessions` only; it does not truncate ticket selection or ticket histories.
The current browser panel still reads `recent_sessions`.

## Current work-package counts API (backend prerequisite for OP#502)

`GET /api/status?limit=N` additionally returns `current_work_packages`, separate
from the unchanged historical `counts` and `agents[].counts` run totals:

```json
{
  "status": "fresh",
  "source": "https://openproject.example",
  "projects": ["demo"],
  "checked_at": "2026-11-16T17:00:01+00:00",
  "observed_at": "2026-11-16T17:00:00+00:00",
  "age_seconds": 12.0,
  "stale_after_seconds": 135.0,
  "blocked": 2,
  "on_hold": 0
}
```

The example is illustrative, not deployment data. Semantics:

- `blocked`: work packages currently in the configured **Blocked** status
  (waiting on dependencies). `on_hold`: work packages currently in configured
  **On Hold** (human intervention). Status IDs come from startup resolution,
  not hardcoded names or historical run outcomes.
- Scope is **all work packages in enabled monitored projects**, including
  human-assigned tickets and tickets without runs, regardless of AGENT or
  eligible type. Disabled/unmonitored projects are excluded. `projects` lists
  the engine's resolved identifiers (also for `OP_PROJECT=*`). Counts are
  independent of dashboard session `limit` and OpenProject pagination.
- The engine makes two filtered, read-only OpenProject collection requests
  (`pageSize=1`, using `total`) each loop cycle, including when paused or slots
  are full. This adds two lightweight requests to otherwise idle cycles.
  Dry runs do not collect or persist these observations.
- `observed_at` is the UTC start of the successful observation, `checked_at`
  is its completion/latest attempt, and `age_seconds` is computed by the
  dashboard. The two queries are not an atomic OpenProject transaction;
  concurrent status changes can appear in the following cycle.
- `status` is `fresh`, `stale` or `unavailable`. Counts are integers **only
  when fresh**, including genuine zero. After
  `max(2 * POLL_INTERVAL_SECONDS + 15, 45)` seconds, status becomes `stale`
  and **both counts are null**; observation timestamp/age remain visible.
- Missing/corrupt observations, missing resolution, source/project scope
  mismatch, invalid provider totals, or either request failing produce
  `unavailable` with **both counts null**, never partial totals or fake zero.
  An unsuccessful refresh replaces the previous observation; `observed_at`
  and `age_seconds` are then null, with `checked_at` recording the attempt.
  Before any usable observation, timestamps are null and `projects` is empty.
  Failures to persist leave the old observation to expire normally.
- Provenance is the configured OpenProject base URL and resolved scope. The
  dashboard reads `DATA_DIR/work-package-counts.json`, atomically replaced by
  the engine; it does not make remote requests, require provider credentials,
  or write data. No SQLite schema migration is needed. Removing the sidecar
  rolls back persisted observations; older engines leave it to expire.
- Consumers must display an unknown/stale indication, not coerce null to zero.
  Existing `counts.blocked` still means historical human-intervention run
  outcomes; it is not used by the headline cards.

### Blocked and On Hold cards

The **Blocked** card shows current tickets waiting on dependencies; **On Hold**
shows current tickets needing human intervention. Both use
`current_work_packages`, not cumulative session outcomes or recent-session
counts. Their scope is all enabled monitored projects, as described above.
A fresh zero is displayed as `0`. Missing, unavailable or stale observations
show `—` with an explicit unavailable/stale hint rather than a fabricated zero.
The hover description includes the last observation time when available.
Cached observations expire in the browser too; a failed dashboard update marks
both current counts unavailable until a successful update. Other session totals
remain visible from the last snapshot, with the existing update-failed banner.
Before the first response, the card area is empty; it never implies zero.
The seven cards wrap to three columns on tablets and two on mobile.

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
- Session totals (not the current Blocked/On Hold ticket cards) are computed
  from the SQLite run history, so they survive engine
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
  current snapshot, not the complete stored ticket history. The section is
  windowed to the newest 8 distinct tickets, each keeping all of its snapshot
  sessions; tickets older than that window are hidden until `--limit` grows
  the snapshot. `--limit` still limits sessions, not distinct tickets, so a
  ticket with many CI checkpoints consumes snapshot entries without adding a
  row. The displayed counts distinguish tickets from sessions, and expanded
  history labels the snapshot limit explicitly. Increase `--limit` (up to 500)
  to include more tickets and sessions. Tickets with a live session in
  `active_sessions` are filtered out of the finished list, so a ticket never
  appears in both panels; when that empties the list, an explicit empty state
  says so instead of showing stale rows. Window size, filtering and grouping
  are browser-only; the JSON API and stored run history are unchanged.
- The JSON API is `GET /api/status?limit=N`; `GET /healthz` returns
  `{"ok": true}` for uptime checks.
