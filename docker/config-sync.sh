#!/bin/sh
# Stage the host's agent configuration into ./docker/config for the container.
#
# The container never mounts the host's home directory: it reads copies from
# ./docker/config (mounted read-only at /config). This script produces those copies,
# with the one invariant the container relies on -- everything in ./config
# except engine.env is world-readable, so the image's own user can read it
# without any UID/GID matching.
#
# ./docker/config ships as templates you edit in place. This script replaces
# them with the operator's own files when they exist; the values it writes are
# real credentials, and only the templates are committed (see .gitignore).
#
# Usage:
#   docker/config-sync.sh              copy config and report what changed
#   docker/config-sync.sh --check      report drift only, exit 1 if different
#   docker/config-sync.sh --no-auth    do not copy pi/auth.json or opencode/auth.json
#   docker/config-sync.sh --no-skills  do not copy pi/skills
#
# Re-running is safe and is the supported way to apply changes: config is
# authoritative at container start, so `docker compose restart` picks it up.
set -eu

REPO_ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
CONFIG_DIR="${CONFIG_DIR:-${REPO_ROOT}/docker/config}"
PI_HOME="${HOME}/.pi/agent"
OPENCODE_HOME="${XDG_CONFIG_HOME:-${HOME}/.config}/opencode"
CHECK=0
COPY_AUTH=1
COPY_SKILLS=1

for arg in "$@"; do
  case "$arg" in
    --check) CHECK=1 ;;
    --no-auth) COPY_AUTH=0 ;;
    --no-skills) COPY_SKILLS=0 ;;
    -h|--help) sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown option: $arg" >&2; exit 2 ;;
  esac
done

say() { printf '%s\n' "$*"; }
changed=0
missing=0

# True when the destination tree already matches a dereferenced copy of the
# source. Skills are often symlinks into a shared directory, so the source is
# dereferenced before comparing (a dangling symlink inside the container would
# be useless anyway).
tree_same() {
  src=$1
  dst=$2
  [ -d "$dst" ] || return 1
  tmp=$(mktemp -d) || return 1
  if cp -aL "${src}/." "${tmp}/" 2>/dev/null && diff -r "$tmp" "$dst" >/dev/null 2>&1; then
    rm -rf "$tmp"
    return 0
  fi
  rm -rf "$tmp"
  return 1
}

# Copy one file and report whether the destination changed. Never writes in
# --check mode.
copy_file() {
  src=$1
  dst=$2
  if [ ! -f "$src" ]; then
    say "missing:   $src (skipped)"
    missing=1
    return 0
  fi
  if [ -f "$dst" ] && cmp -s "$src" "$dst"; then
    [ "$CHECK" -eq 0 ] && chmod 0644 "$dst"
    say "unchanged: ${dst#"$REPO_ROOT"/}"
    return 0
  fi
  changed=1
  if [ "$CHECK" -eq 1 ]; then
    say "outdated:  ${dst#"$REPO_ROOT"/}"
    return 0
  fi
  mkdir -p "$(dirname "$dst")"
  cp "$src" "$dst"
  chmod 0644 "$dst"
  say "updated:   ${dst#"$REPO_ROOT"/}"
}

# Copy a directory tree, replacing the destination. Never writes in --check
# mode.
copy_tree() {
  src=$1
  dst=$2
  if [ ! -d "$src" ]; then
    say "missing:   $src (skipped)"
    missing=1
    return 0
  fi
  if tree_same "$src" "$dst"; then
    [ "$CHECK" -eq 0 ] && chmod -R a+rX "$dst"
    say "unchanged: ${dst#"$REPO_ROOT"/}"
    return 0
  fi
  changed=1
  if [ "$CHECK" -eq 1 ]; then
    say "outdated:  ${dst#"$REPO_ROOT"/}"
    return 0
  fi
  rm -rf "$dst"
  mkdir -p "$dst"
  cp -aL "${src}/." "$dst/"
  chmod -R a+rX "$dst"
  say "updated:   ${dst#"$REPO_ROOT"/}"
}

if [ "$CHECK" -eq 0 ]; then
  # Directory skeleton only: the operator fills these in.
  mkdir -p "${CONFIG_DIR}/pi/skills" "${CONFIG_DIR}/opencode"
  say "skeleton:  ${CONFIG_DIR#"$REPO_ROOT"/}/{pi/skills,opencode}/"
fi

say ""
say "== operator files =="
if [ "$CHECK" -eq 0 ]; then
  if [ -f "${CONFIG_DIR}/workflow.yaml" ]; then
    say "present:   config/workflow.yaml"
  else
    cp "${REPO_ROOT}/workflow.example.yaml" "${CONFIG_DIR}/workflow.yaml"
    chmod 0644 "${CONFIG_DIR}/workflow.yaml"
    say "created:   config/workflow.yaml (edit it)"
    changed=1
  fi
  if [ -f "${CONFIG_DIR}/engine.env" ]; then
    say "present:   config/engine.env"
  else
    # engine.env.example is already the container variant: the engine paths come
    # from the image, so only the URL, the tokens and AGENT need filling in.
    cp "${REPO_ROOT}/engine.env.example" "${CONFIG_DIR}/engine.env"
    chmod 0600 "${CONFIG_DIR}/engine.env"
    say "created:   config/engine.env (edit it: OP_BASE_URL, tokens, AGENT)"
    changed=1
  fi
else
  for f in workflow.yaml engine.env; do
    [ -f "${CONFIG_DIR}/${f}" ] || { say "missing:   config/${f}"; missing=1; }
  done
fi

say ""
say "== agent configuration from your home directory =="
copy_file "${PI_HOME}/settings.json" "${CONFIG_DIR}/pi/settings.json"
if [ ! -f "${HOME}/.gitconfig" ]; then
  say "warning:   no ~/.gitconfig -- agents cannot commit; set user.name/user.email"
fi
copy_file "${HOME}/.gitconfig" "${CONFIG_DIR}/gitconfig"
if [ "$COPY_AUTH" -eq 1 ]; then
  copy_file "${PI_HOME}/auth.json" "${CONFIG_DIR}/pi/auth.json"
else
  say "skipped:   pi/auth.json (--no-auth: supply provider keys via engine.env)"
fi
if [ "$COPY_SKILLS" -eq 1 ]; then
  copy_tree "${PI_HOME}/skills" "${CONFIG_DIR}/pi/skills"
else
  say "skipped:   pi/skills (--no-skills)"
fi

# opencode is optional (INSTALL_OPENCODE=true rebuilds the image with it), and
# the host may not have it installed at all: missing files are only reported.
# Absent files are not drift: these are optional, and opencode is often not
# installed on the host at all.
if [ -f "${OPENCODE_HOME}/opencode.json" ]; then
  copy_file "${OPENCODE_HOME}/opencode.json" "${CONFIG_DIR}/opencode/opencode.json"
else
  say "skipped:   opencode/opencode.json (no ${OPENCODE_HOME}/opencode.json)"
fi
if [ "$COPY_AUTH" -ne 1 ]; then
  say "skipped:   opencode/auth.json (--no-auth)"
elif [ -f "${OPENCODE_HOME}/auth.json" ]; then
  copy_file "${OPENCODE_HOME}/auth.json" "${CONFIG_DIR}/opencode/auth.json"
fi

if [ "$CHECK" -eq 1 ]; then
  if [ "$changed" -eq 1 ] || [ "$missing" -eq 1 ]; then
    say ""
    say "docker/config/ is out of date; run docker/config-sync.sh and restart"
    exit 1
  fi
  say ""
  say "docker/config/ matches the host"
  exit 0
fi

say ""
say "docker/config is not committed: it holds your own credentials, settings,"
say "skills and git identity. Nothing else is needed from the repository."
say ""
say "next: docker compose pull && docker compose up -d"
say "      (after editing docker/config/engine.env: docker compose up -d)"
