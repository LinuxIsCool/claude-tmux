#!/usr/bin/env bash
# claude-tmux hook — forwards events to tmux-arbiter daemon.
# Falls back to direct tmux write if daemon is not running.
#
# Sends bounded lifecycle metadata. Tool bodies never enter the transport.
# The socat write is fire-and-forget; all paths remain nonfatal.
#
# Never returns nonzero — Claude Code treats hook failure as fatal.

EVENT="${1:-}"
SOCK="${HOME}/.claude/local/tmux/arbiter.sock"

# Parse once with bounded input/output, preserving exact server/session identity.
# Missing/invalid bindings are a no-op. Never infer a pane from current focus.
# A malformed or oversized input is a no-op rather than malformed NDJSON.
PLUGIN_DIR="$(dirname "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")")"
PAYLOAD=$(timeout 2 python3 "${PLUGIN_DIR}/scripts/hook-payload.py" \
    "$EVENT" "${TMUX_PANE:-}" "${TMUX:-}" 2>/dev/null) || PAYLOAD=""
if [ -z "$PAYLOAD" ]; then
    echo '{}'
    exit 0
fi

# Only session persistence and the fallback need individual data fields.
STDIN_JSON='{}'
if [ "$EVENT" = "SessionStart" ] || [ ! -S "$SOCK" ]; then
    STDIN_JSON=$(printf '%s' "$PAYLOAD" | jq -c '.data' 2>/dev/null) || STDIN_JSON='{}'
fi

# --- Fast path: forward to daemon (fire-and-forget) ---
# socat -t 0.1: 100ms connect timeout — if daemon not up, fails fast
# &: background the socat write so hook returns immediately
if [ -S "$SOCK" ]; then
    printf '%s\n' "$PAYLOAD" \
        | socat -t 0.1 - "UNIX-CONNECT:${SOCK}" >/dev/null 2>&1 &

    # Session ID persistence still runs in fallback AND daemon path.
    # The arbiter does not write resurrect state files — that's hook.sh's job.
    if [ "$EVENT" = "SessionStart" ] && [ -n "${TMUX_PANE:-}" ]; then
        SOURCE=$(printf '%s' "$STDIN_JSON" | jq -r '.source // empty' 2>/dev/null)
        if [ "$SOURCE" != "compact" ]; then
            SESSION_ID=$(printf '%s' "$STDIN_JSON" | jq -r '.session_id // empty' 2>/dev/null)
            if [ -n "$SESSION_ID" ]; then
                source "${PLUGIN_DIR}/lib/common.sh" 2>/dev/null
                save_session_state "$TMUX_PANE" "$SESSION_ID"
            fi
            # Update session icons in background
            bash "${PLUGIN_DIR}/scripts/claude-tmux-session-icons.sh" 2>/dev/null &
        fi
    fi

    echo '{}'
    exit 0
fi

# --- Fallback path: direct tmux write ---
# Used when daemon is not running. Preserves all behavior from before the refactor.
source "${PLUGIN_DIR}/lib/common.sh"

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
            bash "${PLUGIN_DIR}/scripts/claude-tmux-session-icons.sh" 2>/dev/null &
        fi
        ;;
esac

echo '{}'
