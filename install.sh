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
#
# Re-running is safe and resumes: tokens already stored in engine.env are
# re-verified and reused instead of being prompted for again. Pass
# --reprompt-tokens to re-enter them anyway; extra flags are forwarded to
# `op-agent-engine setup` (e.g. --skip-service, --yes).

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
  Y  |  OP Agent   |  Y
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

# ``--reinstall-package`` is required. uv caches the wheel it builds from a
# local directory, keyed by the package version. This project's version does
# not change between checkouts, so a plain ``--force`` install happily reuses
# the previously built wheel and keeps running stale code (for example an old
# ``setup`` that never printed the generated passwords). Forcing a rebuild of
# just this package keeps the cached third-party dependencies.
"$UV_BIN" tool install --force --reinstall-package op-agent-engine "$ROOT_DIR"

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

# Verify the installed code really is this checkout's code. A cached wheel can
# otherwise make ``setup`` run an older version without any visible error.
RESOLVED_BIN="$(readlink -f -- "$ENGINE_BIN" 2>/dev/null || true)"
TOOL_PYTHON=""
if [[ -n "$RESOLVED_BIN" ]]; then
    TOOL_PYTHON="$(dirname -- "$RESOLVED_BIN")/python"
fi
if [[ -x "$TOOL_PYTHON" && -d "$ROOT_DIR/src/op_agent_engine" ]]; then
    SOURCE_MISMATCH="$("$TOOL_PYTHON" - "$ROOT_DIR/src/op_agent_engine" 2>/dev/null <<'PY'
import hashlib
import pathlib
import sys

import op_agent_engine

checkout = pathlib.Path(sys.argv[1])
installed = pathlib.Path(op_agent_engine.__file__).parent
for name in ("wizard.py", "provision.py"):
    want = hashlib.sha256((checkout / name).read_bytes()).hexdigest()
    have = hashlib.sha256((installed / name).read_bytes()).hexdigest()
    if want != have:
        print(name)
PY
)"
    if [[ -n "$SOURCE_MISMATCH" ]]; then
        die "the installed op-agent-engine ($ENGINE_BIN) still has stale code ($SOURCE_MISMATCH); reinstalling did not take effect"
    fi
fi

# Make the command visible to the setup subprocesses (notably install-service).
export PATH="$(dirname -- "$ENGINE_BIN"):$PATH"

printf '\n'
exec "$ENGINE_BIN" setup --source-dir "$ROOT_DIR" "$@"
