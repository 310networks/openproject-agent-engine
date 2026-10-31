#!/bin/sh
# Create the OpenProject accounts and write the engine's .env file.
#
# Run this before `docker compose up -d`. It is fully interactive: it asks for
# the deployment values it needs, asks for an OpenProject administrator token,
# creates/reconciles the engine and agent users, shows each generated password
# once, then interviews you for one API token per account (sign in at your
# OpenProject URL -> My account -> Access tokens).
#
# Tokens are written to .env next to docker-compose.yml (mode 0600), so nothing
# has to be hand-edited and compose picks them up on the next start.
#
#   ./provision-users.sh                      guided run
#   ./provision-users.sh --reprompt-tokens    enter the tokens again
#
# Needs Docker Compose and the image (pulled if it is missing).
set -eu

deploy_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
cd "$deploy_dir"

env_file="${deploy_dir}/.env"
reprompt=0
for arg in "$@"; do
  case "$arg" in
    --reprompt-tokens) reprompt=1 ;;
    -h|--help) sed -n '2,16p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "provision-users: unknown option: $arg" >&2; exit 2 ;;
  esac
done

say() { printf '%s\n' "$*"; }
die() { printf 'provision-users: ERROR: %s\n' "$*" >&2; exit 2; }
title() { printf '\n%s\n%s\n' "$1" "$(printf '%*s' "${#1}" '' | tr ' ' '-')"; }
info() { printf '    %s\n' "$*"; }
ok() { printf '    ok  %s\n' "$*"; }
warn() { printf '    !!  %s\n' "$*"; }
step() { printf '\n[%s] %s\n' "$1" "$2"; }

# --- .env helpers -----------------------------------------------------------

get_env() {
  [ -f "$env_file" ] || return 0
  sed -n "s/^$1=//p" "$env_file" | tail -n 1
}

usable() {
  case "${1:-}" in
    ""|replace-with*|change-me*|changeme*|your-*|todo|TODO) return 1 ;;
    *) return 0 ;;
  esac
}

set_env() {
  name=$1
  value=$2
  [ -f "$env_file" ] || : > "$env_file"
  tmp="${env_file}.tmp.$$"
  awk -v name="$name" -v value="$value" '
    BEGIN { found = 0 }
    index($0, name "=") == 1 {
      if (!found) { print name "=" value; found = 1 }
      next
    }
    { print }
    END { if (!found) print name "=" value }
  ' "$env_file" > "$tmp"
  mv "$tmp" "$env_file"
  chmod 0600 "$env_file"
}

unset_env() {
  name=$1
  [ -f "$env_file" ] || return 0
  tmp="${env_file}.tmp.$$"
  awk -v name="$name" 'index($0, name "=") == 1 { next } { print }' \
    "$env_file" > "$tmp"
  mv "$tmp" "$env_file"
  chmod 0600 "$env_file"
}

# --- prompting --------------------------------------------------------------
# Prompts go to stderr, answers to stdout, so `value=$(ask_text ...)` captures
# only the answer.

ask_text() {
  label=$1
  default=$2
  if [ -n "$default" ]; then
    printf '    %s [%s]: ' "$label" "$default" >&2
  else
    printf '    %s: ' "$label" >&2
  fi
  IFS= read -r answer || answer=""
  [ -n "$answer" ] || answer=$default
  printf '%s\n' "$answer"
}

ask_secret() {
  label=$1
  printf '    %s: ' "$label" >&2
  stty -echo 2>/dev/null || true
  IFS= read -r answer || answer=""
  stty echo 2>/dev/null || true
  printf '\n' >&2
  printf '%s\n' "$answer"
}

# --- docker helpers ---------------------------------------------------------

compose_run() {
  docker compose run --rm --no-deps -T "$@"
}

verify_token() {
  login=$1
  token=$2
  admin=$3
  err=$(mktemp "${TMPDIR:-/tmp}/op-agent-engine-verify.XXXXXX")
  if [ "$admin" = "1" ]; then
    set -- --admin
  else
    set --
  fi
  if OP_VERIFY_TOKEN="$token" compose_run \
      -e OP_BASE_URL -e OP_VERIFY_TOKEN \
      engine verify-token --login "$login" "$@" >/dev/null 2>"$err"; then
    rm -f "$err"
    return 0
  fi
  sed 's/^/    /' "$err" >&2
  rm -f "$err"
  return 1
}

password_for() {
  [ -s "$passwords_file" ] || return 0
  awk -F '\t' -v login="$1" '$1 == login { print $2; exit }' "$passwords_file"
}

# Sets TOKEN_VALUE; all UI output stays on stdout.
prompt_token() {
  env_var=$1
  login=$2
  label=$3
  admin=$4
  TOKEN_VALUE=""

  existing=$(get_env "$env_var")
  if [ "$reprompt" -eq 0 ] && usable "$existing"; then
    if verify_token "$login" "$existing" "$admin"; then
      ok "$env_var: reusing the existing token"
      TOKEN_VALUE=$existing
      return 0
    fi
    warn "$env_var: the stored token no longer matches $login"
  fi

  password=$(password_for "$login")
  if [ -n "$password" ]; then
    printf '    %s: login %s / password %s\n' "$label" "$login" "$password"
  else
    warn "$login already exists, so its password cannot be shown. Reset it in"
    warn "OpenProject (Administration -> Users -> $login) if you need to sign in."
  fi
  if [ "$admin" = "1" ]; then
    warn "make $login an administrator first: Administration -> Users -> $login"
    warn "  -> Administrator (needed to list users and edit work packages)."
  fi

  attempt=1
  while [ "$attempt" -le 3 ]; do
    TOKEN_VALUE=$(ask_secret "    $label token ($env_var)")
    TOKEN_VALUE=$(printf '%s' "$TOKEN_VALUE" | tr -d '\r')
    if [ -z "$TOKEN_VALUE" ]; then
      warn "a token is required"
      attempt=$((attempt + 1))
      continue
    fi
    if verify_token "$login" "$TOKEN_VALUE" "$admin"; then
      return 0
    fi
    attempt=$((attempt + 1))
  done
  die "could not verify a token for $login after 3 attempts"
}

# --- main -------------------------------------------------------------------

image=$(docker compose config --images 2>/dev/null | head -n 1 || true)
if [ -n "${image:-}" ] && ! docker image inspect "$image" >/dev/null 2>&1; then
  say "pulling $image"
  docker compose pull --quiet
fi

title "OpenProject Agent Engine - users, tokens and .env"
info "deployment: $deploy_dir"

step 1 "Deployment values"
say "  Press Enter to keep a value shown in brackets."
op_base_url=$(ask_text "OpenProject URL" "$(get_env OP_BASE_URL)")
case "$op_base_url" in
  *example.com*) die "set OP_BASE_URL to your OpenProject URL (the example is not usable)" ;;
  http://*|https://*) ;;
  *) die "OP_BASE_URL must start with http:// or https://" ;;
esac
op_base_url=${op_base_url%/}

project=$(ask_text "OpenProject project identifier" "$(get_env OP_PROJECT)")
[ -n "$project" ] || die "OP_PROJECT is required"

repo=$(ask_text "GitHub repository (owner/name)" "$(get_env GITHUB_REPO)")
[ -n "$repo" ] || die "GITHUB_REPO is required"
case "$repo" in
  */*) ;;
  *) die "GITHUB_REPO must be in owner/name form" ;;
esac

branch=$(ask_text "Default branch" "$(get_env GITHUB_DEFAULT_BRANCH)")
[ -n "$branch" ] || branch=main

agent=$(ask_text "Agent CLI (pi or opencode)" "$(get_env AGENT)")
[ -n "$agent" ] || agent=pi
case "$agent" in
  pi|opencode) ;;
  *) die "AGENT must be pi or opencode" ;;
esac

model=$(ask_text "Model for every agent (blank = the CLI's default)" "$(get_env AGENT_MODEL)")

step 2 "OpenProject administrator token"
info "It is used once to create the users and is not stored."
admin_token=$(ask_secret "  Administrator API token")
[ -n "$admin_token" ] || die "an administrator token is required"

export OP_BASE_URL="$op_base_url" \
       OP_PROJECT="$project" \
       GITHUB_REPO="$repo" \
       GITHUB_DEFAULT_BRANCH="$branch" \
       AGENT="$agent" \
       OP_PROVISION_TOKEN="$admin_token"
GITHUB_REPO_DIR=$(get_env GITHUB_REPO_DIR)
AGENT_MODEL="$model"
export GITHUB_REPO_DIR AGENT_MODEL LOG_LEVEL=CRITICAL

passwords_file=$(mktemp "${TMPDIR:-/tmp}/op-agent-engine-passwords.XXXXXX")
provision_err=$(mktemp "${TMPDIR:-/tmp}/op-agent-engine-provision.XXXXXX")
cleanup() { rm -f "$passwords_file" "$provision_err"; }
trap cleanup EXIT INT TERM

step 3 "Creating OpenProject users and memberships"
if ! compose_run \
    -e OP_BASE_URL -e OP_PROJECT -e GITHUB_REPO -e GITHUB_REPO_DIR \
    -e GITHUB_DEFAULT_BRANCH -e AGENT -e AGENT_MODEL -e OP_PROVISION_TOKEN \
    -e LOG_LEVEL \
    engine provision --apply --json --passwords >"$passwords_file" 2>"$provision_err"; then
  sed 's/^/    /' "$provision_err" >&2
  die "provisioning failed (see the message above)"
fi
if [ -s "$passwords_file" ]; then
  ok "created $(wc -l < "$passwords_file" | tr -d ' ') account(s)"
else
  ok "users already existed; no new passwords"
fi

step 4 "Create one API token per account"
info "OpenProject administrators cannot mint API tokens for other users."
info "Sign in as each account and create a token under:"
info "  $op_base_url/my/access_tokens"
info "Passwords for accounts created just now are shown with their prompt."

engine_user=$(get_env OP_ENGINE_USER)
[ -n "$engine_user" ] || engine_user=agent-engine

prompt_token OP_ENGINE_TOKEN "$engine_user" "$engine_user (runtime)" 1
engine_token=$TOKEN_VALUE

prompt_token OP_TOKEN_FRONTEND frontendEngineer "FRONTEND (frontendEngineer)" 0
frontend_token=$TOKEN_VALUE

prompt_token OP_TOKEN_BACKEND backendEngineer "BACKEND (backendEngineer)" 0
backend_token=$TOKEN_VALUE

prompt_token OP_TOKEN_DESIGNER leadDesigner "LEAD_DESIGNER (leadDesigner)" 0
designer_token=$TOKEN_VALUE

prompt_token OP_TOKEN_REVIEWER codeReviewer "REVIEWER (codeReviewer)" 0
reviewer_token=$TOKEN_VALUE

step 5 "Saving .env"
set_env OP_BASE_URL "$op_base_url"
set_env OP_PROJECT "$project"
set_env GITHUB_REPO "$repo"
if [ -n "$GITHUB_REPO_DIR" ]; then
  set_env GITHUB_REPO_DIR "$GITHUB_REPO_DIR"
else
  unset_env GITHUB_REPO_DIR
fi
set_env GITHUB_DEFAULT_BRANCH "$branch"
set_env AGENT "$agent"
if [ -n "$AGENT_MODEL" ]; then
  set_env AGENT_MODEL "$AGENT_MODEL"
else
  unset_env AGENT_MODEL
fi
set_env OP_ENGINE_TOKEN "$engine_token"
set_env OP_TOKEN_FRONTEND "$frontend_token"
set_env OP_TOKEN_BACKEND "$backend_token"
set_env OP_TOKEN_DESIGNER "$designer_token"
set_env OP_TOKEN_REVIEWER "$reviewer_token"
# The administrator token was needed for this run only.
unset_env OP_PROVISION_TOKEN
ok "wrote $env_file (mode 0600)"

step 6 "Validating the configuration"
if ! compose_run engine check-config; then
  die "check-config failed; fix the message above and re-run this script"
fi

step 7 "Next"
info "set the model provider key in .env (e.g. ANTHROPIC_API_KEY=...)"
info "start the engine: docker compose up -d"
info "follow it with:    docker compose logs -f engine"
