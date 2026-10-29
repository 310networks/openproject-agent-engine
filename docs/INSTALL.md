# Installation

The engine is a Python package that installs a single `op-agent-engine`
command. Use a dedicated checkout of this repository, kept on `main`,
separate from any development clone: `WORKSPACE_ROOT` in `engine.env` points
at its `agents/` directory, and the live agents read
`agents/<workspace>/AGENTS.md` from that checkout. The checked-out branch is
the live standard for agents.

## Interactive installer (recommended)

```bash
git clone <engine-repo> ~/op-agent-engine && cd ~/op-agent-engine
cp workflow.example.yaml ./workflow.yaml
cp engine.env.example ./engine.env
chmod 600 ./engine.env
# edit workflow.yaml and engine.env, then:
./install.sh
```

Before running it, complete the one-time instance setup in
[docs/OPENPROJECT_SETUP.md](OPENPROJECT_SETUP.md) (statuses, `AGENT` field,
roles and workflows). In `engine.env`, set `OP_BASE_URL` and
`OP_PROVISION_TOKEN` (an administrator token); leave the per-account tokens
(`OP_ENGINE_TOKEN` and the `OP_TOKEN_*` values) as placeholders — the
installer collects them later.

`install.sh` requires `uv` and performs the rest in order:

1. plays a short terminal animation;
2. installs the package with `uv tool install --force .`;
3. copies your edited `workflow.yaml` and `engine.env` into
   `~/.config/op-agent-engine/` (existing files are backed up as
   `<name>.bak.<timestamp>`) and runs `op-agent-engine init`, which keeps the
   existing files;
4. runs `op-agent-engine provision --apply` to create the engine and agent
   users;
5. prints each newly created user's generated password, then prompts for
   that user's API token one at a time — the `agent-engine` runtime token and
   one per agent in `workflow.yaml` — verifying each through
   `/api/v3/users/me`; log in as the user and create the token under
   **My account → Access tokens**;
6. runs `op-agent-engine check-config`, installs the systemd service
   (`install-service --apply`, sudo), and finishes with a read-only
   `op-agent-engine dry-run`.

Tokens are written back to both `engine.env` copies with mode `600` and are
never printed. Re-running `./install.sh` is safe: identical config files are
left alone and changed ones are backed up first.

`op-agent-engine setup` is the same orchestration without the shell intro, if
you prefer to run it directly. Useful flags: `--skip-service` and `--yes`.

## Manual uv install

Use this if you want to run each step yourself.

```bash
git clone <engine-repo> ~/op-agent-engine && cd ~/op-agent-engine
uv tool install .
```

`op-agent-engine` lands in `~/.local/bin`. Then follow the manual steps in
[docs/CONFIG.md](CONFIG.md) and [docs/OPENPROJECT_SETUP.md](OPENPROJECT_SETUP.md):
`init`, edit the files, `provision --apply`, mint tokens, `check-config`,
`install-service --apply`, `dry-run`.

To update:

```bash
cd ~/op-agent-engine && git pull --ff-only
uv tool install --force .
sudo systemctl restart op-agent-engine
```

## Manual venv install (no uv required)

```bash
python3.12 -m venv ~/.local/share/op-agent-engine/venv
~/.local/share/op-agent-engine/venv/bin/pip install .
ln -s ~/.local/share/op-agent-engine/venv/bin/op-agent-engine \
      ~/.local/bin/op-agent-engine
```

## Scaffolding

`op-agent-engine init` creates `~/.config/op-agent-engine/` (mode `700`) with
`workflow.yaml` and `engine.env` (mode `600`), substituting your user and
home directory for the `<home>`/`<user>` placeholders. It is idempotent and
never overwrites existing files. The interactive installer copies your
checkout files first (applying the same placeholder substitution) and then
runs `init`, which keeps them.

## Systemd service

`op-agent-engine install-service` renders the three unit files with your real
paths and the resolved `op-agent-engine` binary, stages them in
`~/.local/share/op-agent-engine/systemd/`, and:

- without `--apply`: prints the `sudo` commands that would run;
- with `--apply`: runs `sudo tee`, `daemon-reload`, `systemd-analyze verify`
  and `systemctl enable --now` for `op-agent-engine.service` and
  `op-agent-engine-cleanup.timer` (the daily run-directory retention job).

The service autostarts on boot (`WantedBy=multi-user.target`).

Unit notes:

- `EnvironmentFile=` reads `engine.env`; `HOME` comes from `User=`, so
  `~/.pi`, `~/.gitconfig` and `gh` credentials resolve as in a normal login.
- The unit's `PATH` must contain every agent CLI and `gh`/`git`; systemd does
  not read shell profiles. Verified locations: `pi` in `~/.pi/agent/bin`,
  `opencode` in `~/.opencode/bin`. Add a Node version manager bin directory
  here if the agent CLIs are installed that way.
- `KillMode=control-group` kills the whole agent process tree on stop;
  `TimeoutStopSec=90` exceeds `STOP_GRACE_SECONDS` plus the runner's 20
  second kill grace.
- Optional watchdog: uncomment `Type=notify` and `WatchdogSec=` (three poll
  intervals) in the staged `op-agent-engine.service`, then re-run
  `install-service --apply`.
