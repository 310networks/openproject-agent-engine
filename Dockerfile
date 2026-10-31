# Container image for the OpenProject agent engine.
#
# The engine itself is a plain Python package; the interesting part of this
# image is the agent runtime. The agent CLIs (pi, optionally opencode) and the
# tools they use (git, gh, rg, fd, op) are baked in, so a run inside the
# container never depends on the host's installed agents.
#
# User configuration is *copied* into ./docker/config by config-sync.sh and
# mounted read-only at /config. Nothing from the host is mounted directly,
# which is what keeps UID/GID and $HOME out of the picture: every path in here
# belongs to the image's own user, and the files in ./docker/config are readable
# copies. See docs/DOCKER.md.

# --- OpenProject CLI (agents call `op` during a run) ------------------------
# Built from source: the released binary (v0.5.5) predates the `op
# work-package` interface the agent instructions rely on. The default ref is
# the commit the reference host runs; override with --build-arg OP_CLI_REF=v0.5.5.
FROM golang:1.25-bookworm AS opcli
ARG OP_CLI_REF=259f293a
ENV CGO_ENABLED=0 GOTOOLCHAIN=auto
# The module builds a binary named after the module; the agents call it `op`.
RUN GOBIN=/out go install github.com/opf/openproject-cli@${OP_CLI_REF} \
 && mv /out/openproject-cli /out/op

# --- Runtime ---------------------------------------------------------------
FROM python:3.12-slim-bookworm

ARG NODE_VERSION=22.23.3
ARG PI_VERSION=1.4.0
ARG GH_VERSION=2.80.0
# opencode is opt-in: its install downloads a ~180 MB platform binary and the
# engine's example workflow only uses the pi runner.
ARG INSTALL_OPENCODE=false
ARG OPENCODE_VERSION=1.18.4

ENV PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    LANG=C.UTF-8 \
    LC_ALL=C.UTF-8 \
    HOME=/home/agent \
    # pi writes a model-store cache; an unattended run never needs to phone
    # home for a version check.
    PI_SKIP_VERSION_CHECK=1 \
    # Prompt templates live outside the wheel (see prompts.py), so they are
    # installed to a fixed path instead of relying on the workspace layout.
    PROMPTS_DIR=/opt/op-agent-engine/prompts

# Container defaults for the engine's path settings, so a `config/engine.env`
# that only holds the OpenProject URL and tokens is enough. They match the
# volume mounts in docker-compose.yml; `env_file` overrides them if set.
ENV ENGINE_CONFIG=/config/workflow.yaml \
    WORKSPACE_ROOT=/home/agent/agents \
    DATA_DIR=/home/agent/.local/share/op-agent-engine \
    DASHBOARD_HOST=0.0.0.0 \
    DASHBOARD_PORT=8765

# ripgrep and fd back pi's grep/find tools (pi looks for `rg`, `fd` and
# `fdfind` on PATH); git/gh/op are invoked by the engine and by the agents;
# tini reaps the process groups the engine spawns.
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      ca-certificates \
      curl \
      fd-find \
      git \
      gnupg \
      less \
      openssh-client \
      procps \
      ripgrep \
      tini \
 && rm -rf /var/lib/apt/lists/* \
 && ln -sf /usr/bin/fdfind /usr/local/bin/fd

# Node is required by pi (its published package is a Node CLI).
RUN set -eu; \
    arch="$(dpkg --print-architecture)"; \
    case "$arch" in \
      amd64) node_arch=x64 ;; \
      arm64) node_arch=arm64 ;; \
      *) echo "unsupported architecture: $arch" >&2; exit 1 ;; \
    esac; \
    curl -fsSL "https://nodejs.org/dist/v${NODE_VERSION}/node-v${NODE_VERSION}-linux-${node_arch}.tar.gz" \
      | tar -xz --strip-components=1 -C /usr/local; \
    node --version; npm --version

# gh: the engine uses `gh pr list` to verify outcomes, independently of the
# agent CLIs.
RUN set -eu; \
    arch="$(dpkg --print-architecture)"; \
    curl -fsSL "https://github.com/cli/cli/releases/download/v${GH_VERSION}/gh_${GH_VERSION}_linux_${arch}.tar.gz" \
      | tar -xz --strip-components=1 -C /usr/local \
        "gh_${GH_VERSION}_linux_${arch}/bin/gh"; \
    gh --version

# The agent CLI itself. Pinning it makes container runs reproducible; the
# host's own pi install is never touched.
RUN npm install --global --no-fund --no-audit \
      "@earendil-works/pi-coding-agent@${PI_VERSION}" \
 && pi --version

RUN if [ "${INSTALL_OPENCODE}" = "true" ]; then \
      npm install --global --no-fund --no-audit "opencode-ai@${OPENCODE_VERSION}"; \
      opencode --version; \
    fi

COPY --from=opcli /out/op /usr/local/bin/op

# The engine package. `pip install .` also embeds the workflow/env examples and
# the systemd unit templates (see pyproject.toml force-include).
WORKDIR /app
COPY pyproject.toml README.md workflow.example.yaml engine.env.example /app/
COPY src /app/src
COPY deploy /app/deploy
RUN pip install --no-cache-dir . \
 && rm -rf /app

COPY prompts /opt/op-agent-engine/prompts

# Fixed, unprivileged runtime user. The UID is arbitrary on purpose: no host
# file is ever mounted, so it only has to be consistent with the image.
RUN useradd --create-home --uid 10001 --user-group --shell /bin/bash agent

# The engineering standard lives in the workspace volume; baking it in seeds a
# fresh volume on first use (Docker copies image content into new volumes).
COPY --chown=agent:agent agents/ /home/agent/agents/

COPY docker/entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod 0755 /usr/local/bin/docker-entrypoint.sh \
 && mkdir -p /home/agent/.pi/agent/bin \
             /home/agent/.pi/agent/sessions \
             /home/agent/.pi/agent/logs \
             /home/agent/.local/share/op-agent-engine \
             /home/agent/.local/share/opencode \
             /home/agent/.config/opencode \
 && chown -R agent:agent /home/agent

USER agent
WORKDIR /home/agent

# Smoke check at build time: pi must start as the runtime user and create its
# state directory there, not under /root.
RUN pi --version

ENTRYPOINT ["tini", "--", "/usr/local/bin/docker-entrypoint.sh"]
CMD ["run"]
