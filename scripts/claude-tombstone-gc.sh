#!/usr/bin/env bash
# claude-tombstone-gc.sh — Validity-based prune of stale claude-session state files.
#
# the session-continuity work.
#
# Scans STATE_DIR for *.state files. For each:
#   - parses SESSION_ID with grep/cut (never source — injection safety)
#   - if SESSION_ID is empty (pre-Fix-6 corruption) -> tombstone with reason=empty_session_id
#   - if PROJECTS_DIR/$SESSION_ID.jsonl is missing -> tombstone with reason=jsonl_missing
#   - else -> keep
#
# Tombstoning is a MOVE, not a DELETE. Files go to:
#   ${STATE_DIR}/.tombstones/<basename>.<UTC-ISO-timestamp>
#
# A sidecar log records the reason without modifying the moved file's
# contents (so recovery via plan §10 stays trivially correct).
#
# Triggered by: claude-save-sessions.sh (every 15 min via tmux-resurrect post-save).
# Disable: LEGION_TOMBSTONE_GC=0
#
# Idempotent. Concurrency-safe — flock on the existing $STATE_DIR/.lock.
# Best-effort: if telemetry log path is unwritable, GC still succeeds.

set -uo pipefail

[[ "${LEGION_TOMBSTONE_GC:-1}" == "0" ]] && exit 0

# Source the JSONL lookup helper. Phase 0 fix: cross-project resolution
# replaces a hardcoded per-user projects path that
# false-positived non-$HOME panes (the 2026-04-29 root cause).
_LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../lib" && pwd)"
# shellcheck source=../lib/jsonl.sh
[ -f "$_LIB_DIR/jsonl.sh" ] && source "$_LIB_DIR/jsonl.sh"

STATE_DIR="${STATE_DIR:-$HOME/.local/share/tmux/claude-sessions}"
TOMBSTONE_DIR="${TOMBSTONE_DIR:-$STATE_DIR/.tombstones}"
TOMBSTONE_META="${TOMBSTONE_META:-$TOMBSTONE_DIR/tombstone-meta.jsonl}"
LOCK_FILE="${LOCK_FILE:-$STATE_DIR/.lock}"
RESTORE_LOG="${RESTORE_LOG:-$HOME/.claude/local/tmux/restore.log}"

mkdir -p "$STATE_DIR" "$TOMBSTONE_DIR"
mkdir -p "$(dirname "$RESTORE_LOG")" 2>/dev/null || true

# move_to_tombstone <state_file> <reason>
move_to_tombstone() {
    local state_file="$1"
    local reason="$2"
    local basename
    basename=$(basename "$state_file")
    local ts
    ts=$(date -u +%Y%m%dT%H%M%SZ)
    local target="$TOMBSTONE_DIR/${basename}.${ts}"
    if mv -f "$state_file" "$target" 2>/dev/null; then
        # Sidecar reason log — JSON Lines, append-only.
        local meta_ts
        meta_ts=$(date -u --iso-8601=seconds 2>/dev/null || date -u +%Y-%m-%dT%H:%M:%SZ)
        printf '{"ts":"%s","tombstone":"%s","origin":"%s","reason":"%s"}\n' \
            "$meta_ts" "$target" "$state_file" "$reason" \
            >> "$TOMBSTONE_META" 2>/dev/null || true
        return 0
    fi
    return 1
}

scan_and_classify() {
    local moved=0
    local kept=0
    local empties=0
    local missing=0
    local state_file
    for state_file in "$STATE_DIR"/*.state; do
        [ -f "$state_file" ] || continue
        local session_id
        session_id=$(grep -E '^SESSION_ID=' "$state_file" 2>/dev/null | head -1 | cut -d= -f2-)
        if [ -z "$session_id" ]; then
            if move_to_tombstone "$state_file" "empty_session_id"; then
                moved=$((moved + 1))
                empties=$((empties + 1))
            fi
            continue
        fi
        # Cross-project JSONL lookup. session_jsonl_exists searches all
        # ~/.claude/projects/*/ for $session_id.jsonl. If the helper failed
        # to source (lib path unreachable), conservatively keep the file —
        # better stale than wrongly tombstoned.
        if ! type session_jsonl_exists >/dev/null 2>&1; then
            kept=$((kept + 1))
            continue
        fi
        if ! session_jsonl_exists "$session_id"; then
            if move_to_tombstone "$state_file" "jsonl_missing"; then
                moved=$((moved + 1))
                missing=$((missing + 1))
            fi
        else
            kept=$((kept + 1))
        fi
    done
    log_event "$kept" "$moved" "$empties" "$missing"
}

log_event() {
    [[ "${LEGION_RESTORE_TELEMETRY:-1}" == "0" ]] && return 0
    local kept="$1" moved="$2" empties="$3" missing="$4"
    local ts
    ts=$(date -u --iso-8601=seconds 2>/dev/null || date -u +%Y-%m-%dT%H:%M:%SZ)
    printf '{"ts":"%s","event":"tombstone_gc","kept":%d,"moved":%d,"empty_session_id":%d,"jsonl_missing":%d}\n' \
        "$ts" "$kept" "$moved" "$empties" "$missing" >> "$RESTORE_LOG" 2>/dev/null || true
}

# Acquire exclusive lock. If held (concurrent invocation from a previous
# 15-min cycle still running), bail quietly — next cycle will catch up.
exec 9>"$LOCK_FILE" 2>/dev/null || exit 0
if command -v flock >/dev/null 2>&1; then
    if ! flock -n -x 9; then
        exit 0
    fi
fi

scan_and_classify
exit 0
