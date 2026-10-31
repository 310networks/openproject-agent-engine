# Docker image internals

The engine runs as a container and Docker is the supported installation method:
the setup, the `.env` variables and the `config/` layout are documented in the
[root README](../README.md). This page covers how the image is built and what
happens inside it.

Every Docker artifact lives in `docker/`, and that directory is the whole
deployment: copy it to `/opt/op-agent-engine` (or anywhere else) and run
`docker compose up -d` in it. Nothing is built on the host — the image is
pulled from Docker Hub. Paths in this document are relative to the deployment
directory, which is `docker/` when you are looking at the repository.

## What is baked in versus mounted

| Baked into the image | In `.env` (host only) | In `config/` (read-only) | Named volumes (read-write) |
|---|---|---|---|
| Python 3.12, `git`, `gh`, `ripgrep`, `fd`, `op` | deployment values, limits, API tokens | `pi/auth.json`, `pi/settings.json`, `pi/skills/` — **your** files | `/home/agent/.pi/agent` (pi state, model cache, tools) |
| Node 22 and pi (`PI_VERSION`) | — | `gitconfig`, `opencode/*.json` — **your** files | `/home/agent/.local/share/op-agent-engine` (`DATA_DIR`) |
| `agents/*/AGENTS.md` (seeds the workspace volume) | — | — | `/home/agent/agents` (`WORKSPACE_ROOT`) |
| `prompts/` (`PROMPTS_DIR`) | — | — | `/home/agent/.local/share/opencode` (opencode credentials and sessions) |

`/config` is authoritative: the entrypoint copies those files into the writable
volumes on **every start**, so applying a change is never editing a live mount:

| Changed | Command |
|---|---|
| `config/pi/*`, `gitconfig`, `opencode/*` | `docker compose restart` |
| `.env` (tokens, limits, `AGENT`, project, repository) | `docker compose up -d` — compose must recreate the container; `restart` keeps the old environment |
| the image tag | `docker compose pull && docker compose up -d` |

## The entrypoint

`docker/entrypoint.sh` runs as `uid 10001` (`agent`) and, before `exec
op-agent-engine "$@"`:

1. refuses to start without `OP_BASE_URL`, `OP_PROJECT`, `GITHUB_REPO`,
   `AGENT` or `WORKSPACE_ROOT`, so a deployment that forgot one fails
   immediately with the variable named rather than inside a run;
2. copies `gitconfig`, `pi/{settings,auth}.json`, `pi/skills/` and
   `opencode/{opencode,auth}.json` into the volumes, skipping what is absent;
   `auth.json` files become `0600`;
3. warns — and continues — when `config/pi/auth.json` still holds
   `replace-with…` placeholders, so a fresh deployment says "put a real provider
   key here" instead of failing 30 minutes into a run.

Anything the entrypoint seeds is a copy: editing the file in a volume does not
survive the next start, editing `config/` does.

## Volumes

| Volume | Mount point | Holds |
|---|---|---|
| `agent-workspace` | `/home/agent/agents` | the seeded `agents/` tree and one clone per agent workspace |
| `engine-data` | `/home/agent/.local/share/op-agent-engine` | SQLite store, `runs/<id>/`, heartbeat, pause flag, lock |
| `pi-agent` | `/home/agent/.pi/agent` | pi sessions, logs, model cache, installed tools |
| `opencode-data` | `/home/agent/.local/share/opencode` | opencode credentials and sessions |

Every mount point exists in the image. That matters: Docker creates a fresh
named volume with the *image's* ownership for that path, so a mount point
missing from the image (or owned by root) makes the container unusable with
`Permission denied`.

## The dashboard

`docker compose up -d` also starts `op-agent-engine-dashboard` and publishes
`DASHBOARD_PORT` (8765) on `127.0.0.1` only. It has no authentication: keep it
on loopback or put it behind the reverse proxy. Override the bind address with
`DASHBOARD_BIND=0.0.0.0` only if a proxy on another host needs it. Keep
`DASHBOARD_PORT=8765` in `.env`, because that is the port the compose mapping
publishes.

## Images and releases

`.github/workflows/docker.yml` runs the test suite and publishes
[`kookster310/op-agent-engine`](https://hub.docker.com/r/kookster310/op-agent-engine)
when a push to `main` touches the image inputs (`docker/`, `src/`,
`prompts/`, `agents/`, `pyproject.toml`) and on `v*` tags. Published tags:
`<version>`, `latest` (main and release tags) and `sha-<short>`.

There is no supported local build path: `docker/` ships no build overlay, so
`docker compose up -d` can only pull. Building by hand for development still
works with the repository as the build context —
`docker build -f docker/Dockerfile -t kookster310/op-agent-engine:dev .` — but
that image is never what a deployment runs.

- **`docker/version.json` is the tag source.** It must equal
  `project.version` in `pyproject.toml` — `tests/test_docker.py` enforces that,
  so the image tag and `op-agent-engine --version` cannot disagree.
- **Secrets:** `DOCKERHUB_USERNAME` and `DOCKERHUB_TOKEN` (a Docker Hub access
  token with write scope).
- **Optional variables:** `PI_VERSION`, `GH_VERSION`, `OP_CLI_REF`,
  `INSTALL_OPENCODE` override the Dockerfile defaults.

Releasing:

```bash
$EDITOR docker/version.json pyproject.toml     # bump both to X.Y.Z
git commit -am "chore: release X.Y.Z"
git tag "vX.Y.Z" && git push origin main --tags
```

The release job fails if the `v*` tag disagrees with `docker/version.json`.
`workflow_dispatch` rebuilds and republishes the current version from `main`
without a new tag.

## Notes and limits

- **Repos and run logs live in volumes.** Inspect them with
  `docker compose exec engine ls runs/` or `docker cp`. `run_dir` contents are
  also in `runs/<id>/` under `DATA_DIR`, and the dashboard reads the store
  directly.
- **`down -v` deletes the clones.** They are rebuilt on the next run, which is
  slow for large repositories.
- **Symlinked skills are dereferenced.** If `~/.pi/agent/skills` links into a
  shared directory, `config-sync.sh` copies the contents; a link that points
  outside the tree would be dangling inside the container.
- **Secrets.** `config/pi/auth.json` is a `0644` copy so the container user can
  read it; the host's own `auth.json` stays `0600`. If you do not want a key on
  disk, use `config-sync.sh --no-auth` and provide the provider key by other
  means (for example an image built for your provider). `docker/.env` is `0600`
  and never mounted.
- **`docker/.env` is read on the host**, so it may stay `0600`; every file in
  `config/` must be world-readable because the container's own user reads it.
- **Snap-installed Docker cannot read `/tmp` or dot-directories**, so keep the
  deployment directory on a normal path such as `/opt/op-agent-engine` or
  `~/op-agent-engine`. `docker --env-file` fails with "permission denied"
  otherwise.
- **Which agent CLI runs** is `AGENT` in `.env` (`pi` or `opencode`), not
  something to set per agent: every agent uses the selected runner. Switching
  is `AGENT=opencode` followed by `docker compose up -d` (a changed env file
  needs a recreate), plus a rebuilt image when opencode was not installed yet
  (`INSTALL_OPENCODE=true`).
- **opencode** is opt-in (`INSTALL_OPENCODE=true`). Its `run` mode works in the
  container; a host wrapper that `attach`es to a remote opencode server does
  not, because the agent would then work on the *server's* filesystem instead
  of the container's checkout.
- The engine spawns one process group per run and kills it on timeout, so the
  container runs under `tini` and `stop_grace_period` is 90 s
  (`STOP_GRACE_SECONDS` + the runner's 20 s SIGKILL grace). Raise both together.
