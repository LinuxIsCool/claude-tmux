#!/usr/bin/env bash
# claude-tmux hook — forwards events to tmux-arbiter daemon.
# Falls back to direct tmux write if daemon is not running.
#
# Fast path (daemon running):   ~5ms — socat write, fire-and-forget
# Fallback path (no daemon):    ~50ms — bash state resolution via common.sh
#
# Never returns nonzero — Claude Code treats hook failure as fatal.

EVENT="${1:-}"
STDIN_JSON=$(timeout 2 cat 2>/dev/null || echo '{}')
SOCK="${HOME}/.claude/local/tmux/arbiter.sock"

# Self-heal: if TMUX_PANE is not in env, resolve from tmux.
# Claude Code does not always pass TMUX_PANE to hook subprocesses (observed on --resume).
if [ -z "${TMUX_PANE:-}" ] && [ -n "${TMUX:-}" ]; then
    TMUX_PANE=$(tmux display-message -p '#{pane_id}' 2>/dev/null)
fi

# --- Fast path: forward to daemon (fire-and-forget) ---
# socat -t 0.1: 100ms connect timeout — if daemon not up, fails fast
# &: background the socat write so hook returns immediately
if [ -S "$SOCK" ]; then
    printf '%s\n' \
        "{\"type\":\"event\",\"event\":\"${EVENT}\",\"pane\":\"${TMUX_PANE:-}\",\"data\":${STDIN_JSON}}" \
        | socat -t 0.1 - "UNIX-CONNECT:${SOCK}" 2>/dev/null &

    # Session ID persistence still runs in fallback AND daemon path.
    # The arbiter does not write resurrect state files — that's hook.sh's job.
    if [ "$EVENT" = "SessionStart" ] && [ -n "${TMUX_PANE:-}" ]; then
        SOURCE=$(printf '%s' "$STDIN_JSON" | jq -r '.source // empty' 2>/dev/null)
        if [ "$SOURCE" != "compact" ]; then
            SESSION_ID=$(printf '%s' "$STDIN_JSON" | jq -r '.session_id // empty' 2>/dev/null)
            if [ -n "$SESSION_ID" ]; then
                PLUGIN_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
                source "${PLUGIN_DIR}/lib/common.sh" 2>/dev/null
                save_session_state "$TMUX_PANE" "$SESSION_ID"
            fi
            # Update session icons in background
            bash "$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/scripts/claude-tmux-session-icons.sh" 2>/dev/null &
        fi
    fi

    echo '{}'
    exit 0
fi

# --- Fallback path: direct tmux write ---
# Used when daemon is not running. Preserves all behavior from before the refactor.
source "$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/lib/common.sh"

# Debug logging (opt-in via @claude_tmux_debug pane option)
if [ -n "${TMUX_PANE:-}" ] && [ "$(tmux show-option -pqv -t "${TMUX_PANE}" @claude_tmux_debug 2>/dev/null)" = "on" ]; then
    printf '%s %s %s [fallback]\n' "$(date +%H:%M:%S.%3N)" "$EVENT" "${TMUX_PANE}" >> /tmp/claude-tmux-debug.log
fi

# Compaction guard
if [ "$EVENT" = "SessionStart" ]; then
    SOURCE=$(printf '%s' "$STDIN_JSON" | jq -r '.source // empty' 2>/dev/null)
    if [ "$SOURCE" = "compact" ]; then
        echo '{}'
        exit 0
    fi
fi

# Notification dispatch
if [ "$EVENT" = "Notification" ]; then
    NTYPE=$(printf '%s' "$STDIN_JSON" | jq -r '.notification_type // empty' 2>/dev/null)
    if [ -n "$NTYPE" ] && [ -n "$THEME_FILE" ]; then
        MAPPED_STATE=$(jq -r --arg nt "$NTYPE" '.notification_states[$nt] // empty' "$THEME_FILE" 2>/dev/null)
        resolve_state "${MAPPED_STATE:-nudge}"
    else
        resolve_state "nudge"
    fi
else
    resolve_hook "$EVENT"
fi

case "$STATE_NAME" in
    clear)
        clear_indicator "${TMUX_PANE:-}"
        ;;
    *)
        set_indicator "${TMUX_PANE:-}" "$STATE_COLOR" "$STATE_GLYPH"

        # Persona extraction
        AGENT_TYPE=$(printf '%s' "$STDIN_JSON" | jq -r '.agent_type // empty' 2>/dev/null)
        if [ -n "$AGENT_TYPE" ] && [ -n "${TMUX_PANE:-}" ]; then
            PERSONA_KEY="${AGENT_TYPE##*:}"
            PERSONA_EMOJI=$(get_persona_emoji "$PERSONA_KEY")
            [ -n "$PERSONA_EMOJI" ] && tmux set -p -t "${TMUX_PANE}" @claude_persona "$PERSONA_EMOJI" 2>/dev/null
        fi

        # Session ID persistence + session icons
        if [ "$EVENT" = "SessionStart" ]; then
            SESSION_ID=$(printf '%s' "$STDIN_JSON" | jq -r '.session_id // empty' 2>/dev/null)
            [ -n "$SESSION_ID" ] && [ -n "${TMUX_PANE:-}" ] && save_session_state "$TMUX_PANE" "$SESSION_ID"
            bash "$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/scripts/claude-tmux-session-icons.sh" 2>/dev/null &
        fi
        ;;
esac

echo '{}'
