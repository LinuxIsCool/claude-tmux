#!/usr/bin/env bash
# claude-tmux — shared library
# Sourced by hooks/hook.sh
#
# All visual tokens (colors, glyphs) come from theme.json, the single
# source of truth for the visual system. Nothing is hardcoded here.
# Change theme.json, everything updates.

# Theme resolution: $CLAUDE_TMUX_THEME, then the machine-local override,
# then the theme shipped with the plugin. Local override keeps a private
# persona registry out of the public repo.
_PLUGIN_THEME="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/theme.json"
THEME_FILE="${CLAUDE_TMUX_THEME:-$HOME/.claude/local/tmux/theme.json}"
[ -f "$THEME_FILE" ] || THEME_FILE="$_PLUGIN_THEME"

# --- Dependency guard --------------------------------------------------------
# jq is required. If missing or theme.json absent, show a visible error state
# (red "!") rather than silently falling back to hardcoded values.

THEME_ERROR=""
if ! command -v jq >/dev/null 2>&1; then
    THEME_ERROR="claude-tmux: jq required but not found"
elif [ ! -f "$THEME_FILE" ]; then
    THEME_ERROR="claude-tmux: theme.json not found at $THEME_FILE"
fi

if [ -n "$THEME_ERROR" ]; then
    echo "$THEME_ERROR" >&2
    resolve_state() { STATE_NAME="error"; STATE_COLOR="#f38ba8"; STATE_GLYPH="!"; }
    resolve_hook()  { STATE_NAME="error"; STATE_COLOR="#f38ba8"; STATE_GLYPH="!"; }
    get_persona_emoji() { echo ""; }
    # Fall through — set_indicator etc. still defined below
else

# --- Theme loader -----------------------------------------------------------
# Resolves state → semantic color name → hex color, plus glyph.
# Single jq call per resolution for atomicity and fewer forks.
#
# Usage:  resolve_state "working"  →  sets STATE_COLOR and STATE_GLYPH
#         resolve_state "idle"     →  sets STATE_COLOR and STATE_GLYPH
#         resolve_hook  "Stop"     →  sets STATE_COLOR, STATE_GLYPH, STATE_NAME

resolve_state() {
    local state="$1"
    local result
    result=$(jq -r --arg s "$state" '
        .states[$s] as $st |
        [(.semantic_colors[$st.semantic] // ""), ($st.glyph // "")]
        | @tsv
    ' "$THEME_FILE" 2>/dev/null)
    STATE_NAME="$state"
    STATE_COLOR="${result%%	*}"
    STATE_GLYPH="${result#*	}"
    # Visible error if resolution produced nothing (bad state name or malformed JSON)
    [ -z "$STATE_COLOR" ] && STATE_COLOR="#f38ba8" && STATE_GLYPH="!"
}

resolve_hook() {
    local hook_event="$1"
    local state
    state=$(jq -r ".hook_to_state.\"$hook_event\"" "$THEME_FILE" 2>/dev/null)
    if [ "$state" = "clear" ]; then
        STATE_NAME="clear"
        STATE_COLOR=""
        STATE_GLYPH=""
        return
    fi
    if [ "$state" = "null" ] || [ -z "$state" ]; then
        echo "claude-tmux: unknown hook event '$hook_event' — not in theme.json hook_to_state" >&2
        STATE_NAME="error"
        STATE_COLOR="#f38ba8"
        STATE_GLYPH="!"
        return
    fi
    resolve_state "$state"
}

# Persona emoji lookup (returns empty string if persona unknown)
# Primary: claude-emoji DB, Fallback: theme.json
get_persona_emoji() {
    local persona="$1"
    local emoji=""
    if [ -f "$EMOJI_FLAT" ]; then
        emoji=$(jq -r ".\"persona:$persona\" // empty" "$EMOJI_FLAT" 2>/dev/null)
    fi
    if [ -z "$emoji" ]; then
        emoji=$(jq -r ".personas.\"$persona\".emoji // empty" "$THEME_FILE" 2>/dev/null)
    fi
    echo "$emoji"
}

# Session emoji: map session name "{prefix}{persona}" → persona emoji
# Prefix defaults to "legion-" (historical); override with $CLAUDE_TMUX_SESSION_PREFIX.
# Primary source: claude-emoji flat export (single source of truth)
# Fallback: theme.json personas section
# Returns default emoji if session name doesn't match a persona
EMOJI_FLAT="${HOME}/.claude/local/emoji/emoji_flat.json"

PERSONA_ENV="$HOME/.claude/local/emoji/persona-emoji.env"

get_session_emoji() {
    local session_name="$1"
    local default_emoji="${2:-✨}"
    local persona_key="${session_name#${CLAUDE_TMUX_SESSION_PREFIX:-legion-}}"
    if [ "$persona_key" != "$session_name" ] && [ -n "$persona_key" ]; then
        local emoji=""
        # Primary: persona-emoji.env (fast grep, no jq needed)
        if [ -f "$PERSONA_ENV" ]; then
            emoji=$(grep "^${persona_key}=" "$PERSONA_ENV" 2>/dev/null | cut -d'"' -f2)
        fi
        # Fallback: emoji_flat.json via jq
        if [ -z "$emoji" ] && [ -f "$EMOJI_FLAT" ]; then
            emoji=$(jq -r ".\"persona:$persona_key\" // empty" "$EMOJI_FLAT" 2>/dev/null)
        fi
        # Last resort: theme.json
        if [ -z "$emoji" ]; then
            emoji=$(jq -r ".personas.\"$persona_key\".emoji // empty" "$THEME_FILE" 2>/dev/null)
        fi
        if [ -n "$emoji" ]; then
            echo "$emoji"
            return
        fi
    fi
    echo "$default_emoji"
}

fi  # end THEME_ERROR guard

# State persistence — maps tmux pane positions to Claude session IDs
# Survives reboots so claude-restore.sh can resume after tmux-resurrect
STATE_DIR="${HOME}/.local/share/tmux/claude-sessions"

# find_session_jsonl helper is defined in lib/jsonl.sh (Phase 0 deliverable
# 0.1 of Backlog 324). Sourced here so common.sh consumers (the hook fast path)
# and standalone scripts (claude-restore.sh, claude-tombstone-gc.sh) share one
# canonical implementation.
_JSONL_LIB="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/jsonl.sh"
[ -f "$_JSONL_LIB" ] && source "$_JSONL_LIB"

# Set indicator on a tmux pane via native pane options
set_indicator() {
    local pane_id="$1" color="$2" char="$3"
    [ -z "$pane_id" ] && return 0
    tmux set -p -t "$pane_id" @claude_state "$char" 2>/dev/null
    tmux set -p -t "$pane_id" @claude_color "$color" 2>/dev/null
}

# Remove indicator from a tmux pane
clear_indicator() {
    local pane_id="$1"
    [ -z "$pane_id" ] && return 0
    tmux set -pu -t "$pane_id" @claude_state 2>/dev/null
    tmux set -pu -t "$pane_id" @claude_color 2>/dev/null
    tmux set -pu -t "$pane_id" @claude_persona 2>/dev/null
    tmux set -pu -t "$pane_id" @claude_hook 2>/dev/null
    tmux set -pu -t "$pane_id" @claude_voice 2>/dev/null
    tmux set -pu -t "$pane_id" @claude_focus 2>/dev/null
}

# Persist session_id for a pane, keyed by session:window.pane
# Called on SessionStart so state survives reboots for resurrect restore
#
# Hardened 2026-04-27 for the session-restore reliability work
# (2026-04-24 session-restore incident):
#   Fix 3 — after writing the state file at the new key, scan other state
#           files for the same SESSION_ID and remove them. Latest write
#           wins; prevents the duplicate-id zombie reads that produced
#           multi-pane collisions on 2026-04-24.
#   Fix 5 — also write the SESSION_ID into a tmux pane option
#           (@claude_session_id), so an out-of-band re-save script can
#           refresh state files after layout changes without needing the
#           Claude process to re-emit SessionStart.
#   Fix 6 — atomic-write via tmpfile + size guard, so we never leave a
#           zero-byte state file even if printf is interrupted.
save_session_state() {
    local pane_id="$1" session_id="$2"
    [ -z "$pane_id" ] || [ -z "$session_id" ] && return 0
    mkdir -p "$STATE_DIR"
    local session_name window_index pane_index
    session_name=$(tmux display -p -t "$pane_id" '#{session_name}' 2>/dev/null) || return 0
    window_index=$(tmux display -p -t "$pane_id" '#{window_index}' 2>/dev/null) || return 0
    pane_index=$(tmux display -p -t "$pane_id" '#{pane_index}' 2>/dev/null) || return 0
    # Empty values from tmux mean the pane vanished mid-call — bail
    [ -z "$session_name" ] || [ -z "$window_index" ] || [ -z "$pane_index" ] && return 0

    local key="${session_name}:${window_index}.${pane_index}"
    local target="${STATE_DIR}/${key}.state"
    local tmp="${target}.tmp.$$"

    # Concurrency guard — flock on the STATE_DIR makes the
    # write+set-pane-option+dedupe sequence atomic across concurrent
    # SessionStart calls during a restore-storm. Without this, two panes
    # firing SessionStart with the same session_id (which DID happen on
    # 2026-04-24) could both pass the dedupe-grep before either had
    # written, then mutually unlink each other's just-written file.
    #
    # flock fd 9 over $STATE_DIR; release on exit of subshell. If flock
    # is somehow unavailable, we fall through unguarded — better racy
    # than non-functional.
    if command -v flock >/dev/null 2>&1; then
        exec 9>"${STATE_DIR}/.lock" 2>/dev/null
        flock -x 9 2>/dev/null
    fi

    # Fix 6 — write to tmpfile, validate non-empty, then atomic mv.
    # This is bulletproof against interrupted printfs leaving 0-byte files.
    printf 'SESSION_ID=%s\nTMUX_SESSION=%s\nWINDOW=%s\nPANE=%s\nTIMESTAMP=%s\n' \
        "$session_id" "$session_name" "$window_index" "$pane_index" "$(date +%s)" \
        > "$tmp" 2>/dev/null
    if [ ! -s "$tmp" ]; then
        rm -f "$tmp"
        exec 9>&- 2>/dev/null   # release lock fd if held
        return 1
    fi
    mv -f "$tmp" "$target"

    # Fix 5 — store SESSION_ID on the pane itself for layout-change re-save.
    # Quietly ignore failure (pane may have died, that's fine).
    tmux set -p -t "$pane_id" @claude_session_id "$session_id" 2>/dev/null

    # Fix 3 — dedupe: if any OTHER state file contains this SESSION_ID, remove
    # it. Avoids resume-collisions when a session moves between panes/windows.
    # Use grep -Fx with the exact SESSION_ID line to avoid prefix matches.
    local other
    for other in "$STATE_DIR"/*.state; do
        [ "$other" = "$target" ] && continue
        [ -f "$other" ] || continue
        if grep -qFx "SESSION_ID=${session_id}" "$other" 2>/dev/null; then
            rm -f "$other"
        fi
    done

    exec 9>&- 2>/dev/null   # release lock fd
}

# Remove state file for a pane (call on intentional session close, not crash)
clear_session_state() {
    local pane_id="$1"
    [ -z "$pane_id" ] && return 0
    local session_name window_index pane_index
    session_name=$(tmux display -p -t "$pane_id" '#{session_name}' 2>/dev/null) || return 0
    window_index=$(tmux display -p -t "$pane_id" '#{window_index}' 2>/dev/null) || return 0
    pane_index=$(tmux display -p -t "$pane_id" '#{pane_index}' 2>/dev/null) || return 0
    rm -f "${STATE_DIR}/${session_name}:${window_index}.${pane_index}.state"
}
