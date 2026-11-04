# Container image for the OpenProject agent engine.
#
# The engine itself is a plain Python package; the interesting part of this
# image is the agent runtime. The pi CLI and the tools it uses (git, gh, rg,
# fd, op) are baked in, so a run inside the container never depends on the
# host's installed agent.
#
# Build context is the repository root:
#   docker build -f docker/Dockerfile -t kookster310/op-agent-engine:latest .
# (published by CI only -- users pull).
#
# Nothing from the host is mounted. Deployment settings and provider keys are
# environment variables in docker/.env; everything the container writes lives in
# named volumes. That keeps UID/GID and $HOME out of the picture. See
# docs/DOCKER.md.

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

# Container defaults for the engine's path settings, so .env only has to hold
# the deployment values (URL, project, repository, tokens). They match the
# volume mounts in docker-compose.yml.
ENV WORKSPACE_ROOT=/home/agent/agents \
    DATA_DIR=/home/agent/.local/share/op-agent-engine \
    DASHBOARD_HOST=0.0.0.0 \
    DASHBOARD_PORT=8765

# ripgrep and fd back pi's grep/find tools (pi looks for `rg`, `fd` and
# `fdfind` on PATH); git/gh/op are invoked by the engine and by the agents;
# tini reaps the process groups the engine spawns.
#
# git-lfs is not optional: agent repositories track binary assets (*.wav,
# *.png, *.fbx) through it. Where a clone carries the `filter.lfs` config, a
# checkout of such a path dies with `git-lfs filter-process: git-lfs: not
# found`. Every run checks its branch out with `git worktree add`, so a missing
# binary fails the run before the agent ever starts (a workspace_error).
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      ca-certificates \
      curl \
      fd-find \
      git \
      git-lfs \
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

# gh: the engine uses `gh pr list` to verify outcomes, independently of pi.
RUN set -eu; \
    arch="$(dpkg --print-architecture)"; \
    curl -fsSL "https://github.com/cli/cli/releases/download/v${GH_VERSION}/gh_${GH_VERSION}_linux_${arch}.tar.gz" \
      | tar -xz --strip-components=1 -C /usr/local \
        "gh_${GH_VERSION}_linux_${arch}/bin/gh"; \
    gh --version

# The pi CLI. Pinning it makes container runs reproducible; the host's own pi
# install is never touched.
RUN npm install --global --no-fund --no-audit \
      "@earendil-works/pi-coding-agent@${PI_VERSION}" \
 && pi --version

COPY --from=opcli /out/op /usr/local/bin/op

# The engine package.
WORKDIR /app
COPY pyproject.toml README.md /app/
COPY src /app/src
RUN pip install --no-cache-dir . \
 && rm -rf /app

# Audio QA toolchain for the AUDIO role (music-as-code: LUFS/true-peak
# measurement, loop and stem-sum nulls, sub checks). Same argument as git-lfs:
# a run must not install its own tools. A per-run `pip install --user` into
# `$HOME/.local` is unpinned, lands outside every volume (so it is lost on the
# next image recreate) and burns minutes of the run budget on downloads -- the
# first audio QA run spent 10.5 of its 14.6 minutes on it and then stopped
# without committing (WP 144, run 304).
#
# Pinned to majors, installed system-wide as root so the runtime user inherits
# it. `soundfile` bundles libsndfile in its wheel (verified: 1.2.2), so no apt
# package for it.
RUN pip install --no-cache-dir \
      "numpy>=2,<3" \
      "scipy>=1.14,<2" \
      "soundfile>=0.13,<1" \
      "pyloudnorm>=0.2,<1"

COPY prompts /opt/op-agent-engine/prompts
COPY LICENSE /opt/op-agent-engine/LICENSE

# Fixed, unprivileged runtime user. The UID is arbitrary on purpose: no host
# file is ever mounted, so it only has to be consistent with the image.
RUN useradd --create-home --uid 10001 --user-group --shell /bin/bash agent

# The engineering standards are baked in twice on purpose:
#
# * /opt/op-agent-engine/agents is outside every volume. The entrypoint syncs
#   from it on every start, so an existing workspace volume receives the
#   standards of agents added by an upgrade too. Docker only seeds a named
#   volume when it is created; without this copy a deployment that predates
#   VFX/Audio would never get their AGENTS.md.
# * /home/agent/agents seeds a fresh workspace volume (Docker copies image
#   content into a new volume), and the entrypoint keeps it in sync.
COPY --chown=agent:agent agents/ /opt/op-agent-engine/agents/
COPY --chown=agent:agent agents/ /home/agent/agents/

COPY docker/entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod 0755 /usr/local/bin/docker-entrypoint.sh \
 && mkdir -p /home/agent/.pi/agent/bin \
             /home/agent/.pi/agent/sessions \
             /home/agent/.pi/agent/logs \
             /home/agent/.local/share/op-agent-engine \
 && chown -R agent:agent /home/agent

USER agent
WORKDIR /home/agent

# Smoke check at build time: pi must start as the runtime user and create its
# state directory there, not under /root.
RUN pi --version

ENTRYPOINT ["tini", "--", "/usr/local/bin/docker-entrypoint.sh"]
CMD ["run"]
