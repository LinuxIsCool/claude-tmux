#!/usr/bin/env bash
# tmux-window-state.sh — Copy Claude pane state to window-level fallback options.
# Called by after-select-pane hook for split-pane indicator preservation.
#
# Scans all panes in the current window for @claude_state.
# If found, copies to @claude_window_state (window-level option).
# If not found, clears the window-level options.
#
# This ensures the window tab always shows an indicator if ANY pane
# in the window has Claude running, even when a non-Claude pane is focused.

# --- Unread → Read transition ---
# If the focused pane is in "unread" state (lavender), mark it as "read" (idle blue).
# The user is now looking at the response.
UNREAD_COLOR="#b4befe"
IDLE_COLOR="#89b4fa"
FOCUSED_COLOR=$(tmux display-message -p '#{@claude_color}' 2>/dev/null)
if [ "$FOCUSED_COLOR" = "$UNREAD_COLOR" ]; then
    tmux set -p @claude_color "$IDLE_COLOR" 2>/dev/null
    # Also update the arbiter's state file via socket (fire-and-forget)
    SOCK="$HOME/.claude/local/tmux/arbiter.sock"
    PANE_ID=$(tmux display-message -p '#{pane_id}' 2>/dev/null)
    if [ -S "$SOCK" ] && [ -n "$PANE_ID" ]; then
        printf '%s\n' "{\"type\":\"event\",\"event\":\"PaneRead\",\"pane\":\"${PANE_ID}\",\"data\":{}}" \
            | socat -t 0.1 - "UNIX-CONNECT:${SOCK}" 2>/dev/null &
    fi
fi

# Scan all panes for Claude state. Prefer higher-priority states:
# error > asking > agents > working > unread > idle
# Priority encoded by color — unread (#b4befe) beats idle (#89b4fa).
BEST_STATE=""
BEST_COLOR=""
BEST_PERSONA=""
BEST_VOICE=""
BEST_FOCUS=""
BEST_PRIORITY=0

# Priority map (higher = more important for window tab display)
get_priority() {
    case "$1" in
        "#a6e3a1") echo 50 ;;  # working (green)
        "#cba6f7") echo 40 ;;  # agents (mauve)
        "#f9e2af") echo 60 ;;  # asking (yellow)
        "#f38ba8") echo 70 ;;  # error (red)
        "#b4befe") echo 30 ;;  # unread (lavender)
        "#89b4fa") echo 10 ;;  # idle (blue)
        *)         echo 10 ;;  # unknown → idle priority
    esac
}

# Single tmux call to get all pane states at once (instead of N×4 sequential calls)
while IFS='|' read -r pid st cl pe vo fo; do
    [ -n "$st" ] || continue
    pr=$(get_priority "$cl")
    if [ "$pr" -gt "$BEST_PRIORITY" ]; then
        BEST_STATE="$st"
        BEST_COLOR="$cl"
        BEST_PERSONA="$pe"
        BEST_VOICE="$vo"
        BEST_FOCUS="$fo"
        BEST_PRIORITY="$pr"
    fi
done < <(tmux list-panes -F '#{pane_id}|#{@claude_state}|#{@claude_color}|#{@claude_persona}|#{@claude_voice}|#{@claude_focus}' 2>/dev/null)

if [ -n "$BEST_STATE" ]; then
    tmux set -w @claude_window_state "$BEST_STATE" 2>/dev/null
    tmux set -w @claude_window_color "$BEST_COLOR" 2>/dev/null
    if [ -n "$BEST_PERSONA" ]; then
        tmux set -w @claude_window_persona "$BEST_PERSONA" 2>/dev/null
    fi
    if [ -n "$BEST_VOICE" ]; then
        tmux set -w @claude_window_voice "$BEST_VOICE" 2>/dev/null
    else
        tmux set -wu @claude_window_voice 2>/dev/null
    fi
    if [ -n "$BEST_FOCUS" ]; then
        tmux set -w @claude_window_focus "$BEST_FOCUS" 2>/dev/null
    else
        tmux set -wu @claude_window_focus 2>/dev/null
    fi
else
    tmux set -wu @claude_window_state 2>/dev/null
    tmux set -wu @claude_window_color 2>/dev/null
    tmux set -wu @claude_window_persona 2>/dev/null
    tmux set -wu @claude_window_voice 2>/dev/null
    tmux set -wu @claude_window_focus 2>/dev/null
fi

exit 0
