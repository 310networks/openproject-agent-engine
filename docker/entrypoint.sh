#!/bin/sh
# Validate the deployment environment, prepare the writable state and exec the
# engine command.
#
# Everything deployment-specific is an environment variable (compose reads
# docker/.env); nothing is mounted from the host. Provider credentials for pi
# are environment variables too, so this script does not seed auth.json or
# settings.json.
set -eu

umask 022

AGENT_DIR=${PI_CODING_AGENT_DIR:-${HOME}/.pi/agent}

# Fail at startup, with the variable named, instead of inside a poll or a run.
: "${OP_BASE_URL:?OP_BASE_URL is not set -- your OpenProject URL (see .env)}"
: "${OP_PROJECT:?OP_PROJECT is not set -- one project identifier, a list, or *}"
: "${WORKSPACE_ROOT:?WORKSPACE_ROOT is not set (see the image defaults)}"
: "${DATA_DIR:?DATA_DIR is not set (see the image defaults)}"

mkdir -p "${AGENT_DIR}" \
         "${AGENT_DIR}/sessions" \
         "${AGENT_DIR}/logs" \
         "${WORKSPACE_ROOT}" \
         "${DATA_DIR}"

# Keep the per-agent standards in the workspace volume in step with the image.
# Docker seeds a named volume only when it is created, so an existing
# deployment would otherwise keep the old agent set after an upgrade (and lack
# the standards of agents added by the new release). Copy every regular file,
# not just AGENTS.md, so auxiliary standards travel with the agent as well.
standards_dir=${AGENT_STANDARDS_DIR:-/opt/op-agent-engine/agents}
if [ -d "$standards_dir" ]; then
  for source_dir in "$standards_dir"/*; do
    [ -d "$source_dir" ] || continue
    agent_name=${source_dir##*/}
    target_dir="${WORKSPACE_ROOT}/${agent_name}"
    mkdir -p "$target_dir"
    for source_file in "$source_dir"/*; do
      [ -f "$source_file" ] || continue
      file_name=${source_file##*/}
      # Engine and dashboard share the volume and start together, so replace
      # each file atomically instead of letting two copies truncate one file.
      temporary="${target_dir}/.${file_name}.$$"
      cp "$source_file" "$temporary"
      mv -f "$temporary" "${target_dir}/${file_name}"
    done
  done
fi

# The agents commit from inside a run. Git reads its identity from the global
# config, so write one on every start from environment variables. There is no
# file to mount; the defaults name the automation account.
git config --global user.name "${GIT_AUTHOR_NAME:-${OP_ENGINE_USER:-op-agent-engine}}"
git config --global user.email \
  "${GIT_AUTHOR_EMAIL:-${OP_ENGINE_USER:-op-agent-engine}@${OP_EMAIL_DOMAIN:-agent-engine.local}}"

exec op-agent-engine "$@"
