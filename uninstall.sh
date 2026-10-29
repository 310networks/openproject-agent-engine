#!/usr/bin/env bash
#
# Uninstall OpenProject Agent Engine.
#
# Removes the systemd units, local configuration/data and (when permitted) the
# OpenProject users created by provisioning. The uv tool install is removed
# last. Dry-run is the default in the Python command; this script performs the
# removal after its confirmation prompt.
#
# Useful flags are passed through:
#   --dry-run            show the plan and change nothing
#   --keep-config        keep ~/.config/op-agent-engine and checkout configs
#   --keep-data          keep runs, database and staged systemd units
#   --keep-users         keep OpenProject users
#   --purge-agent-repos  also delete WORKSPACE_ROOT/*/repositories
#   --yes                skip the confirmation prompt
#   --keep-tool          do not remove the uv tool install

set -euo pipefail

ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

if [[ -t 1 ]]; then
    BOLD=$'\033[1m'
    CYAN=$'\033[36m'
    RED=$'\033[31m'
    RESET=$'\033[0m'
else
    BOLD=''
    CYAN=''
    RED=''
    RESET=''
fi

die() {
    printf '%s\n' "${RED}error:${RESET} $*" >&2
    exit 1
}

UV_BIN="$(command -v uv || true)"
if [[ -z "$UV_BIN" && -x "$HOME/.local/bin/uv" ]]; then
    UV_BIN="$HOME/.local/bin/uv"
fi

ENGINE_BIN=""
if [[ -n "$UV_BIN" ]]; then
    UV_TOOL_BIN="$("$UV_BIN" tool dir --bin 2>/dev/null || true)"
    if [[ -n "$UV_TOOL_BIN" && -x "$UV_TOOL_BIN/op-agent-engine" ]]; then
        ENGINE_BIN="$UV_TOOL_BIN/op-agent-engine"
    fi
fi
if [[ -z "$ENGINE_BIN" && -x "$HOME/.local/bin/op-agent-engine" ]]; then
    ENGINE_BIN="$HOME/.local/bin/op-agent-engine"
fi
if [[ -z "$ENGINE_BIN" ]]; then
    ENGINE_BIN="$(command -v op-agent-engine || true)"
fi
# Use the installed command when it knows about 'uninstall'. Otherwise fall
# back to the checkout, so an older tool install can still be removed.
RUNNER=()
if [[ -n "$ENGINE_BIN" ]] && "$ENGINE_BIN" uninstall --help >/dev/null 2>&1; then
    RUNNER=("$ENGINE_BIN")
elif [[ -x "$ROOT_DIR/.venv/bin/python" ]] && \
     "$ROOT_DIR/.venv/bin/python" -m op_agent_engine uninstall --help >/dev/null 2>&1; then
    printf '%s\n' "note: installed op-agent-engine is stale; using the checkout" >&2
    RUNNER=("$ROOT_DIR/.venv/bin/python" -m op_agent_engine)
elif [[ -n "$UV_BIN" ]] && \
     "$UV_BIN" run --quiet --project "$ROOT_DIR" python -m op_agent_engine uninstall --help >/dev/null 2>&1; then
    printf '%s\n' "note: installed op-agent-engine is stale; using the checkout via uv" >&2
    RUNNER=("$UV_BIN" run --quiet --project "$ROOT_DIR" python -m op_agent_engine)
fi
if [[ ${#RUNNER[@]} -eq 0 ]]; then
    if [[ -z "$ENGINE_BIN" ]]; then
        die "op-agent-engine is not installed and the checkout cannot run it"
    fi
    die "op-agent-engine at $ENGINE_BIN is older than this checkout; run 'uv tool install --force .' and retry"
fi

APPLY_EXPLICIT=0
DRY_RUN=0
KEEP_TOOL=0
ARGS=()
for arg in "$@"; do
    case "$arg" in
        --apply)
            APPLY_EXPLICIT=1
            ARGS+=("$arg")
            ;;
        --dry-run)
            DRY_RUN=1
            ARGS+=("$arg")
            ;;
        --keep-tool)
            KEEP_TOOL=1
            ;;
        *)
            ARGS+=("$arg")
            ;;
    esac
done

if [[ $DRY_RUN -eq 1 && $APPLY_EXPLICIT -eq 1 ]]; then
    die "--dry-run and --apply are mutually exclusive"
fi
if [[ $DRY_RUN -eq 0 && $APPLY_EXPLICIT -eq 0 ]]; then
    ARGS+=("--apply")
fi

set +e
"${RUNNER[@]}" uninstall --source-dir "$ROOT_DIR" "${ARGS[@]}"
STATUS=$?
set -e

if [[ $STATUS -ne 0 ]]; then
    exit "$STATUS"
fi

if [[ $DRY_RUN -eq 1 || $KEEP_TOOL -eq 1 ]]; then
    exit 0
fi

if [[ -z "$UV_BIN" ]]; then
    printf '%s\n' "warning: uv not found; remove the op-agent-engine tool manually" >&2
    exit 0
fi

printf '\nRemoving the uv tool install...\n'
if ! "$UV_BIN" tool uninstall op-agent-engine; then
    die "local cleanup succeeded, but 'uv tool uninstall op-agent-engine' failed"
fi

printf '\n%s\n' "${BOLD}${CYAN}Uninstall complete${RESET}"
