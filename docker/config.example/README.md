# Operator configuration: examples

`../config/` is where your configuration lives. It is mounted read-only at
`/config`, and the entrypoint copies the agent-side files from it into the
container's volumes on every start — so it is the source of truth, and applying
a change is `docker compose restart`.

Nothing in `../config/` is committed to the repository. These examples are, so
that the shape of each optional file is documented. Copy the ones you need:

```bash
cp docker/config.example/gitconfig       docker/config/
cp -r docker/config.example/pi           docker/config/
cp -r docker/config.example/opencode     docker/config/
```

| file | goes to | needed for |
|---|---|---|
| `gitconfig` | `docker/config/gitconfig` | committing as the agent (otherwise `git commit` fails with "Author identity unknown") |
| `pi/settings.json` | `docker/config/pi/settings.json` | pi preferences: default provider/model, thinking level, tools |
| `pi/auth.json` | `docker/config/pi/auth.json` | pi provider API keys (alternative: put `OPENROUTER_API_KEY` in `workflow.yaml`'s runner `env`) |
| `opencode/opencode.json` | `docker/config/opencode/opencode.json` | opencode model/provider config, only when the image is built with `INSTALL_OPENCODE=true` |
| (opencode credentials) | `docker/config/opencode/auth.json` | opencode provider keys; seeded to `~/.local/share/opencode/auth.json`, where opencode keeps them (or leave it out and pass keys as environment variables) |

Two more files are required and are **not** kept as examples here, because
their canonical form has to be rewritten for container paths:

```bash
./docker/config-sync.sh     # creates docker/config/workflow.yaml and engine.env
```

It seeds `workflow.yaml` from `workflow.example.yaml` and `engine.env` from
`engine.env.example`, with `WORKSPACE_ROOT`, `DATA_DIR` and `ENGINE_CONFIG`
pointing inside the image, and stages the pi/opencode configuration from your
own `~` if you already run the agents on this host. After the first run, edit
`docker/config/workflow.yaml` (your projects and agents) and
`docker/config/engine.env` (your OpenProject URL and tokens).

## Permissions

The container runs as its own unprivileged user (uid 10001), so it must be able
to *read* these files — no UID matching is involved:

- everything under `docker/config/` except `engine.env` must be world-readable
  (`0644`); `config-sync.sh` sets that for you. A file with `0600` will make the
  entrypoint skip it with a "seeded" line missing from the log.
- `engine.env` may stay `0600`: compose reads it as *you*, on the host, and
  passes it to the container as environment variables.

Never commit a populated `docker/config/`: `.gitignore` allows only the empty
skeleton, and the tokens in it are yours.
