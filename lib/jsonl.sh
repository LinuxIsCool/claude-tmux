#!/usr/bin/env bash
# jsonl.sh — JSONL transcript path resolution helper.
#
# the session-continuity work.
#
# Pre-Phase-0 code hardcoded `~/.claude/projects/<encoded-home>/` as the
# transcript root. Claude Code actually encodes the launch cwd into the
# project dir name:
#   ~/some-repo/                       -> -home-user-some-repo/
#   ~/Workspace/project/v2/            -> -home-user-Workspace-project-v2/
#   ~/                                 -> -home-user/
#
# A pane launched in any non-$HOME cwd has its JSONL in a different per-cwd
# directory. Hardcoding one encoded dir silently false-negatived every such
# pane on resume — this is the actual root cause of the 2026-04-29 silent
# fall-through (window 2 cwd=~/some-repo).
#
# The helper searches *all* per-cwd project directories under the projects
# root. First-match wins. Tests override the root via PROJECTS_ROOT.
#
# This file deliberately has zero top-level side effects so it can be sourced
# from any script (hot-path hooks, restore-time scripts, GC) without paying
# theme/jq init cost.

# find_session_jsonl <session_id>
#
# Locates a Claude session's JSONL transcript file across all per-cwd
# project directories. Returns:
#   0 + prints absolute JSONL path on first match
#   1 with no output if not found
find_session_jsonl() {
    local sid="$1"
    [ -z "$sid" ] && return 1
    local projects_root="${PROJECTS_ROOT:-$HOME/.claude/projects}"
    [ -d "$projects_root" ] || return 1
    local match
    match=$(find "$projects_root" -maxdepth 2 -name "${sid}.jsonl" -type f 2>/dev/null | head -1)
    if [ -n "$match" ]; then
        printf '%s\n' "$match"
        return 0
    fi
    return 1
}

# session_jsonl_exists <session_id>
#
# Boolean wrapper. Returns 0 (yes) / 1 (no). Discards the path. Used in
# `if session_jsonl_exists "$sid"; then ...` patterns where the caller
# only needs liveness, not the path.
session_jsonl_exists() {
    find_session_jsonl "$1" >/dev/null 2>&1
}

# log_restore_step <step_number> <outcome> [details]
#
# the session-continuity work. Append-only JSON Lines record of
# every claude-restore.sh decision. One file at $RESTORE_LOG. Best-effort:
# if the log path is unwritable, restore continues normally (telemetry
# never blocks restore).
#
# Disable: LEGION_RESTORE_TELEMETRY=0
#
# Required env (caller-provided): TMUX_PANE, SESSION_NAME, WINDOW_INDEX,
# PANE_INDEX, PWD. Missing values become "unknown".
#
# Schema:
#   {"ts":"<ISO8601>","pane_id":"%17","key":"main:2.1","cwd":"/home/user/some-repo",
#    "step":1,"outcome":"resumed","details":"session_id=fbac1b04"}
#
# step:    1=state-file lookup, 2=resurrect-snapshot fallback,
#          3=project-marker --continue, 4=fresh launch
# outcome: resumed | fresh | continue_attempt | continue_failed_to_fresh | error
log_restore_step() {
    [[ "${LEGION_RESTORE_TELEMETRY:-1}" == "0" ]] && return 0
    local step="$1"
    local outcome="$2"
    local details="${3:-}"
    local log_file="${RESTORE_LOG:-$HOME/.claude/local/tmux/restore.log}"
    local ts
    ts=$(date -u --iso-8601=seconds 2>/dev/null || date -u +%Y-%m-%dT%H:%M:%SZ)
    local pane_id="${TMUX_PANE:-unknown}"
    local sess="${SESSION_NAME:-unknown}"
    local win="${WINDOW_INDEX:-unknown}"
    local pidx="${PANE_INDEX:-unknown}"
    local cwd="${PWD:-unknown}"
    mkdir -p "$(dirname "$log_file")" 2>/dev/null || return 0
    if command -v jq >/dev/null 2>&1; then
        jq -nc \
            --arg ts "$ts" --arg pid "$pane_id" \
            --arg key "${sess}:${win}.${pidx}" --arg cwd "$cwd" \
            --argjson step "$step" --arg outcome "$outcome" \
            --arg details "$details" \
            '{ts:$ts,pane_id:$pid,key:$key,cwd:$cwd,step:$step,outcome:$outcome,details:$details}' \
            >> "$log_file" 2>/dev/null || true
    else
        # Fallback: best-effort manual JSON. Escapes only the most common
        # hazards (backslash, double-quote). Acceptable since the fields
        # we control (cwd, details) are tightly scoped.
        local esc_details="${details//\\/\\\\}"
        esc_details="${esc_details//\"/\\\"}"
        local esc_cwd="${cwd//\\/\\\\}"
        esc_cwd="${esc_cwd//\"/\\\"}"
        printf '{"ts":"%s","pane_id":"%s","key":"%s:%s.%s","cwd":"%s","step":%s,"outcome":"%s","details":"%s"}\n' \
            "$ts" "$pane_id" "$sess" "$win" "$pidx" "$esc_cwd" "$step" "$outcome" "$esc_details" \
            >> "$log_file" 2>/dev/null || true
    fi
}
