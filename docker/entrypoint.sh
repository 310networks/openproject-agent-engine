#!/bin/sh
# Validate the deployment environment, prepare the writable state and exec the
# engine command.
#
# Everything deployment-specific is an environment variable (compose reads
# docker/.env); nothing is mounted from the host. Provider credentials for the
# agent CLIs are environment variables too, so this script does not seed
# auth.json, settings.json or an opencode config.
set -eu

umask 022

AGENT_DIR=${PI_CODING_AGENT_DIR:-${HOME}/.pi/agent}

# Fail at startup, with the variable named, instead of inside a poll or a run.
: "${OP_BASE_URL:?OP_BASE_URL is not set -- your OpenProject URL (see .env)}"
: "${OP_PROJECT:?OP_PROJECT is not set -- the OpenProject project identifier}"
: "${GITHUB_REPO:?GITHUB_REPO is not set -- the repository as owner/name}"
: "${AGENT:?AGENT is not set -- use pi or opencode}"
: "${WORKSPACE_ROOT:?WORKSPACE_ROOT is not set (see the image defaults)}"
: "${DATA_DIR:?DATA_DIR is not set (see the image defaults)}"

mkdir -p "${AGENT_DIR}" \
         "${AGENT_DIR}/sessions" \
         "${AGENT_DIR}/logs" \
         "${WORKSPACE_ROOT}" \
         "${DATA_DIR}"

# The agents commit from inside a run. Git reads its identity from the global
# config, so write one on every start from environment variables. There is no
# file to mount; the defaults name the automation account.
git config --global user.name "${GIT_AUTHOR_NAME:-${OP_ENGINE_USER:-op-agent-engine}}"
git config --global user.email \
  "${GIT_AUTHOR_EMAIL:-${OP_ENGINE_USER:-op-agent-engine}@${OP_EMAIL_DOMAIN:-agent-engine.local}}"

exec op-agent-engine "$@"
