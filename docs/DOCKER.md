# Docker image internals

The engine runs as a container and Docker is the supported installation method:
the setup and the `.env` variables are documented in the
[root README](../README.md). This page covers how the image is built and what
happens inside it.

Every Docker artifact lives in `docker/`, and that directory is the whole
deployment: copy it to `/opt/op-agent-engine` (or anywhere else) and run
`docker compose up -d` in it. Nothing is built on the host — the image is
pulled from Docker Hub. Paths in this document are relative to the deployment
directory, which is `docker/` when you are looking at the repository.

## What is baked in versus configured

| Baked into the image | In `.env` (host only) | Named volumes (read-write) |
|---|---|---|
| Python 3.12, `numpy`, `scipy`, `soundfile`, `pyloudnorm`, `git`, `git-lfs`, `gh`, `ripgrep`, `fd`, `op` | deployment values, limits, API tokens, provider keys | `/home/agent/.pi/agent` (pi state, model cache, tools) |
| Node 22 and pi (`PI_VERSION`) | model, git identity | `/home/agent/.local/share/op-agent-engine` (`DATA_DIR`) |
| `agents/*/AGENTS.md` (at `/opt/op-agent-engine/agents`, synced into the workspace volume) | — | `/home/agent/agents` (`WORKSPACE_ROOT`) |
| `prompts/` (`PROMPTS_DIR`) | — | — |
| `LICENSE` (MIT, at `/opt/op-agent-engine/LICENSE`) | — | — |

Nothing is mounted from the host. Change `.env`, then recreate the stack:

| Changed | Command |
|---|---|
| `.env` (written by `agent-init.sh` plus the manual GitHub token(s): tokens, model, provider key, limits) | `docker compose up -d` — compose must recreate the container; `restart` keeps the old environment |
| the image tag | `docker compose pull && docker compose up -d` |

## The entrypoint

`docker/entrypoint.sh` runs as `uid 10001` (`agent`) and, before `exec
op-agent-engine "$@"`:

1. refuses to start without `OP_BASE_URL`, `OP_PROJECT`, `WORKSPACE_ROOT` or
   `DATA_DIR`, so a deployment that forgot one fails immediately with the
   variable named rather than inside a run;
2. creates the writable state directories in the volumes;
3. syncs the baked agent standards from `/opt/op-agent-engine/agents` into
   `$WORKSPACE_ROOT`, creating any missing agent workspace and replacing an
   old `AGENTS.md` with the image's version;
4. writes the agents' git identity from `GIT_AUTHOR_NAME`/`GIT_AUTHOR_EMAIL`
   (defaults: `OP_ENGINE_USER` and `OP_ENGINE_USER@OP_EMAIL_DOMAIN`);
5. registers the git-lfs filters globally (`git lfs install --skip-repo`), so
   a clone of an asset-tracking repository checks out the real `*.wav`,
   `*.png` or `*.fbx` rather than the LFS pointer file.

Provider credentials and model settings are environment variables in `.env`;
the entrypoint seeds no credential files.

## Volumes

| Volume | Mount point | Holds |
|---|---|---|
| `agent-workspace` | `/home/agent/agents` | the synced `agents/` standards and one clone per agent workspace |
| `engine-data` | `/home/agent/.local/share/op-agent-engine` | SQLite store, `runs/<id>/`, heartbeat, pause flag, lock |
| `pi-agent` | `/home/agent/.pi/agent` | pi sessions, logs, model cache, installed tools |

Every mount point exists in the image. That matters: Docker creates a fresh
named volume with the *image's* ownership for that path, so a mount point
missing from the image (or owned by root) makes the container unusable with
`Permission denied`.

Docker only seeds a named volume when it is created. An existing
`agent-workspace` therefore never receives files added by a later image, which
is why the entrypoint syncs the standards from `/opt/op-agent-engine/agents` on
every start. Pulling a new image and recreating the container is enough to add
the VFX/Audio standards to an old deployment and to refresh the others; the
clones and worktrees in the same volume are left alone. `AGENT_STANDARDS_DIR`
overrides the source directory (the default is the baked copy; it is not needed
in a normal `.env`).

## The dashboard

`docker compose up -d` also starts `op-agent-engine-dashboard` and publishes
`DASHBOARD_PORT` (8765) on `127.0.0.1` only. It has no authentication: keep it
on loopback or put it behind the reverse proxy. Override the bind address with
`DASHBOARD_BIND=0.0.0.0` only if a proxy on another host needs it. Keep
`DASHBOARD_PORT=8765` in `.env`, because that is the port the compose mapping
publishes.

## Images and releases

`.github/workflows/docker.yml` runs the test suite on every pull request that
touches the image inputs (`docker/`, `src/`, `prompts/`, `agents/`,
`pyproject.toml`) and publishes
[`kookster310/op-agent-engine`](https://hub.docker.com/r/kookster310/op-agent-engine)
when a push to `main` touches them, and on `v*` tags. Publishing is gated to
push events, so a pull request — including one from a fork — never reaches the
Docker Hub credentials. Published tags: `<version>`, `latest` (main and release
tags) and `sha-<short>`.

There is no supported local build path: `docker/` ships no build overlay, so
`docker compose up -d` can only pull. Building by hand for development still
works with the repository as the build context —
`docker build -f docker/Dockerfile -t kookster310/op-agent-engine:dev .` — but
that image is never what a deployment runs.

- **`docker/version.json` is the tag source.** It must equal
  `project.version` in `pyproject.toml` — `tests/test_docker.py` enforces that,
  so the image tag and `op-agent-engine --version` cannot disagree.
- **Review:** `.github/CODEOWNERS` names owners only for `.github/`, `docker/`
  and `pyproject.toml` — `@Kookster310` and `@KookyAgentReviewer`, so either
  can approve and merge. Everything else, including agent-authored pull
  requests, merges on the normal review rules.
- **Secrets:** `DOCKERHUB_USERNAME` and `DOCKERHUB_TOKEN` (a Docker Hub access
  token with write scope).
- **Optional variables:** `PI_VERSION`, `GH_VERSION`, `OP_CLI_REF` override
  the Dockerfile defaults.

Releasing — the docs no longer embed the version, so only these two files
change:

```bash
$EDITOR docker/version.json pyproject.toml     # bump both to X.Y.Z
pytest tests/test_docker.py                   # verify before pushing
git commit -am "chore: release X.Y.Z"
git tag "vX.Y.Z" && git push origin main --tags
```

Merge the bump first, then tag the merged `main` commit so the published image
matches a commit reachable from `main`. The release job fails if the `v*` tag
disagrees with `docker/version.json`.
`workflow_dispatch` rebuilds and republishes the current version from `main`
without a new tag.

## Notes and limits

- **Repos and run logs live in volumes.** Inspect them with
  `docker compose exec engine ls runs/` or `docker cp`. `run_dir` contents are
  also in `runs/<id>/` under `DATA_DIR`, and the dashboard reads the store
  directly.
- **`down -v` deletes the clones.** They are rebuilt on the next run, which is
  slow for large repositories.
- **Secrets.** Provider keys and API tokens live in `docker/.env`, which is
  read on the host by compose and never mounted. Keep it `0600`.
- **No host files are mounted.** Agent settings, provider credentials and git
  identity all arrive as environment variables.
- **Snap-installed Docker cannot read `/tmp` or dot-directories**, so keep the
  deployment directory on a normal path such as `/opt/op-agent-engine` or
  `~/op-agent-engine`. `docker --env-file` fails with "permission denied"
  otherwise.
- **The agent is pi.** `AGENT_MODEL` selects the model; there is no separate
  agent-CLI setting.
- **Audio QA tooling is baked in, like git-lfs.** `numpy`, `scipy`,
  `soundfile` (wheel bundles libsndfile 1.2.2) and `pyloudnorm` are installed
  system-wide, pinned to majors, for the AUDIO role's LUFS/true-peak and
  null-sum measurements. Do not have a run `pip install` into `$HOME/.local`:
  that is unpinned and invisible to every volume, so it is lost on the next
  image recreate and eats the run budget (run 304 spent 10.5 of 14.6 minutes
  on it). Version is not pinned exactly -- if an AUDIO ticket needs a specific
  behaviour, pin it in this layer and bump the image.
- The engine spawns one process group per run and kills it on timeout, so the
  container runs under `tini` and `stop_grace_period` is 90 s
  (`STOP_GRACE_SECONDS` + the runner's 20 s SIGKILL grace). Raise both together.
