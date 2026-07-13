#!/usr/bin/env bash
# claude-restore.sh — Resume previous Claude session or start fresh
# Called by tmux-resurrect when restoring a pane that had `claude` running.
# Looks up the saved session_id for this pane position, then resumes it.
#
# tmux-resurrect config:
#   set -g @resurrect-processes '"~claude->${CLAUDE_TMUX_ROOT}/scripts/claude-restore.sh"'
#
# Bug history:
#   2026-04-24 — All panes loaded same session after OOM-crash restore.
#     Root causes (2026-04-24 session-restore incident):
#       Fix 4 applied: display-message was reading focused pane (no -t flag),
#                      so during parallel restore many panes read the WRONG
#                      state file. Now pinned to "$TMUX_PANE".
#       Fix 1 applied: fallback was `claude --continue`, which collapses every
#                      pane sharing cwd onto the most-recent session JSONL.
#                      Now only uses --continue when cwd is project-scoped
#                      (any common project marker), and otherwise starts a
#                      fresh session — safer than wrong session.
#     Code review hardening (same day):
#       - $CLAUDE_CMD converted to a bash array (avoid SC2086 word-splitting).
#       - Project marker broadened from `.claude/` only to common markers
#         (.git, pyproject.toml, package.json, .claude/) — `.claude/` alone
#         was too narrow; most project dirs do not have it.
#
#   2026-04-27 — Resurrect-snapshot fallback (Fix 2).
#     If the position-keyed state file is missing or invalid, look up the
#     calling pane in the most recent tmux-resurrect snapshot and extract
#     `--resume <SESSION_ID>` from the saved command, if present. This
#     uses tmux-resurrect's own data as a redundant truth source and
#     handles the case where state files are pruned (>7 days) but the
#     session is still alive.

set -u

STATE_DIR="${HOME}/.local/share/tmux/claude-sessions"

# JSONL lookup helper — the session-continuity work. Resolves
# SESSION_ID -> JSONL path across ALL per-cwd project directories. Replaces
# the hardcoded per-user projects-dir check that silently
# false-negatived non-$HOME panes (e.g., the 2026-04-29 window-2 fall-through).
_LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../lib" && pwd)"
# shellcheck source=../lib/jsonl.sh
[ -f "$_LIB_DIR/jsonl.sh" ] && source "$_LIB_DIR/jsonl.sh"

# Use an array so args with spaces are safe and shellcheck-clean.
CLAUDE_CMD=(claude --dangerously-skip-permissions --agent claude-personas:matt)

# Identify the calling pane explicitly. tmux-resurrect runs this script INSIDE
# the pane being restored, so $TMUX_PANE is set in env. If absent, fall back
# to whatever the client thinks is current — there is no safer default at
# that point, but it's flagged as the 2026-04-24 failure mode.
PANE_ID="${TMUX_PANE:-}"
if [ -z "$PANE_ID" ]; then
    PANE_ID=$(tmux display-message -p '#{pane_id}' 2>/dev/null)
fi

# All geometry queries pinned to PANE_ID. Without -t, display-message returns
# the *focused* pane's coordinates, which is global-mutable state during a
# multi-pane resurrect run.
SESSION_NAME=$(tmux display-message -t "$PANE_ID" -p '#{session_name}' 2>/dev/null)
WINDOW_INDEX=$(tmux display-message -t "$PANE_ID" -p '#{window_index}' 2>/dev/null)
PANE_INDEX=$(tmux display-message -t "$PANE_ID" -p '#{pane_index}' 2>/dev/null)

KEY="${SESSION_NAME}:${WINDOW_INDEX}.${PANE_INDEX}"
STATE_FILE="${STATE_DIR}/${KEY}.state"

if [ -f "$STATE_FILE" ]; then
    # Parse with grep/cut (safe — no eval/source)
    SESSION_ID=$(grep '^SESSION_ID=' "$STATE_FILE" | cut -d= -f2)

    # Liveness check: search all per-cwd project dirs, not just the home one.
    # Phase 0 fix — the hardcoded path was the root cause of the 2026-04-29
    # silent fall-through.
    if [ -n "$SESSION_ID" ] && session_jsonl_exists "$SESSION_ID"; then
        log_restore_step 1 "resumed" "session_id=${SESSION_ID:0:8}"
        exec "${CLAUDE_CMD[@]}" --resume "$SESSION_ID"
    fi
fi

# Fix 2 — Resurrect snapshot fallback. Walk the latest tmux-resurrect file
# for the line matching THIS pane's (session, window, pane), then extract
# `--resume <SESSION_ID>` from the saved command. Tmux-resurrect saves the
# launch command verbatim, so a pane that was started via `claude --resume X`
# will have X recorded there even if the state file is gone.
#
# Resurrect file format: `pane\t<session>\t<window>\t<active>\t<flags>\t<pane>\t<title>\t<dir>\t<active2>\t<cmd>\t:<full_cmd>`
# We match on session/window/pane and parse the last field for `--resume`.
RESURRECT_DIR="${HOME}/.local/share/tmux/resurrect"
RESURRECT_FILE=""
if [ -L "$RESURRECT_DIR/last" ]; then
    RESURRECT_FILE=$(readlink -f "$RESURRECT_DIR/last")
elif [ -d "$RESURRECT_DIR" ]; then
    RESURRECT_FILE=$(ls -t "$RESURRECT_DIR"/tmux_resurrect_*.txt 2>/dev/null | head -1)
fi

if [ -n "$RESURRECT_FILE" ] && [ -f "$RESURRECT_FILE" ] \
   && [ -n "$SESSION_NAME" ] && [ -n "$WINDOW_INDEX" ] && [ -n "$PANE_INDEX" ]; then
    # awk extracts the saved command for the matching pane line.
    # Field layout: $1=pane, $2=session, $3=window, $5=flags, $6=pane_idx, ...
    # The "--resume <ID>" pattern lives anywhere in the line; use match() on
    # the whole line to grab it portably.
    # UUIDv4 pattern: 8-4-4-4-12 hex digits = 36 chars total. Tightening
    # from `[a-f0-9-]+` to a fixed-length match prevents a malformed cmd
    # from grabbing a longer run of hex+dash chars that happens to follow.
    SAVED_RESUME=$(awk -F'\t' -v s="$SESSION_NAME" -v w="$WINDOW_INDEX" -v p="$PANE_INDEX" '
        $1 == "pane" && $2 == s && $3 == w && $6 == p {
            if (match($0, /--resume[ \t]+[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}/)) {
                cmd = substr($0, RSTART, RLENGTH)
                sub(/^--resume[ \t]+/, "", cmd)
                print cmd
                exit
            }
        }
    ' "$RESURRECT_FILE" 2>/dev/null)

    if [ -n "$SAVED_RESUME" ]; then
        # Phase 0 fix — same cross-project lookup as step 1.
        if session_jsonl_exists "$SAVED_RESUME"; then
            log_restore_step 2 "resumed" "session_id=${SAVED_RESUME:0:8} via_snapshot"
            exec "${CLAUDE_CMD[@]}" --resume "$SAVED_RESUME"
        fi
    fi
fi

# Fallback: only use --continue when cwd is project-scoped (not $HOME).
# In $HOME, --continue collapses every pane sharing cwd onto the same most-
# recent session JSONL — this is what produced the 2026-04-24 "all panes
# same conversation" symptom.
#
# Project marker = any of: .git/, pyproject.toml, package.json, .claude/
# (broad enough to cover python, node, rust, generic git repos, plus
# .claude/-marked plugin dirs). Matches the typical "this is
# a real project, not just a shell prompt in $HOME" intent.
is_project_dir() {
    [ "$PWD" = "$HOME" ] && return 1
    [ -d ".git" ] && return 0
    [ -f "pyproject.toml" ] && return 0
    [ -f "package.json" ] && return 0
    [ -d ".claude" ] && return 0
    [ -f "Cargo.toml" ] && return 0
    [ -f "go.mod" ] && return 0
    return 1
}

if is_project_dir; then
    # Phase 0 telemetry — note the attempt before exec replaces this process.
    # If --continue exec succeeds we never reach line below; the brief
    # investigator treats step 3 as "continue_attempt" and infers success
    # by absence of a follow-up step-4 entry for the same pane key.
    log_restore_step 3 "continue_attempt" "cwd=${PWD}"
    exec "${CLAUDE_CMD[@]}" --continue 2>/dev/null || {
        log_restore_step 3 "continue_failed_to_fresh" "cwd=${PWD}"
        exec "${CLAUDE_CMD[@]}"
    }
fi

# Otherwise: start a fresh session. Better an empty conversation than the
# wrong conversation. This is the silent-fall-through case the morning brief
# now surfaces (Phase 0 deliverable 0.2).
log_restore_step 4 "fresh" "cwd=${PWD} reason=no_resume_target"
exec "${CLAUDE_CMD[@]}"
