#!/bin/sh
# Seed the writable agent state from the read-only /config mount, then exec the
# engine command.
#
# /config holds *copies* of the host's agent configuration (docker/config-sync.sh
# puts them there). It is mounted read-only and is authoritative: every start
# overwrites the agent-side config files with the ones found there, so applying
# a configuration change is `docker compose restart`, not editing a live mount.
# Anything the agents write themselves (sessions, logs, downloaded tools,
# model cache) stays in the named volume and is never touched here.
set -eu

umask 022

CONFIG_DIR=${CONFIG_DIR:-/config}
AGENT_DIR=${PI_CODING_AGENT_DIR:-${HOME}/.pi/agent}

log() { printf 'entrypoint: %s\n' "$*"; }
die() { printf 'entrypoint: ERROR: %s\n' "$*" >&2; exit 1; }

[ -f "${CONFIG_DIR}/workflow.yaml" ] || die \
  "missing ${CONFIG_DIR}/workflow.yaml -- add it to ./config (see docs/DOCKER.md) and restart"

: "${WORKSPACE_ROOT:?WORKSPACE_ROOT is not set (see config/engine.env)}"
: "${DATA_DIR:?DATA_DIR is not set (see config/engine.env)}"

mkdir -p "${AGENT_DIR}" \
         "${AGENT_DIR}/sessions" \
         "${AGENT_DIR}/logs" \
         "${WORKSPACE_ROOT}" \
         "${DATA_DIR}"

seed_file() {
  src=$1
  dst=$2
  mode=$3
  [ -f "${src}" ] || return 0
  install -m "${mode}" "${src}" "${dst}"
  log "seeded ${dst}"
}

seed_file "${CONFIG_DIR}/pi/settings.json" "${AGENT_DIR}/settings.json" 0600
seed_file "${CONFIG_DIR}/pi/auth.json" "${AGENT_DIR}/auth.json" 0600
seed_file "${CONFIG_DIR}/gitconfig" "${HOME}/.gitconfig" 0644

# Skills are a directory: replace it wholesale so a skill deleted on the host
# does not linger in the volume.
if [ -d "${CONFIG_DIR}/pi/skills" ]; then
  rm -rf "${AGENT_DIR}/skills"
  mkdir -p "${AGENT_DIR}/skills"
  cp -a "${CONFIG_DIR}/pi/skills/." "${AGENT_DIR}/skills/"
  log "seeded ${AGENT_DIR}/skills ($(find "${AGENT_DIR}/skills" -name SKILL.md | wc -l) skills)"
fi

if [ ! -f "${AGENT_DIR}/auth.json" ]; then
  log "WARNING: no pi credentials at ${CONFIG_DIR}/pi/auth.json -- runs will"
  log "         fail unless the agent CLI gets its key from the environment"
fi

exec op-agent-engine "$@"
