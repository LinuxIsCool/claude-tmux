#!/usr/bin/env bash
# Inject claude-tmux indicator into tmux format strings
# Run once after tmux-power or theme plugin loads
#
# Architecture: hooks write to tmux pane options (@claude_state, @claude_color, @claude_persona).
# Format strings read them with #{@claude_state}, #[fg=#{@claude_color}], #{@claude_persona}.
# Conditional #{?...} hides the indicator when no state is set.

CURRENT_WSF=$(tmux show-option -gv window-status-format 2>/dev/null)

# --- Start tmux-arbiter if not running ---
# Must run unconditionally — even if format strings are already injected,
# the arbiter needs to be running for indicators to work.
if ! systemctl --user is-active --quiet claude-tmux-arbiter.service 2>/dev/null; then
    ARBITER="$(cd "$(dirname "$0")" && pwd)/tmux-arbiter.py"
    if [ -f "$ARBITER" ] && command -v uv >/dev/null 2>&1; then
        uv run "$ARBITER" 2>/dev/null &
    fi
fi

# --- Format string injection (idempotent: strips old, injects new) ---
THEME_FILE="$(cd "$(dirname "$0")/.." && pwd)/theme.json"

# Build segment fragments from theme.json segment_order array.
# Each segment maps to a tmux user option: @claude_{segment} / @claude_window_{segment}
# Default order if theme.json missing: persona voice hook
build_segments() {
    local prefix="$1"  # "" for pane-level, "window_" for window-level
    local fg_restore="$2"  # color to restore after state dot

    local segments=""
    if [ -f "$THEME_FILE" ] && command -v jq >/dev/null 2>&1; then
        segments=$(jq -r '.segment_order // ["persona","voice","hook"] | .[]' "$THEME_FILE" 2>/dev/null)
    fi
    [ -z "$segments" ] && segments="persona voice hook"

    local result=""
    for seg in $segments; do
        local var="@claude_${prefix}${seg}"
        if [ "$prefix" = "" ] && [ "$seg" = "voice" ]; then
            # window-status-format renders in the ACTIVE pane's context, so the
            # pane branch shows the active pane's icon for the whole tab. A
            # window running Claude in two panes would hide the speaking star
            # whenever the speaker was not the active pane — invisible by
            # construction, not a race (found 2026-08-05).
            #
            # Voice is a window-scoped question: "is anyone in this tab
            # talking?" voice_arbiter.py mirrors the highest-priority state of
            # a window's panes into @claude_window_voice, so prefer it and fall
            # back to the pane option when it is unset.
            result="${result}#{?#{@claude_window_voice},#{@claude_window_voice},#{?#{${var}},#{${var}},}}"
        else
            result="${result}#{?#{${var}},#{${var}},}"
        fi
    done
    echo "#[fg=#{@claude_${prefix}color}]#{@claude_${prefix}state}#[fg=${fg_restore}]${result} "
}

PANE_IND=$(build_segments "" "default")
WIN_IND=$(build_segments "window_" "default")
INDICATOR="#{?#{@claude_state},${PANE_IND},#{?#{@claude_window_state},${WIN_IND}, }}"

# Strip previously-injected indicator block. The indicator sits between
# #{?#{@claude_state}... and #I. Nested #{...} makes regex unreliable,
# so we use python string slicing: find marker start, rfind #I, splice.
strip_claude_indicator() {
    python3 -c "
import sys
s = sys.stdin.read()
marker = '#{?#{@claude_state}'
idx = s.find(marker)
if idx < 0:
    print(s, end='')
else:
    suffix_marker = '#I'
    suffix_idx = s.rfind(suffix_marker)
    if suffix_idx > idx:
        print(s[:idx] + s[suffix_idx:], end='')
    else:
        print(s, end='')
" <<< "$1"
}

# Get current formats (may contain old indicators)
CURRENT_WSCF=$(tmux show-option -gv window-status-current-format 2>/dev/null)

# Strip old indicators to get clean base
BASE_WSF=$(strip_claude_indicator "$CURRENT_WSF")
BASE_WSCF=$(strip_claude_indicator "$CURRENT_WSCF")

# For CURRENT window: extract theme's text color and restore to THAT instead of default
CURRENT_FG=$(echo "$BASE_WSCF" | sed 's/#I.*//' | grep -oP 'fg=#[0-9a-fA-F]{6}' | tail -1 | cut -d= -f2)
CURRENT_FG="${CURRENT_FG:-default}"

PANE_IND_CUR=$(build_segments "" "$CURRENT_FG")
WIN_IND_CUR=$(build_segments "window_" "$CURRENT_FG")
INDICATOR_CURRENT="#{?#{@claude_state},${PANE_IND_CUR},#{?#{@claude_window_state},${WIN_IND_CUR}, }}"

# Insert before #I; strip #F (last-window flag is visual noise)
NEW_WSF=$(echo "$BASE_WSF" | sed "s|#F||" | sed "s|#I|${INDICATOR}#I|")
NEW_WSCF=$(echo "$BASE_WSCF" | sed "s|#F||" | sed "s|#I|${INDICATOR_CURRENT}#I|")

if [ "$NEW_WSF" = "$BASE_WSF" ]; then
    echo "claude-tmux: WARNING — #I not found in window-status-format" >&2
    exit 1
fi

# Apply format strings immediately (in-memory)
tmux set -g window-status-format "$NEW_WSF"
tmux set -g window-status-current-format "$NEW_WSCF"
tmux set -g status-fg "#8a8a8a"

# --- Session persona icon in status-left ---
if [ -f "$THEME_FILE" ] && command -v jq >/dev/null 2>&1; then
    CURRENT_SL=$(tmux show-option -gv status-left 2>/dev/null)
    if ! echo "$CURRENT_SL" | grep -q "@session_icon"; then
        if echo "$CURRENT_SL" | grep -q '✨'; then
            NEW_SL=$(echo "$CURRENT_SL" | sed 's|✨|#{?#{@session_icon},#{@session_icon},✨}|')
            tmux set -g status-left "$NEW_SL"
        fi
        bash "$(dirname "$0")/claude-tmux-session-icons.sh" 2>/dev/null
    fi
fi

# Terminal title (for Alacritty/WezTerm window title bars)
if [ "$(tmux show-option -gqv @claude_tmux_set_titles 2>/dev/null)" != "off" ]; then
    tmux set -g set-titles on
    tmux set -g set-titles-string "#S | ${INDICATOR}#I:#W"
fi

# --- Write persistent dynamic config ---
# Survives chezmoi apply + tmux source-file. Written AFTER all injections.
DYNAMIC_CONF="$HOME/.config/tmux/plugins-dynamic.conf"
FINAL_SL=$(tmux show-option -gv status-left 2>/dev/null)
cat > "$DYNAMIC_CONF" << DYNEOF
# Generated by claude-tmux-setup.sh — do not edit manually
# Regenerated on every tmux startup or theme change
# chezmoi ignores this file (.chezmoiignore)
set -g window-status-format "$NEW_WSF"
set -g window-status-current-format "$NEW_WSCF"
set -g status-fg "#8a8a8a"
set -g status-left "$FINAL_SL"
DYNEOF

# --- END format string injection ---

# --- Split-pane indicator preservation ---
# When focus moves between panes, scan the window for any Claude pane and copy
# its state to window-level fallback options. The format string cascades:
# pane @claude_state -> window @claude_window_state -> empty.
WINDOW_STATE_SCRIPT="$(cd "$(dirname "$0")" && pwd)/tmux-window-state.sh"
if [ -f "$WINDOW_STATE_SCRIPT" ]; then
    # Unconditional re-registration: unset then set on every run, so a stale
    # path from a prior install location can never survive a re-run. A
    # "only if not already registered" guard here previously masked exactly
    # that case after the 2026-07-13 graduation to ~/Workspace/claude-tmux.
    tmux set-hook -gu after-select-pane 2>/dev/null
    tmux set-hook -gu after-select-window 2>/dev/null
    # after-select-pane: fires on pane switch within a window
    tmux set-hook -ga after-select-pane "run-shell -b '$WINDOW_STATE_SCRIPT'"
    # after-select-window: fires on window switch — needed for unread→read
    # (visiting an unread window via Ctrl-B N doesn't trigger after-select-pane)
    tmux set-hook -ga after-select-window "run-shell -b '$WINDOW_STATE_SCRIPT'"
fi

# Self-healing: clear stale indicators when a pane's process dies
# Uses tmux native conditional — only fires if the dying pane had an indicator
# Also notifies voice arbiter to clean up voice state for the dead pane
tmux set-hook -g pane-died 'if -F "#{@claude_state}" "set -pu @claude_state ; set -pu @claude_color ; set -pu @claude_persona ; set -pu @claude_hook ; set -pu @claude_voice ; set -pu @claude_focus ; run-shell -b \"echo {\\\"type\\\":\\\"pane_died\\\",\\\"pane_id\\\":\\\"#{pane_id}\\\"} | socat -t0.1 - UNIX-CONNECT:$HOME/.claude/local/voice/arbiter.sock 2>/dev/null || true\""'

# --- Layout-change re-save (Fix 5, 2026-04-27) -----------------------------
# Position-keyed state files (legion:5.1.state) drift whenever windows OR
# panes are added/removed/swapped. Without re-save, state files at old
# positions go stale and post-reboot restore falls through to the fallback
# chain. These hooks call claude-resave-state.sh to refresh state files at
# current geometry. The dedupe in save_session_state collapses old keys.
#
# Hook list covers both window-level events and pane-level events:
#   - Window: kill, renumber, rename, swap, move, link, unlink
#   - Pane:   split-window (creates pane), pane-exited (kills pane)
# Pane-level events matter because pane_index renumbers when sibling panes
# are added/removed within the same window.
#
# Unconditional re-registration on every run (see rationale above the
# after-select-pane/-window block): the previous marker-option guard
# (@claude_resave_hooks_installed) recorded only that registration had
# happened once, never whether the registered path was still valid, so it
# masked a stale post-graduation path indefinitely. Some event names below
# are not recognized by every tmux version ("invalid option"); that's
# expected and harmless, so stderr is discarded for both the clear and the
# re-register pass.
RESAVE_SCRIPT="$(cd "$(dirname "$0")" && pwd)/claude-resave-state.sh"
if [ -f "$RESAVE_SCRIPT" ]; then
    for evt in after-kill-window after-renumber-window after-rename-window \
               after-swap-window after-move-window after-link-window \
               after-unlink-window after-split-window pane-exited; do
        tmux set-hook -gu "$evt" 2>/dev/null
        tmux set-hook -ga "$evt" "run-shell -b '$RESAVE_SCRIPT'" 2>/dev/null
    done
fi
