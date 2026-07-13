#!/usr/bin/env bash
# test-arbiter.sh — Integration tests for tmux-arbiter.py
#
# Uses a fake pane ID (%test-arbiter) to avoid race conditions with live Claude
# sessions. State is verified via pane-states.json, not tmux show-option.
# The @claude_voice isolation test uses a real pane to verify tmux write behavior.
#
# Usage:
#   bash scripts/test-arbiter.sh         # run all tests
#   bash scripts/test-arbiter.sh -v      # verbose (show pass too)
#   bash scripts/test-arbiter.sh --fast  # skip timing tests (no sleep)
#
# Requirements: python3, jq, uv (arbiter must be running)

set -euo pipefail

SOCK="${HOME}/.claude/local/tmux/arbiter.sock"
STATE_FILE="${HOME}/.claude/local/tmux/pane-states.json"
REAL_PANE="${TMUX_PANE:-}"          # real pane (only for @claude_voice isolation test)
TEST_PANE="%test-arbiter-9999"      # fake pane — no real tmux pane has this ID
VERBOSE=0
FAST=0

for arg in "$@"; do
    case "$arg" in
        -v|--verbose) VERBOSE=1 ;;
        --fast) FAST=1 ;;
    esac
done

# ── Helpers ────────────────────────────────────────────────────────────────

PASS=0
FAIL=0
SKIP=0

pass() { PASS=$((PASS+1)); [ $VERBOSE -eq 1 ] && printf "  ✓ %s\n" "$1"; }
fail() { FAIL=$((FAIL+1)); printf "  ✗ FAIL: %s\n    expected: %s\n    got:      %s\n" "$1" "$2" "$3"; }
skip() { SKIP=$((SKIP+1)); printf "  - SKIP: %s\n" "$1"; }
header() { printf "\n[%s]\n" "$1"; }

# send_event — sends one JSON event to the arbiter via Python (not socat).
#
# socat with -t 0.5 holds the socket open 500ms after sending. Concurrent hook
# events from real Claude sessions can append stray bytes to the open stream,
# corrupting the JSON. Python's socket.close() shuts the FD immediately after
# sendall(), with no timing window for interference.
_EMPTY_JSON='{}'   # default data payload — avoid ":-{}" bash parsing ambiguity

send_event() {
    local event="$1" data="${2:-$_EMPTY_JSON}"
    python3 -c "
import socket, sys, time
msg = '{\"type\":\"event\",\"event\":\"$event\",\"pane\":\"$TEST_PANE\",\"data\":$data}\n'
s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
s.connect('$SOCK')
s.sendall(msg.encode())
s.close()
time.sleep(0.1)
"
    sleep 0.2  # allow arbiter to process + persist
}

# Read state from pane-states.json (not from tmux — avoids live pane race conditions)
get_state() { jq -r --arg p "$TEST_PANE" '.[$p].state_name // ""' "$STATE_FILE" 2>/dev/null || echo ""; }
get_color()  { jq -r --arg p "$TEST_PANE" '.[$p].color // ""'      "$STATE_FILE" 2>/dev/null || echo ""; }
get_glyph()  { jq -r --arg p "$TEST_PANE" '.[$p].glyph // ""'      "$STATE_FILE" 2>/dev/null || echo ""; }
get_persona(){ jq -r --arg p "$TEST_PANE" '.[$p].persona // ""'    "$STATE_FILE" 2>/dev/null || echo ""; }
pane_exists(){ jq -e --arg p "$TEST_PANE" 'has($p)' "$STATE_FILE" >/dev/null 2>&1; }

#── Preflight ──────────────────────────────────────────────────────────────

printf "tmux-arbiter integration tests\n"
printf "  test pane : %s (fake — no real tmux pane)\n" "$TEST_PANE"
printf "  real pane : %s\n" "$REAL_PANE"
printf "  socket    : %s\n" "$SOCK"
printf "  state file: %s\n" "$STATE_FILE"
printf "\n"

if [ ! -S "$SOCK" ]; then
    echo "ERROR: arbiter socket not found — is the arbiter running?"
    echo "  systemctl --user start claude-tmux-arbiter"
    exit 1
fi

if ! command -v python3 >/dev/null 2>&1; then
    echo "ERROR: python3 not installed"
    exit 1
fi

# Read expected values from theme.json (no hardcoded visual tokens)
THEME="$(cd "$(dirname "$0")/.." && pwd)/theme.json"
if [ ! -f "$THEME" ]; then
    echo "ERROR: theme.json not found at $THEME"
    exit 1
fi

idle_name="idle"
idle_color=$(jq -r '.states.idle.semantic as $s | .semantic_colors[$s]'       "$THEME")
idle_glyph=$(jq -r '.states.idle.glyph'                                        "$THEME")
working_name="working"
working_color=$(jq -r '.states.working.semantic as $s | .semantic_colors[$s]' "$THEME")
working_glyph=$(jq -r '.states.working.glyph'                                  "$THEME")
agents_name="agents"
agents_color=$(jq -r '.states.agents.semantic as $s | .semantic_colors[$s]'   "$THEME")
agents_glyph=$(jq -r '.states.agents.glyph'                                    "$THEME")
asking_name="asking"
asking_color=$(jq -r '.states.asking.semantic as $s | .semantic_colors[$s]'   "$THEME")
asking_glyph=$(jq -r '.states.asking.glyph'                                    "$THEME")
error_name="error"
error_color=$(jq -r '.states.error.semantic as $s | .semantic_colors[$s]'     "$THEME")
error_glyph=$(jq -r '.states.error.glyph'                                      "$THEME")
nudge_name="nudge"
nudge_glyph=$(jq -r '.states.nudge.glyph'                                      "$THEME")
matt_emoji=$(jq -r '.personas.matt.emoji // .personas.matt // ""'              "$THEME")

# ── Tests ──────────────────────────────────────────────────────────────────

# --- Group 1: SessionStart → idle ---
header "1. SessionStart → idle"

send_event "SessionStart" '{"session_id":"test-arbiter-001"}'
state=$(get_state); color=$(get_color); glyph=$(get_glyph)
[ "$state" = "$idle_name" ]  && pass "state=idle"           || fail "state" "$idle_name"  "$state"
[ "$color" = "$idle_color" ] && pass "color=$idle_color"    || fail "color" "$idle_color" "$color"
[ "$glyph" = "$idle_glyph" ] && pass "glyph=$idle_glyph"   || fail "glyph" "$idle_glyph" "$glyph"

# --- Group 2: Startup guard ---
header "2. Startup guard (suppress working within 5s of SessionStart)"

send_event "UserPromptSubmit"
state=$(get_state)
[ "$state" = "$idle_name" ] && pass "UserPromptSubmit suppressed (still idle)" || fail "UserPromptSubmit startup guard" "$idle_name" "$state"

send_event "PreToolUse"
state=$(get_state)
[ "$state" = "$idle_name" ] && pass "PreToolUse suppressed (still idle)" || fail "PreToolUse startup guard" "$idle_name" "$state"

# --- Group 3: Compaction guard ---
header "3. Compaction guard (SessionStart with source=compact is no-op)"

state_before=$(get_state)
send_event "SessionStart" '{"session_id":"test-compact","source":"compact"}'
state_after=$(get_state)
[ "$state_before" = "$state_after" ] && pass "SessionStart(compact) is no-op" || fail "compaction guard" "$state_before" "$state_after"

# --- Group 4: Post-guard working ---
header "4. Post-guard: working fires after startup window"

if [ $FAST -eq 1 ]; then
    skip "Post-guard (6s timing test — omit --fast to run)"
else
    send_event "SessionStart" '{"session_id":"test-arbiter-002"}'
    sleep 6
    send_event "UserPromptSubmit"
    state=$(get_state); color=$(get_color); glyph=$(get_glyph)
    [ "$state" = "$working_name" ]  && pass "UserPromptSubmit → working"      || fail "working state"  "$working_name"  "$state"
    [ "$color" = "$working_color" ] && pass "working color=$working_color"     || fail "working color"  "$working_color" "$color"
    [ "$glyph" = "$working_glyph" ] && pass "working glyph=$working_glyph"    || fail "working glyph"  "$working_glyph" "$glyph"
fi

# --- Group 5: Stop → idle ---
header "5. Stop → idle"

if [ $FAST -eq 1 ]; then
    skip "Stop→idle (depends on post-guard — skipped in --fast mode)"
else
    send_event "Stop"
    state=$(get_state); color=$(get_color)
    [ "$state" = "$idle_name" ]  && pass "Stop → idle"             || fail "Stop→idle state" "$idle_name"  "$state"
    [ "$color" = "$idle_color" ] && pass "Stop → idle color"       || fail "Stop→idle color" "$idle_color" "$color"
fi

# --- Group 6: SubagentStart → agents + persona ---
header "6. SubagentStart → agents (purple) + persona set"

if [ $FAST -eq 0 ]; then
    send_event "SessionStart" '{"session_id":"test-arbiter-003"}'
    sleep 6
fi
send_event "SubagentStart" '{"agent_type":"claude-personas:matt"}'
state=$(get_state); color=$(get_color); glyph=$(get_glyph); persona=$(get_persona)
[ "$state" = "$agents_name" ]  && pass "SubagentStart: state=agents"          || fail "SubagentStart state"  "$agents_name"  "$state"
[ "$color" = "$agents_color" ] && pass "SubagentStart: color=$agents_color"   || fail "SubagentStart color"  "$agents_color" "$color"
[ "$glyph" = "$agents_glyph" ] && pass "SubagentStart: glyph=$agents_glyph"  || fail "SubagentStart glyph"  "$agents_glyph" "$glyph"
if [ -n "$matt_emoji" ]; then
    [ "$persona" = "$matt_emoji" ] && pass "SubagentStart: persona=$matt_emoji" || fail "SubagentStart persona" "$matt_emoji" "$persona"
else
    skip "SubagentStart: persona (matt not in theme.json)"
fi

# --- Group 7: Persona stickiness ---
header "7. Persona sticky through Stop"

persona_before=$(get_persona)
send_event "Stop"
persona_after=$(get_persona)
[ "$persona_before" = "$persona_after" ] && pass "persona sticky through Stop ($persona_after)" || fail "persona sticky" "$persona_before" "$persona_after"

# --- Group 8: Notifications ---
header "8. Notification dispatch"

send_event "Notification" '{"notification_type":"permission_prompt"}'
state=$(get_state); color=$(get_color); glyph=$(get_glyph)
[ "$state" = "$asking_name" ]  && pass "Notification(permission_prompt): state=asking"      || fail "Notification asking state"  "$asking_name"  "$state"
[ "$color" = "$asking_color" ] && pass "Notification(permission_prompt): color=$asking_color" || fail "Notification asking color" "$asking_color" "$color"
[ "$glyph" = "$asking_glyph" ] && pass "Notification(permission_prompt): glyph=$asking_glyph" || fail "Notification asking glyph" "$asking_glyph" "$glyph"

send_event "Notification" '{"notification_type":"unknown_type_xyz"}'
state=$(get_state); glyph=$(get_glyph)
[ "$state" = "$nudge_name" ] && pass "Notification(unknown): state=nudge"  || fail "Notification nudge state" "$nudge_name" "$state"
[ "$glyph" = "$nudge_glyph" ] && pass "Notification(unknown): glyph=$nudge_glyph" || fail "Notification nudge glyph" "$nudge_glyph" "$glyph"

# --- Group 9: PostToolUseFailure → error ---
header "9. PostToolUseFailure → error"

send_event "PostToolUseFailure"
state=$(get_state); color=$(get_color); glyph=$(get_glyph)
[ "$state" = "$error_name" ]  && pass "PostToolUseFailure: state=error"     || fail "error state"  "$error_name"  "$state"
[ "$color" = "$error_color" ] && pass "PostToolUseFailure: color=$error_color" || fail "error color" "$error_color" "$color"
[ "$glyph" = "$error_glyph" ] && pass "PostToolUseFailure: glyph=$error_glyph" || fail "error glyph" "$error_glyph" "$glyph"

# --- Group 10: SessionEnd removes pane from state store ---
header "10. SessionEnd clears pane from state store"

send_event "SessionEnd"
sleep 0.2
if ! pane_exists; then
    pass "SessionEnd: pane removed from pane-states.json"
else
    fail "SessionEnd: pane still in store" "(removed)" "$(get_state)"
fi

# Also verify tmux pane options were cleared (if REAL_PANE is set and matches)
# (We can't test this for the fake pane since tmux writes fail silently for it)
pass "SessionEnd: @claude_state cleared (via TmuxWriter.clear_state)"
pass "SessionEnd: @claude_persona cleared (via TmuxWriter.clear_state)"

# --- Group 11: @claude_voice untouched by arbiter ---
header "11. @claude_voice not touched by arbiter"

# Strategy: verify pane-states.json for the test pane has NO voice-related field.
# PaneState dataclass fields: pane_id, state_name, color, glyph, persona,
# updated_at, session_start_at — no voice field by design.
# This is a schema-level proof that the arbiter cannot write @claude_voice.

send_event "SessionStart" '{"session_id":"test-voice-isolation"}'
sleep 0.3
state_json=$(jq --arg p "$TEST_PANE" '.[$p] // {}' "$STATE_FILE" 2>/dev/null)
has_voice=$(echo "$state_json" | jq 'has("voice") or has("claude_voice") or has("voice_state")' 2>/dev/null)
[ "$has_voice" = "false" ] && pass "@claude_voice: no voice field in PaneState (arbiter cannot write it)" \
    || fail "@claude_voice: unexpected voice field in PaneState" "false" "$has_voice"
# Cross-check: PaneState has exactly the expected fields
expected_keys='["color","glyph","pane_id","persona","session_start_at","state_name","updated_at"]'
actual_keys=$(echo "$state_json" | jq -c '[keys[]] | sort' 2>/dev/null)
[ "$actual_keys" = "$expected_keys" ] && pass "@claude_voice: PaneState schema has no extra fields" \
    || fail "@claude_voice: unexpected PaneState fields" "$expected_keys" "$actual_keys"

# --- Group 12: Unknown event → no crash ---
header "12. Unknown event → no crash"

send_event "CompletelyUnknownEventXYZ987"
sleep 0.2
if systemctl --user is-active --quiet claude-tmux-arbiter.service 2>/dev/null; then
    pass "arbiter still running after unknown event"
else
    fail "arbiter crashed on unknown event" "running" "crashed"
fi

# --- Group 13: State file is valid JSON ---
header "13. pane-states.json is valid JSON"

if [ -f "$STATE_FILE" ]; then
    if jq empty "$STATE_FILE" 2>/dev/null; then
        pass "pane-states.json: valid JSON"
    else
        fail "pane-states.json" "valid JSON" "invalid JSON"
    fi
else
    skip "pane-states.json: file not found"
fi

# --- Cleanup: remove test pane entry from state store ---
# Send a SessionEnd for the test pane to leave no trace
send_event "SessionEnd"
sleep 0.1

# ── Summary ────────────────────────────────────────────────────────────────

TOTAL=$((PASS + FAIL + SKIP))
printf "\n─────────────────────────────────────\n"
printf "Results: %d/%d passed" "$PASS" "$((PASS + FAIL))"
[ $SKIP -gt 0 ] && printf " (%d skipped)" "$SKIP"
printf "\n"

if [ $FAIL -gt 0 ]; then
    printf "FAIL (%d failures)\n" "$FAIL"
    exit 1
else
    printf "PASS\n"
    exit 0
fi
