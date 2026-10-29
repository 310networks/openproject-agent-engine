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
if [[ -z "$ENGINE_BIN" ]]; then
    die "op-agent-engine is not installed; nothing to uninstall"
fi
if ! "$ENGINE_BIN" uninstall --help >/dev/null 2>&1; then
    die "the selected op-agent-engine ($ENGINE_BIN) is too old to uninstall"
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

printf '\n%s\n' "${BOLD}${CYAN}OpenProject Agent Engine uninstall${RESET}"
printf '%s\n' "${BOLD}-----------------------------------${RESET}"

set +e
"$ENGINE_BIN" uninstall --source-dir "$ROOT_DIR" "${ARGS[@]}"
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
