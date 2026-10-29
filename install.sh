#!/usr/bin/env bash
#
# Interactive installer for OpenProject Agent Engine.
#
# Before running this script, copy the two examples into this directory and
# edit them:
#
#   cp workflow.example.yaml ./workflow.yaml
#   cp engine.env.example ./engine.env
#   chmod 600 ./engine.env
#
# Then run ./install.sh. The script installs the package with uv and hands
# over to `op-agent-engine setup`, which provisions the OpenProject users,
# prompts for one API token per account, verifies them, installs the systemd
# service and runs a dry poll.

set -euo pipefail

ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
WORKFLOW_FILE="$ROOT_DIR/workflow.yaml"
ENV_FILE="$ROOT_DIR/engine.env"

if [[ -t 1 ]]; then
    BOLD=$'\033[1m'
    DIM=$'\033[2m'
    CYAN=$'\033[36m'
    RED=$'\033[31m'
    RESET=$'\033[0m'
else
    BOLD=''
    DIM=''
    CYAN=''
    RED=''
    RESET=''
fi

cleanup() {
    if [[ -t 1 ]]; then
        printf '\033[?25h'
    fi
}
trap cleanup EXIT

die() {
    printf '%s\n' "${RED}error:${RESET} $*" >&2
    exit 1
}

robot_frame() {
    local hand="$1"
    cat <<EOF
            o
            |
    .---------------.
    |  .-.     .-.  |
    |  |o|     |o|  |
    |  '-'     '-'  |
    |    [=====]    |
    '---------------'
          |   |
 ${hand} .----+---+----. ${hand}
  Y  |    AGENT    |  Y
  |__|   o  o  o   |__|
     '-------------'
EOF
}

animate_robot() {
    # Two pinch cycles: open -> closing -> pinched -> closing -> open.
    local frames=('\|/' '\| ' '>|<' '\| ' '\|/')
    if [[ ! -t 1 ]]; then
        robot_frame '\|/'
        return
    fi

    printf '\033[?25l'
    local cycle hand
    for cycle in 1 2; do
        for hand in "${frames[@]}"; do
            printf '\033[2J\033[H'
            robot_frame "$hand"
            sleep 0.12
        done
    done
    printf '\033[2J\033[H'
    robot_frame '\|/'
    printf '\033[?25h'
}

if [[ ! -f "$WORKFLOW_FILE" || ! -f "$ENV_FILE" ]]; then
    cat >&2 <<EOF
${BOLD}Missing configuration files.${RESET}

Copy the two examples into this directory and edit them first:

  cp workflow.example.yaml ./workflow.yaml
  cp engine.env.example ./engine.env
  chmod 600 ./engine.env

Then edit ./workflow.yaml (project, repository, statuses) and ./engine.env
(OP_BASE_URL and OP_PROVISION_TOKEN). The installer prompts for the API
tokens after provisioning the users.
EOF
    exit 1
fi

chmod 600 "$ENV_FILE" 2>/dev/null || true

UV_BIN="$(command -v uv || true)"
if [[ -z "$UV_BIN" && -x "$HOME/.local/bin/uv" ]]; then
    UV_BIN="$HOME/.local/bin/uv"
fi
if [[ -z "$UV_BIN" ]]; then
    die "uv is required. Install it from https://docs.astral.sh/uv/getting-started/installation/"
fi

animate_robot

printf '\n%s\n' "${BOLD}${CYAN}OpenProject Agent Engine${RESET}"
printf '%s\n' "${DIM}------------------------${RESET}"
printf '\nInstalling the op-agent-engine command with uv...\n\n'

"$UV_BIN" tool install --force "$ROOT_DIR"

# Prefer the uv tool shim over any older copy that may be earlier on PATH.
ENGINE_BIN=""
UV_TOOL_BIN="$("$UV_BIN" tool dir --bin 2>/dev/null || true)"
if [[ -n "$UV_TOOL_BIN" && -x "$UV_TOOL_BIN/op-agent-engine" ]]; then
    ENGINE_BIN="$UV_TOOL_BIN/op-agent-engine"
elif [[ -x "$HOME/.local/bin/op-agent-engine" ]]; then
    ENGINE_BIN="$HOME/.local/bin/op-agent-engine"
else
    ENGINE_BIN="$(command -v op-agent-engine || true)"
fi
if [[ -z "$ENGINE_BIN" ]]; then
    die "installed, but 'op-agent-engine' is not on PATH. Add \$HOME/.local/bin to PATH and re-run."
fi
if ! "$ENGINE_BIN" setup --help >/dev/null 2>&1; then
    die "the selected op-agent-engine ($ENGINE_BIN) is older than this checkout; remove it or fix PATH"
fi

# Make the command visible to the setup subprocesses (notably install-service).
export PATH="$(dirname -- "$ENGINE_BIN"):$PATH"

printf '\n'
exec "$ENGINE_BIN" setup --source-dir "$ROOT_DIR" "$@"
