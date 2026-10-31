# Docker

Runs the engine in a container with the agent CLIs baked into the image. The
host is only used to *supply configuration*: everything lives in `docker/`,
and `docker/config/` is mounted read-only at `/config`. Nothing else from the host is mounted, so no UID/GID matching and no
home-directory layout is involved — the container runs as its own user (`uid
10001`) and the whole compose directory can be copied to another machine as-is.

## What is baked in versus mounted

| Baked into the image | In `docker/config/` (read-only) | Named volumes (read-write) |
|---|---|---|
| Python 3.12, `git`, `gh`, `ripgrep`, `fd`, `op` | `workflow.yaml` (yours; a checkout can seed it from `workflow.example.yaml`) | `/home/agent/agents` (`WORKSPACE_ROOT`) |
| Node 22 and pi (`PI_VERSION`) | `engine.env` (seeded from `engine.env.example`, read by compose, never mounted) | `/home/agent/.local/share/op-agent-engine` (`DATA_DIR`) |
| `agents/*/AGENTS.md` (seeds the workspace volume) | `pi/auth.json`, `pi/settings.json`, `pi/skills/` — **your** files | `/home/agent/.pi/agent` (pi state, model cache, tools) |
| `prompts/` (`PROMPTS_DIR`) | `gitconfig`, `opencode/*.json` — **your** files | `/home/agent/.local/share/opencode` (opencode credentials and sessions) |

Your configuration is never part of the repository. Only the empty skeleton
(`docker/config/{,pi/,pi/skills/,opencode/}`, each with a `.gitkeep`) is
committed; `.gitignore` allow-lists the skeleton and ignores everything else,
because the rest is a copy of your credentials, settings, skills and git
identity. `docker/config.example/` is the committed, non-personal counterpart:
it documents the shape of each optional file (`pi/settings.json`,
`pi/auth.json`, `opencode/opencode.json`, `gitconfig`) so you can fill them in
without guessing.

`docker/config-sync.sh` does that filling for you when you have the repository
checked out: it creates the tree, seeds `workflow.yaml` and `engine.env` from
the same examples a systemd install uses (with the paths rewritten to container
paths and every token left as a placeholder), stages
`~/.pi/agent/{auth.json,settings.json,skills}`, `~/.gitconfig` and
`~/.config/opencode/*`, and makes everything world-readable — except
`engine.env`, which stays `0600` because compose reads it on the host.

`/config` is authoritative: the entrypoint copies those files into the writable
volumes on **every start**, so applying a change is never editing a live mount:

| Changed | Command |
|---|---|
| `docker/config/workflow.yaml`, `pi/*`, `gitconfig`, `opencode/*` | `docker compose restart` |
| `docker/config/engine.env` (tokens, limits) | `docker compose up -d` — compose must recreate the container; `restart` keeps the old environment |
| the image tag | `docker compose pull && docker compose up -d` |

## What you supply

The image is published on Docker Hub, so the deployment never builds anything.
A directory holding `docker-compose.yml` and `docker/` is the whole deployment:

```
op-agent-engine/
├── docker-compose.yml
├── docker-compose.build.yml   only for building the image yourself
└── docker/
    ├── provision-users.sh     create the OpenProject accounts and tokens
    ├── config-sync.sh         stage your ~ into config/ (checkout only)
    ├── config.example/        committed examples, copy what you need
    │   ├── gitconfig
    │   ├── pi/{settings,auth}.json
    │   └── opencode/opencode.json
    └── config/                yours, never committed
        ├── engine.env         required  OP_BASE_URL + tokens (mode 0600)
        ├── workflow.yaml      required  project, repository, agents
        ├── gitconfig          recommended  identity for agent commits
        ├── pi/
        │   ├── auth.json      provider credentials (or keys via env)
        │   ├── settings.json  optional  default provider/model
        │   └── skills/        optional  skills available to every run
        └── opencode/
            └── opencode.json  optional  only with INSTALL_OPENCODE=true
```

`engine.env`, `workflow.yaml`, `pi/*` and `opencode/*` are never part of the
image and never committed anywhere — they are yours.

## Quick start

```bash
mkdir -p ~/op-agent-engine/docker/config/pi && cd ~/op-agent-engine
base=https://raw.githubusercontent.com/310networks/openproject-agent-engine/main
curl -fsSLO "$base/docker-compose.yml"
curl -fsS --create-dirs -o docker/provision-users.sh "$base/docker/provision-users.sh"
chmod +x docker/provision-users.sh

# engine.env: only the OpenProject URL and the tokens are required. The paths
# (ENGINE_CONFIG, WORKSPACE_ROOT, DATA_DIR, DASHBOARD_*) already default to the
# container mounts inside the image.
cat > docker/config/engine.env <<'EOF'
OP_BASE_URL=https://openproject.example.com
OP_PROVISION_TOKEN=replace-with-an-administrator-api-token
OP_ENGINE_TOKEN=replace-with-agent-engine-api-token
OP_TOKEN_FRONTEND=replace-with-frontend-api-token
OP_TOKEN_BACKEND=replace-with-backend-api-token
OP_TOKEN_DESIGNER=replace-with-designer-api-token
OP_TOKEN_REVIEWER=replace-with-reviewer-api-token
EOF
chmod 600 docker/config/engine.env

# workflow.yaml: the same document a systemd install uses (see CONFIG.md).
curl -fsS -o docker/config/workflow.yaml "$base/workflow.example.yaml"

# Agent configuration: copy the examples and edit them, or take your own files.
cp -r docker/config.example/pi docker/config.example/opencode docker/config/ 2>/dev/null || true
cp ~/.gitconfig docker/config/gitconfig                     # optional but recommended

docker compose run --rm --no-deps check-config   # validate before starting

docker compose pull
docker compose up -d
docker compose logs -f engine
```

The example files under `docker/config.example/` are not in the image and are
not fetched above: `pi/settings.json` and `pi/auth.json` are optional (provider
keys can also come from `workflow.yaml`'s runner `env`), and the point of the
examples is the shape, not the content. From a checkout,
`./docker/config-sync.sh` stages all of it for you: it creates the tree, seeds
the two required files with container paths and copies
`~/.pi/agent/{auth.json,settings.json,skills}`, `~/.gitconfig` and
`~/.config/opencode/*`.

## Creating the users and tokens

Users and tokens are created by a script that runs **before** the engine
container, so you can take as long as you need to sign in and mint each token.
Do the one-time instance setup first (statuses, `AGENT` custom field, Agent
Worker role): [OPENPROJECT_SETUP.md](OPENPROJECT_SETUP.md).
It needs only `OP_BASE_URL` and `OP_PROVISION_TOKEN` (an administrator's token)
in `docker/config/engine.env`; leave the `OP_TOKEN_*` values as placeholders.

```bash
./docker/provision-users.sh
```

It does, in order:

1. creates `agent-engine` plus one user per agent in `workflow.yaml`;
2. shows each **generated password once**, at that account's token prompt —
   store it if you may need to sign in again (passwords are never written to
   disk; accounts that already exist keep theirs, so reset them under
   **Administration → Users** instead);
3. warns you to make `agent-engine` an administrator (**Administration → Users
   → agent-engine → Administrator**) — the engine needs that to list users and
   edit work packages;
4. interviews you for one token per account (mint it at
   `<OP_BASE_URL>/my/access_tokens`) and verifies each against
   `/api/v3/users/me` — OpenProject does not let an administrator create tokens
   for other users, so each one has to be minted while signed in as that user;
5. writes the tokens into `docker/config/engine.env` (mode `0600`) and runs
   `check-config`.

Then start the stack:

```bash
docker compose up -d
```

`up -d`, not `restart`: a changed env file is only picked up when the container
is recreated. Re-running the script is safe — valid stored tokens are reused
(`--reprompt-tokens` asks again) — which makes it the way to fix a token that
turned out to belong to the wrong account.

The script runs the same code as `install.sh` does on a systemd host
(`op-agent-engine provision-users` inside the image), so the accounts, the
one-time passwords and the verification are identical. For a non-interactive
run, `docker compose run --rm provision --apply` provisions without the
interview.

Provider credentials for the agent CLI are separate from all of this: either
`docker/config/pi/auth.json` (see `docker/config.example/pi/auth.json` for the
shape) or provider keys in the runner's `env:` block in
`docker/config/workflow.yaml`.

## Building the image locally

`docker-compose.yml` only pulls. To build from a checkout (engine changes,
`prompts/`, `agents/*/AGENTS.md`), overlay the build file:

```bash
docker compose -f docker-compose.yml -f docker-compose.build.yml up -d --build
```

One-time OpenProject setup (statuses, `AGENT` field, Agent Worker role, admin
engine user) is the same as for any other install: see
[OPENPROJECT_SETUP.md](OPENPROJECT_SETUP.md). `config-sync.sh` seeds
`docker/config/workflow.yaml` from `workflow.example.yaml` and
`docker/config/engine.env`
from `engine.env.example` (with container paths); neither is ever overwritten
afterwards. Everything else in `docker/config/` comes from your own host and is
only copied when present — a fresh `docker/config/` is just the directories
plus those two files.

`config-sync.sh --check` reports drift without writing (exit code 1 when out of
date), which is useful in a deploy script or CI.

## The dashboard

`docker compose up -d` also starts `op-agent-engine-dashboard` and publishes
`DASHBOARD_PORT` (8765) on `127.0.0.1` only. It has no authentication: keep it
on loopback or put it behind the reverse proxy. Override the bind address with
`DASHBOARD_BIND=0.0.0.0` only if a proxy on another host needs it. Keep
`DASHBOARD_PORT=8765` in `engine.env`, because that is the port the compose
mapping publishes.

## Maintenance

```bash
docker compose run --rm provision --apply   # create/refresh OpenProject users
docker compose run --rm check-config        # validate config without polling
docker compose run --rm cleanup             # retention sweep (replaces the timer)
docker compose restart                      # after editing workflow.yaml or pi/*
docker compose up -d                        # after editing engine.env
docker compose down                         # stop; volumes survive
docker compose down -v                      # stop and delete all state (re-clones repos)
```

Schedule cleanup on the host instead of the systemd timer:

```cron
0 4 * * * cd /opt/op-agent-engine && docker compose run --rm cleanup
```

To upgrade the engine, pull a newer release: `docker compose pull && docker
compose up -d`, optionally pinning `OP_AGENT_ENGINE_TAG=0.1.0` in the shell or a
`.env` file next to `docker-compose.yml`. To upgrade the
*agent* tooling itself, change `PI_VERSION` (or `GH_VERSION`, `OP_CLI_REF`,
`INSTALL_OPENCODE=true`) and rebuild locally, or push the new value as a
repository variable so the published image carries it. `op` is built from
`OP_CLI_REF`, defaulting to the commit whose `op work-package` interface the
agent instructions rely on.

## Images and releases

`.github/workflows/docker.yml` runs the test suite and publishes
[`kookster310/op-agent-engine`](https://hub.docker.com/r/kookster310/op-agent-engine)
when a push to `main` touches the image inputs (`Dockerfile`, `docker/`,
`src/`, `prompts/`, `agents/`, `pyproject.toml`) and on `v*` tags. Published
tags: `<version>`, `latest` (main and release tags) and `sha-<short>`.

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
- **Secrets.** `docker/config/pi/auth.json` is a `0644` copy so the container user can
  read it; the host's own `auth.json` stays `0600`. Prefer
  `config-sync.sh --no-auth` plus the provider key in the runner `env` block
  (the engine only forwards `PATH`, `HOME`, `USER`, `LOGNAME`, `TZ`, `LANG` and
  `LC_ALL` to the agent, so add the key to the runner's `env:` block in
  `docker/config/workflow.yaml`) if you do not want a key on disk.
  `docker/config/engine.env`
  is `0600` and never mounted.
- **Snap-installed Docker cannot read `/tmp` or dot-directories**, so keep the
  compose directory (and `./config`) on a normal path such as
  `/opt/op-agent-engine` or `~/op-agent-engine`. `docker --env-file` fails with
  "permission denied" otherwise.
- **opencode** is opt-in (`INSTALL_OPENCODE=true`). Its `run` mode works in the
  container; a host wrapper that `attach`es to a remote opencode server does
  not, because the agent would then work on the *server's* filesystem instead
  of the container's checkout.
- The engine spawns one process group per run and kills it on timeout, so the
  container runs under `tini` and `stop_grace_period` is 90 s
  (`STOP_GRACE_SECONDS` + the runner's 20 s SIGKILL grace). Raise both together.
