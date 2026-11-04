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
temporary=
cleanup_temporary() {
  if [ -n "$temporary" ]; then
    rm -f "$temporary"
  fi
}
trap cleanup_temporary EXIT
trap 'exit 1' HUP INT TERM

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
      # Engine, dashboard and one-shot containers start together and share
      # this volume, each in its own PID namespace. A name derived from $$ is
      # therefore not unique across them: use mktemp in the target directory
      # so every writer gets its own file, then replace the standard
      # atomically. The trap removes an unfinished temporary file on failure.
      temporary=$(mktemp "${target_dir}/.${file_name}.XXXXXX")
      cp -p "$source_file" "$temporary"
      mv -f "$temporary" "${target_dir}/${file_name}"
      temporary=
    done
  done
fi

# The agents commit from inside a run. Git reads its identity from the global
# config, so write one on every start from environment variables. There is no
# file to mount; the defaults name the automation account.
git config --global user.name "${GIT_AUTHOR_NAME:-${OP_ENGINE_USER:-op-agent-engine}}"
git config --global user.email \
  "${GIT_AUTHOR_EMAIL:-${OP_ENGINE_USER:-op-agent-engine}@${OP_EMAIL_DOMAIN:-agent-engine.local}}"

# Repositories that track binary assets declare git-lfs filters in
# .gitattributes. An unregistered filter is silently ignored, so a clone would
# check the pointer files out as if they were the assets and an agent would
# read a 130-byte text file instead of a .wav or .png. Register the filters for
# every clone in this container; --skip-repo writes the global config only and
# does not install hooks into whatever directory the container starts in.
git lfs install --skip-repo >/dev/null

exec op-agent-engine "$@"
