#!/bin/sh
# Set up a deployment: the OpenProject accounts, the agent configuration and
# the engine's .env file.
#
# Setting up is three steps:
#   1. ./agent-init.sh        (this script, fully interactive)
#   2. add the GitHub token(s) to .env
#   3. docker compose up -d
#
# It asks for the deployment values, the model (optionally one per agent) and
# its provider key, and an OpenProject administrator token. Then it creates or
# reconciles the engine and agent users, shows each generated password once,
# interviews you for one API token per account (sign in at your OpenProject
# URL -> My account -> Access tokens), writes .env and validates the result.
#
# It does not ask for GitHub credentials. Add GH_TOKEN (and the optional
# GH_TOKEN_REVIEWER) to .env before starting the stack; the final message
# reminds you.
#
# Before running it, set up two fields in OpenProject (the API cannot create
# them):
#   * the AGENT work-package list custom field (or OP_AGENT_FIELD)
#   * the GIT_REPO project attribute (text)
# The engine reads each project's GitHub repository from the GIT_REPO project
# attribute, so any project with that value set (and the AGENT field enabled)
# can be monitored simply by setting OP_PROJECT=*.
#
# OpenProject tokens and the provider key are written to .env next to
# docker-compose.yml (mode 0600), so compose picks them up on the next start.
#
#   ./agent-init.sh                      guided run
#   ./agent-init.sh --reprompt-tokens    enter the OpenProject tokens again
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
    -h|--help) awk 'NR > 1 && /^#/ { sub(/^# ?/, ""); print; next } NR > 1 { exit }' "$0"; exit 0 ;;
    *) echo "agent-init: unknown option: $arg" >&2; exit 2 ;;
  esac
done

say() { printf '%s\n' "$*"; }
die() { printf 'agent-init: ERROR: %s\n' "$*" >&2; exit 2; }
title() { printf '\n%s\n%s\n' "$1" "$(printf '%*s' "${#1}" '' | tr ' ' '-')"; }
info() { printf '    %s\n' "$*"; }
ok() { printf '    ok  %s\n' "$*"; }
warn() { printf '    !!  %s\n' "$*"; }
step() { printf '\n[%s] %s\n\n' "$1" "$2"; }

# A credential block on its own lines, so the generated password can be
# copied without hunting through a wall of prompt text.
account_block() {
  account_label=$1
  account_login=$2
  account_password=$3
  account_token=$4
  say ""
  say "    ------------------------------------------------------------"
  printf '    %s\n' "$account_label"
  printf '      login:    %s\n' "$account_login"
  if [ -n "$account_password" ]; then
    printf '      password: %s\n' "$account_password"
    say "      ^ copy this password now; it is shown only once"
  else
    say "      password: (not shown; the account already existed)"
  fi
  printf '      token:    %s\n' "$account_token"
  say "    ------------------------------------------------------------"
  say ""
}

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

# --- model providers --------------------------------------------------------
# AGENT_MODEL is <provider>/<model>, and pi authenticates with the provider's
# own environment variables. This table mirrors PROVIDER_ENV_VARS in
# src/op_agent_engine/providers.py (tests/test_providers.py keeps them in step);
# it lives here as well so the prompts work before the engine runs. The first
# variable of a provider is normally its API key, the rest are endpoints or
# alternative credentials. A local provider can put its endpoint first instead
# (llama.cpp: LLAMA_BASE_URL, with an optional LLAMA_API_KEY). `-` means the
# provider has no environment variable (interactive login only), which an
# unattended run cannot use.
provider_table=$(cat <<'EOF'
amazon-bedrock AWS_BEARER_TOKEN_BEDROCK AWS_PROFILE AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_SESSION_TOKEN AWS_REGION
ant-ling ANT_LING_API_KEY
anthropic ANTHROPIC_API_KEY ANTHROPIC_AUTH_TOKEN ANTHROPIC_OAUTH_TOKEN ANTHROPIC_BASE_URL
azure AZURE_OPENAI_API_KEY AZURE_OPENAI_ENDPOINT AZURE_OPENAI_BASE_URL AZURE_OPENAI_RESOURCE_NAME AZURE_OPENAI_API_VERSION AZURE_OPENAI_DEPLOYMENT_NAME_MAP
baseten BASETEN_API_KEY
cerebras CEREBRAS_API_KEY
cloudflare-ai-gateway CLOUDFLARE_API_KEY CLOUDFLARE_ACCOUNT_ID CLOUDFLARE_GATEWAY_ID
cloudflare-workers-ai CLOUDFLARE_API_KEY CLOUDFLARE_ACCOUNT_ID
deepseek DEEPSEEK_API_KEY
fireworks FIREWORKS_API_KEY
github-copilot COPILOT_GITHUB_TOKEN
google GEMINI_API_KEY GOOGLE_API_KEY
google-vertex GOOGLE_CLOUD_API_KEY GOOGLE_CLOUD_PROJECT GOOGLE_CLOUD_LOCATION GOOGLE_APPLICATION_CREDENTIALS
groq GROQ_API_KEY
huggingface HF_TOKEN
kimi-coding KIMI_API_KEY
llama.cpp LLAMA_BASE_URL LLAMA_API_KEY
meta META_API_KEY
minimax MINIMAX_API_KEY
minimax-cn MINIMAX_CN_API_KEY
mistral MISTRAL_API_KEY
moonshotai MOONSHOT_API_KEY
moonshotai-cn MOONSHOT_API_KEY
nvidia NVIDIA_API_KEY
openai OPENAI_API_KEY OPENAI_BASE_URL
openai-codex -
opencode OPENCODE_API_KEY
opencode-go OPENCODE_API_KEY
openrouter OPENROUTER_API_KEY
qwen-token-plan QWEN_TOKEN_PLAN_API_KEY
qwen-token-plan-cn QWEN_TOKEN_PLAN_CN_API_KEY
qwen-token-plan-individual QWEN_TOKEN_PLAN_API_KEY
radius RADIUS_API_KEY
together TOGETHER_API_KEY
typesafe TYPESAFE_API_KEY
vercel-ai-gateway AI_GATEWAY_API_KEY
xai XAI_API_KEY
xiaomi XIAOMI_API_KEY
xiaomi-token-plan-ams XIAOMI_TOKEN_PLAN_AMS_API_KEY
xiaomi-token-plan-cn XIAOMI_TOKEN_PLAN_CN_API_KEY
xiaomi-token-plan-sgp XIAOMI_TOKEN_PLAN_SGP_API_KEY
zai ZAI_API_KEY
zai-coding-cn ZAI_CODING_CN_API_KEY
EOF
)

provider_vars() {
  printf '%s\n' "$provider_table" |
    awk -v wanted="$1" '$1 == wanted { sub(/^[^ \t]+[ \t]+/, ""); print; exit }'
}

provider_known() {
  printf '%s\n' "$provider_table" |
    awk -v wanted="$1" '$1 == wanted { found = 1 } END { exit(found ? 0 : 1) }'
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
  hint=${2:-}
  if [ -n "$hint" ]; then
    printf '    %s [%s]: ' "$label" "$hint" >&2
  else
    printf '    %s: ' "$label" >&2
  fi
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
  # OP_PROJECT is not in .env yet on a fresh deployment, so it is passed
  # through explicitly too.
  if OP_VERIFY_TOKEN="$token" compose_run \
      -e OP_BASE_URL -e OP_PROJECT -e OP_VERIFY_TOKEN \
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
  account_block "$label" "$login" "$password" "$env_var"
  if [ -z "$password" ]; then
    warn "$login already exists, so its password cannot be shown. Reset it in"
    warn "OpenProject (Administration -> Users -> $login) if you need to sign in."
  fi
  if [ "$admin" = "1" ]; then
    warn "make $login an administrator first: Administration -> Users -> $login"
    warn "  -> Administrator (needed to list users and edit work packages)."
  fi

  attempt=1
  while [ "$attempt" -le 3 ]; do
    TOKEN_VALUE=$(ask_secret "API token for $login ($env_var)")
    TOKEN_VALUE=$(printf '%s' "$TOKEN_VALUE" | tr -d '\r')
    if [ -z "$TOKEN_VALUE" ]; then
      warn "a token is required"
      attempt=$((attempt + 1))
      continue
    fi
    if verify_token "$login" "$TOKEN_VALUE" "$admin"; then
      ok "$env_var: token verified for $login"
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

project=$(ask_text "Project identifier(s), comma-separated or * for all projects with GIT_REPO" "$(get_env OP_PROJECT)")
[ -n "$project" ] || die "OP_PROJECT is required"
project=$(printf '%s' "$project" | tr -d ' ')
case "$project" in
  "*") ;;
  *"*"*) die "'*' must be used alone, not in a project list" ;;
esac
case ",$project," in
  *,,*) die "OP_PROJECT contains an empty project identifier" ;;
esac

# GITHUB_REPO_DIR is a single-project override and must not leak into a
# multi-project run.
GITHUB_REPO_DIR=$(get_env GITHUB_REPO_DIR)
case "$project" in
  *,*|"*")
    GITHUB_REPO_DIR=
    ;;
esac
export GITHUB_REPO_DIR

say ""
info "Repositories are read from the GIT_REPO project attribute."
info "Create it once under Administration -> Projects -> Project attributes"
info "(text format) and make it available for all projects."
info "The AGENT work-package custom field is set up separately."

step 2 "Model and provider key"
# The model is one setting for the engine (AGENT_MODEL=<provider>/<model>), but
# two questions here: the provider is also what selects the credential variable.
existing_model=$(get_env AGENT_MODEL)
if [ -z "$existing_model" ]; then
  # A deployment that names a model per agent has no AGENT_MODEL; offer one of
  # those as the default so switching back to a shared model is one Enter.
  for name in \
    AGENT_MODEL_FRONTEND \
    AGENT_MODEL_BACKEND \
    AGENT_MODEL_DESIGNER \
    AGENT_MODEL_REVIEWER
  do
    existing_model=$(get_env "$name")
    if [ -n "$existing_model" ]; then
      break
    fi
  done
fi
provider_default=
model_default=$existing_model
case "$existing_model" in
  */*)
    provider_default=${existing_model%%/*}
    model_default=${existing_model#*/}
    ;;
esac

say ""
info "The model is passed to the CLI as <provider>/<model>."
info "Known providers:"
printf '%s\n' "$provider_table" |
  awk 'NF { printf "%s%s", sep, $1; sep = ", " } END { print "" }' |
  fold -s -w 72 |
  sed 's/^/    /'
provider=$(ask_text "Model provider" "$provider_default")
provider=$(printf '%s' "$provider" | tr '[:upper:]' '[:lower:]')
[ -n "$provider" ] || die "a model provider is required"

# One model for every agent, or one model per agent. The provider is asked for
# once either way, so every model keeps it and the deployment stays on one
# provider key.
say ""
info "All agents can share one model, or each can name its own."
info "Either way they stay on the provider $provider."
separate=$(ask_text "A separate model for each agent? (y/N)" "n")
case "$separate" in
  y|Y|yes|YES)
    say ""
    info "The model id only - the provider prefix is added for you."
    info "Press Enter to leave an agent on pi's own default model."
    agent_model=
    configured=0
    for entry in \
      FRONTEND:AGENT_MODEL_FRONTEND \
      BACKEND:AGENT_MODEL_BACKEND \
      LEAD_DESIGNER:AGENT_MODEL_DESIGNER \
      REVIEWER:AGENT_MODEL_REVIEWER
    do
      agent_name=${entry%%:*}
      name=${entry#*:}
      existing=$(get_env "$name")
      case "$existing" in
        "$provider"/*) default=${existing#"$provider"/} ;;
        "") default=$model_default ;;
        *) default=$existing ;;
      esac
      override=$(ask_text "$agent_name model" "$default")
      case "$override" in
        "$provider"/*) override=${override#"$provider"/} ;;
      esac
      case "$override" in
        *" "*|*"	"*) die "the model id for $agent_name must be one token (no spaces)" ;;
      esac
      if [ -n "$override" ]; then
        set_env "$name" "${provider}/${override}"
        ok "$name: ${provider}/${override}"
        configured=1
      else
        unset_env "$name"
        warn "$agent_name: no model; pi's own default is used"
      fi
    done
    [ "$configured" -eq 1 ] ||
      die "no agent model was entered; re-run and answer n for one shared model"
    if [ -n "$(get_env AGENT_MODEL)" ]; then
      info "removing AGENT_MODEL; each agent names its own model"
      unset_env AGENT_MODEL
    fi
    ;;
  *)
    say ""
    info "The model id only - the provider prefix is added for you."
    model=$(ask_text "Model (e.g. deepseek/deepseek-v4.1-flash-1017)" "$model_default")
    case "$model" in
      "$provider"/*) model=${model#"$provider"/} ;;
    esac
    case "$model" in
      "" ) die "a model id is required" ;;
      *" "*|*"	"*) die "the model id must be one token (no spaces)" ;;
    esac
    agent_model="${provider}/${model}"
    for name in \
      AGENT_MODEL_FRONTEND \
      AGENT_MODEL_BACKEND \
      AGENT_MODEL_DESIGNER \
      AGENT_MODEL_REVIEWER
    do
      if [ -n "$(get_env "$name")" ]; then
        warn "removing $name; every agent uses $agent_model"
        unset_env "$name"
      fi
    done
    ;;
esac

# The provider's own credential variables; the CLI reads them from the
# environment, and the engine forwards them to the agent process.
say ""
if [ "$provider" = "llama.cpp" ]; then
  info "llama.cpp is a local server: LLAMA_BASE_URL is the router URL"
  info "(for example http://192.168.1.10:8080) and LLAMA_API_KEY is optional."
fi
provider_keys=$(provider_vars "$provider")
if provider_known "$provider"; then
  if [ -z "$provider_keys" ] || [ "$provider_keys" = "-" ]; then
    warn "$provider authenticates with an interactive login, which an"
    warn "unattended run cannot use; there is no key to store."
    provider_keys=
  fi
else
  warn "$provider is not in the built-in provider list, so its credential"
  warn "variable is asked for below (see docker/.env.example for the usual ones)."
  provider_keys=$(ask_text "Credential variable (e.g. OPENROUTER_API_KEY)" \
    "$(printf '%s' "$provider" | tr '[:lower:]-' '[:upper:]_')_API_KEY")
fi

if [ -n "$provider_keys" ]; then
  # The first variable is normally the provider's API key; a local provider's
  # endpoint (llama.cpp) is asked for as plain text instead. The rest are
  # optional, and LLAMA_API_KEY stays optional for an unauthenticated server.
  set -- $provider_keys
  primary=$1
  shift
  existing=$(get_env "$primary")
  if [ -n "$existing" ]; then
    hint="stored - blank keeps it"
  else
    hint=
  fi
  case "$primary" in
    *_URL|*_ENDPOINT)
      key=$(ask_text "$primary" "$existing")
      ;;
    *)
      key=$(ask_secret "$primary" "$hint")
      ;;
  esac
  if [ -n "$key" ]; then
    set_env "$primary" "$key"
  elif [ -n "$existing" ]; then
    ok "$primary: keeping the stored value"
  else
    warn "$primary: no value set; check-config will reject the configuration"
  fi

  if [ "$#" -gt 0 ]; then
    say ""
    info "Optional settings for $provider: $*"
    configure=$(ask_text "Set them now? (y/N)" "n")
    case "$configure" in
      y|Y|yes|YES)
        for name in "$@"; do
          existing=$(get_env "$name")
          case "$name" in
            *KEY*|*TOKEN*|*SECRET*|*PASSWORD*)
              value=$(ask_secret "$name" "$existing")
              ;;
            *)
              value=$(ask_text "$name" "$existing")
              ;;
          esac
          if [ -n "$value" ]; then
            set_env "$name" "$value"
          fi
        done
        ;;
    esac
  fi
fi

step 3 "OpenProject administrator token"
info "It is used once to create the users and is not stored."
admin_token=$(ask_secret "Administrator API token")
[ -n "$admin_token" ] || die "an administrator token is required"

export OP_BASE_URL="$op_base_url" \
       OP_PROJECT="$project" \
       OP_PROVISION_TOKEN="$admin_token"
AGENT_MODEL="$agent_model"
export AGENT_MODEL LOG_LEVEL=CRITICAL

passwords_file=$(mktemp "${TMPDIR:-/tmp}/op-agent-engine-passwords.XXXXXX")
provision_err=$(mktemp "${TMPDIR:-/tmp}/op-agent-engine-provision.XXXXXX")
cleanup() { rm -f "$passwords_file" "$provision_err"; }
trap cleanup EXIT INT TERM

step 4 "Creating OpenProject users and memberships"
if ! compose_run \
    -e OP_BASE_URL -e OP_PROJECT \
    -e GITHUB_REPO_DIR \
    -e AGENT_MODEL -e OP_PROVISION_TOKEN -e LOG_LEVEL \
    engine provision --apply --json --passwords >"$passwords_file" 2>"$provision_err"; then
  sed 's/^/    /' "$provision_err" >&2
  die "provisioning failed (see the message above)"
fi
if [ -s "$passwords_file" ]; then
  ok "created $(wc -l < "$passwords_file" | tr -d ' ') account(s)"
else
  ok "users already existed; no new passwords"
fi

step 5 "Create one API token per account"
info "OpenProject administrators cannot mint API tokens for other users."
info "Sign in as each account and create a token under:"
info "  $op_base_url/my/access_tokens"
info "Copy each password when it appears; it is shown only once."

engine_user=$(get_env OP_ENGINE_USER)
[ -n "$engine_user" ] || engine_user=agent-engine

prompt_token OP_ENGINE_TOKEN "$engine_user" "ENGINE (runtime user)" 1
engine_token=$TOKEN_VALUE

prompt_token OP_TOKEN_FRONTEND frontendEngineer "FRONTEND" 0
frontend_token=$TOKEN_VALUE

prompt_token OP_TOKEN_BACKEND backendEngineer "BACKEND" 0
backend_token=$TOKEN_VALUE

prompt_token OP_TOKEN_DESIGNER leadDesigner "LEAD_DESIGNER" 0
designer_token=$TOKEN_VALUE

prompt_token OP_TOKEN_REVIEWER codeReviewer "REVIEWER" 0
reviewer_token=$TOKEN_VALUE

step 6 "Saving .env"
set_env OP_BASE_URL "$op_base_url"
set_env OP_PROJECT "$project"
if [ -n "$(get_env OP_GIT_REPO_FIELD)" ]; then
  warn "removing the obsolete OP_GIT_REPO_FIELD value; repositories come from GIT_REPO"
  unset_env OP_GIT_REPO_FIELD
fi
if [ -n "$(get_env GITHUB_REPO)" ]; then
  warn "removing the obsolete GITHUB_REPO value; repositories come from GIT_REPO"
  unset_env GITHUB_REPO
fi
if [ -n "$(get_env AGENT)" ]; then
  warn "removing the obsolete AGENT value; pi is always used"
  unset_env AGENT
fi
case "$project" in
  *,*|"*")
    if [ -n "$(get_env GITHUB_REPO_DIR)" ]; then
      warn "removing the single-project GITHUB_REPO_DIR value"
    fi
    unset_env GITHUB_REPO_DIR
    ;;
esac
if [ -n "$AGENT_MODEL" ]; then
  set_env AGENT_MODEL "$AGENT_MODEL"
  ok "model: $AGENT_MODEL"
else
  unset_env AGENT_MODEL
  ok "model: one per agent, see AGENT_MODEL_*"
fi
set_env OP_ENGINE_TOKEN "$engine_token"
set_env OP_TOKEN_FRONTEND "$frontend_token"
set_env OP_TOKEN_BACKEND "$backend_token"
set_env OP_TOKEN_DESIGNER "$designer_token"
set_env OP_TOKEN_REVIEWER "$reviewer_token"
# GitHub credentials (GH_TOKEN and optional GH_TOKEN_REVIEWER) are left for
# the operator to fill in by hand; this wizard only sets up OpenProject.

# The administrator token was needed for this run only.
unset_env OP_PROVISION_TOKEN
ok "wrote $env_file (mode 0600)"

step 7 "Validating the configuration"
if ! compose_run engine check-config; then
  die "check-config failed; fix the message above and re-run this script"
fi

step 8 "Before you start the stack"
info "Finish updating $env_file:"
info "  - add GH_TOKEN (and the optional GH_TOKEN_REVIEWER)"
info "  - review any other settings this wizard does not manage"
say ""
info "Then start the engine:"
info "  docker compose up -d"
info "  docker compose logs -f engine"
say ""
info "Every answer from this wizard is a line in $env_file. After editing it,"
info "run docker compose up -d again so the container is recreated with the"
info "new values."
