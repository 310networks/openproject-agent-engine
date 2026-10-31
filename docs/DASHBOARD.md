# Dashboard

A read-only web UI for watching the engine. It answers four questions at a
glance:

- **Is the engine alive and polling?** The status pill pulses green while the
  heartbeat is fresh, turns amber when the engine is paused, and red when the
  heartbeat is stale or missing.
- **Which agents are available?** Every agent in the fixed team, its
  runner/model, project scope, current session (if any) and last session.
- **What is running, and what ran before?** Active sessions plus the most
  recent finished sessions, with ticket id, agent, kind, duration, outcome
  and PR link.
- **How are the totals trending?** Total, running, completed, declined,
  blocked and failed counts, plus a breakdown of every recorded outcome.

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
- The JSON API is `GET /api/status?limit=N`; `GET /healthz` returns
  `{"ok": true}` for uptime checks.
