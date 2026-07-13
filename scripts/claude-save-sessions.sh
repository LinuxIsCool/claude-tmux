#!/usr/bin/env bash
# claude-save-sessions.sh — Resurrect save hook
# Consolidates per-pane .state files into session-map.tsv
# Called by tmux-continuum every 15 min via @resurrect-hook-post-save-layout
#
# Also prunes state files older than 7 days to prevent accumulation.

STATE_DIR="${HOME}/.local/share/tmux/claude-sessions"
MAP_FILE="${STATE_DIR}/session-map.tsv"

mkdir -p "$STATE_DIR"

# Prune state files older than 7 days
find "$STATE_DIR" -name "*.state" -mtime +7 -delete 2>/dev/null

# Rebuild session-map.tsv from current .state files
TMP="${MAP_FILE}.tmp.$$"
printf '# tmux_session\twindow\tpane\tsession_id\ttimestamp\n' > "$TMP"

for state_file in "${STATE_DIR}"/*.state; do
    [ -f "$state_file" ] || continue
    # Parse with grep/cut (safe — no eval/source)
    TMUX_SESSION=$(grep '^TMUX_SESSION=' "$state_file" | cut -d= -f2)
    WINDOW=$(grep '^WINDOW=' "$state_file" | cut -d= -f2)
    PANE=$(grep '^PANE=' "$state_file" | cut -d= -f2)
    SESSION_ID=$(grep '^SESSION_ID=' "$state_file" | cut -d= -f2)
    TIMESTAMP=$(grep '^TIMESTAMP=' "$state_file" | cut -d= -f2)
    [ -z "$SESSION_ID" ] && continue
    printf '%s\t%s\t%s\t%s\t%s\n' \
        "$TMUX_SESSION" "$WINDOW" "$PANE" "$SESSION_ID" "$TIMESTAMP" >> "$TMP"
done

mv "$TMP" "$MAP_FILE"

# Tombstone GC — . Validity-based
# prune: state files whose SESSION_ID has no alive JSONL get moved to
# .tombstones/. Disable via LEGION_TOMBSTONE_GC=0. Idempotent + flock-safe.
GC_SCRIPT="$(dirname "$0")/claude-tombstone-gc.sh"
[ -x "$GC_SCRIPT" ] && "$GC_SCRIPT" || true
