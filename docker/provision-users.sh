#!/bin/sh
# Create the OpenProject accounts and collect one API token per account.
#
# Run this BEFORE `docker compose up -d`. It provisions the engine and agent
# users, shows each generated password once, then interviews you for one token
# per account (sign in at your OpenProject URL -> My account -> Access tokens).
# Tokens are written back into config/engine.env, so nothing has to be
# hand-edited and the engine starts with them already in place.
#
#   ./provision-users.sh                      guided run
#   ./provision-users.sh --reprompt-tokens    enter the tokens again
#
# Needs config/engine.env (OP_BASE_URL + OP_PROVISION_TOKEN) and
# config/workflow.yaml, plus the image (pulled if it is missing).
set -eu

# The deployment directory is this script's directory: docker-compose.yml and
# config/ live next to it.
deploy_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
cd "$deploy_dir"

config_dir="${deploy_dir}/config"
for file in "${config_dir}/engine.env" "${config_dir}/workflow.yaml"; do
  if [ ! -f "$file" ]; then
    echo "provision-users: missing $file" >&2
    exit 2
  fi
done

if docker compose ps --status running -q engine 2>/dev/null | grep -q .; then
  echo "note: the engine is already running. Tokens are read when a container is"
  echo "      created, so finish with 'docker compose up -d' (not 'restart')."
  echo
fi

image=$(docker compose config --images 2>/dev/null | head -n 1 || true)
if [ -n "${image:-}" ] && ! docker image inspect "$image" >/dev/null 2>&1; then
  echo "pulling $image"
  docker compose pull --quiet
fi

# config/engine.env is 0600 on the host and belongs to you, so the container's
# own user cannot write to it (and handing the container your UID is exactly
# what this design avoids). Work on a readable copy instead: it is mounted as
# /config for this one command, and the result is copied back with mode 0600.
work=$(mktemp -d "${deploy_dir}/provision-tmp.XXXXXX")
trap 'rm -rf "$work"' EXIT INT TERM
cp "${config_dir}/engine.env" "${config_dir}/workflow.yaml" "$work/"
chmod 0755 "$work"
chmod 0644 "$work/engine.env" "$work/workflow.yaml"

status=0
docker compose run --rm --no-deps \
  -v "${work}:/config" \
  --entrypoint op-agent-engine \
  engine provision-users "$@" || status=$?

if [ -s "${work}/engine.env" ]; then
  cp "${work}/engine.env" "${config_dir}/engine.env"
  chmod 0600 "${config_dir}/engine.env"
fi

exit "$status"
