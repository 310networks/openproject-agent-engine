# Installation

The engine is a Python package that installs a single `op-agent-engine`
command. Use a dedicated checkout of this repository, kept on `main`,
separate from any development clone: `WORKSPACE_ROOT` in `engine.env` points
at its `agents/` directory, and the live agents read
`agents/<workspace>/AGENTS.md` from that checkout. The checked-out branch is
the live standard for agents.

## Option 1: uv (recommended)

```bash
git clone <engine-repo> ~/op-agent-engine && cd ~/op-agent-engine
uv tool install .
```

`op-agent-engine` lands in `~/.local/bin`. To update:

```bash
cd ~/op-agent-engine && git pull --ff-only
uv tool install --force .
sudo systemctl restart op-agent-engine
```

## Option 2: venv (no uv required)

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
never overwrites existing files.

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
