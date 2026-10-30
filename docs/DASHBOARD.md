# Dashboard

A read-only web UI for watching the engine. It answers four questions at a
glance:

- **Is the engine alive and polling?** The status pill pulses green while the
  heartbeat is fresh, turns amber when the engine is paused, and red when the
  heartbeat is stale or missing.
- **Which agents are available?** Every agent from `workflow.yaml`, its
  runner/model, project scope, current session (if any) and last session.
- **What is running, and what ran before?** Active sessions plus the most
  recent finished sessions, with ticket id, agent, kind, duration, outcome
  and PR link.
- **How are the totals trending?** Total, running, completed, declined,
  blocked and failed counts, plus a breakdown of every recorded outcome.

## Run it

```bash
# installed config and data dir from engine.env
op-agent-engine dashboard
# -> op-agent-engine dashboard: http://127.0.0.1:8765
```

`install-service --apply` also installs and starts
`op-agent-engine-dashboard.service`, so a normal install serves it
automatically. Use `--host`, `--port`, `--limit` (recent sessions in the
snapshot, max 500) or `--refresh` (browser refresh in seconds) for one-off
overrides; the same defaults can be set with `DASHBOARD_HOST` and
`DASHBOARD_PORT` in `engine.env`.

## Changing the port

The port is a plain setting — no rebuild or code change is needed. Pick one
of these:

**Persistent (recommended):** edit `~/.config/op-agent-engine/engine.env`

```ini
DASHBOARD_PORT=9000
```

then restart the dashboard service so systemd re-reads the environment file:

```bash
sudo systemctl restart op-agent-engine-dashboard
# it is now at http://127.0.0.1:9000
```

**One-off / manual run:**

```bash
op-agent-engine dashboard --port 9000
# or bind another interface as well
op-agent-engine dashboard --host 0.0.0.0 --port 9000
```

The interactive installer and `op-agent-engine init` both write
`DASHBOARD_PORT=8765` into `engine.env`, so the value is visible where the
rest of the service settings live. `DASHBOARD_HOST` works the same way.

**Systemd drop-in alternative** (keeps `engine.env` untouched):

```bash
sudo systemctl edit op-agent-engine-dashboard
# add:
# [Service]
# Environment=DASHBOARD_PORT=9000
sudo systemctl restart op-agent-engine-dashboard
```

If the port is already in use the dashboard exits with a clear bind error;
check `journalctl -u op-agent-engine-dashboard -n 20`. Remember to update the
reverse-proxy upstream as well if the dashboard is published through it.

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
