#!/usr/bin/env bash
# claude-resave-state.sh — Re-save state files for all live Claude panes
# at their CURRENT positions. Run on layout-change tmux hooks so state files
# track windows that move/renumber/swap.
#
# Reads each pane's @claude_session_id option (set by save_session_state on
# SessionStart) and re-emits a state file at the pane's current key. The
# Fix 3 dedupe in save_session_state will collapse old keys.
#
# Tmux hooks that should call this script (wired in claude-tmux-setup.sh):
#   after-kill-window, after-renumber-window, after-rename-window,
#   after-swap-window, after-move-window, after-link-window,
#   after-unlink-window, after-split-window, pane-exited
#
# Liveness guard: a pane may carry a stale @claude_session_id from a Claude
# process that already exited (the option lives as long as the pane). We
# only re-save if the session's JSONL file still exists in PROJECTS_DIR
# AND the pane's running command is still `claude`. Otherwise the option
# is cleared so future re-saves don't keep resurrecting dead session ids.

set -u

PLUGIN_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck disable=SC1091
source "${PLUGIN_DIR}/lib/common.sh"

# Sessions live under a per-cwd directory, so search the whole projects root
# (same approach as lib/jsonl.sh; a hardcoded per-user dir false-negatives
# every session started outside $HOME and clears its state).
PROJECTS_ROOT="${PROJECTS_ROOT:-$HOME/.claude/projects}"

# Walk every pane in every session. For each pane that has @claude_session_id,
# verify liveness, then either re-save or clear the stale option.
while IFS=$'\t' read -r pane_id session_id pane_cmd; do
    [ -z "$pane_id" ] && continue
    [ -z "$session_id" ] && continue

    # Liveness check 1: JSONL must exist (in any per-cwd project dir)
    if [ -z "$(find "$PROJECTS_ROOT" -maxdepth 2 -name "${session_id}.jsonl" -type f 2>/dev/null | head -1)" ]; then
        tmux set -pu -t "$pane_id" @claude_session_id 2>/dev/null
        continue
    fi

    # Liveness check 2: pane's running command must contain "claude"
    # (covers `claude`, `claude --resume ...`, `claude-restore.sh` etc.)
    case "$pane_cmd" in
        *claude*) ;;
        *)
            tmux set -pu -t "$pane_id" @claude_session_id 2>/dev/null
            continue
            ;;
    esac

    # save_session_state computes current geometry itself — that's the point.
    # It also handles the dedupe + atomic write.
    save_session_state "$pane_id" "$session_id"
done < <(
    tmux list-panes -a \
        -F '#{pane_id}'$'\t''#{@claude_session_id}'$'\t''#{pane_current_command}' \
        2>/dev/null
)
