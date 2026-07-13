#!/usr/bin/env bash
# test-stream.sh — Integration tests for tmux-stream Phase 2
#
# Usage: bash scripts/test-stream.sh [-v]
set -euo pipefail

HANDLER="$(dirname "$0")/stream-handler.py"
CLI="$(dirname "$0")/tmux-stream-cli.py"
STREAM_DIR="$HOME/.claude/local/tmux-stream"
PASS=0
FAIL=0
VERBOSE="${1:-}"

check() {
    local name="$1" expected="$2" actual="$3"
    if echo "$actual" | grep -q "$expected"; then
        PASS=$((PASS + 1))
        [ "$VERBOSE" = "-v" ] && echo "  ✓ $name"
    else
        FAIL=$((FAIL + 1))
        echo "  ✗ $name"
        echo "    expected: $expected"
        echo "    actual:   $actual"
    fi
}

TODAY=$(date -u +%Y-%m-%d)
JSONL="$STREAM_DIR/$TODAY.jsonl"

echo "tmux-stream Phase 2 — Integration Tests"
echo "========================================"
echo ""

# Helper: jq parse over JSONL, tolerating any malformed lines from concurrent
# writers in the live stream by pre-filtering with grep (only candidate lines
# are passed to jq). Returns empty string if no match.
filter_jq() {
    local pane="$1" jq_expr="$2"
    grep -F "\"pane\": \"$pane\"" "$JSONL" 2>/dev/null | \
        jq -c "$jq_expr" 2>/dev/null | tail -1 || true
}

# --- Test 1: Basic OSC segmentation ---
echo "Test 1: OSC marker segmentation"
printf '\033]tmux-stream;cmd;9000000;echo test1\007\ntest output 1\n\033]tmux-stream;end;9000100;0\007\n' | \
  python3 -u "$HANDLER" '%test-p2-1' 2>/dev/null
result=$(filter_jq '%test-p2-1' 'select(.pane=="%test-p2-1") | {command, exit_code}')
check "command captured" '"echo test1"' "$result"
check "exit code 0" '"exit_code":0' "$result"

# --- Test 2: Non-zero exit code ---
echo "Test 2: Non-zero exit code"
printf '\033]tmux-stream;cmd;9100000;bad-cmd\007\n\033]tmux-stream;end;9100050;127\007\n' | \
  python3 -u "$HANDLER" '%test-p2-2' 2>/dev/null
result=$(filter_jq '%test-p2-2' 'select(.pane=="%test-p2-2") | .exit_code')
check "exit code 127" "127" "$result"

# --- Test 3: TUI detection ---
echo "Test 3: TUI detection"
printf '\033[?1049h\033[2;5Hhello from vim\033[?1049l\n' | \
  timeout 4 python3 -u "$HANDLER" '%test-p2-tui' 2>/dev/null
result=$(filter_jq '%test-p2-tui' 'select(.pane=="%test-p2-tui") | .tui')
check "tui=true" "true" "$result"

# --- Test 4: Per-pane state file ---
echo "Test 4: Per-pane state isolation"
[ -f "$STREAM_DIR/state-pane-test-p2-1.json" ] && state="exists" || state="missing"
check "state file exists" "exists" "$state"
seq=$(jq '.seq' "$STREAM_DIR/state-pane-test-p2-1.json" 2>/dev/null)
# seq starts at 0 and increments per segment; just verify it's a positive integer
check "seq counter > 0" "^[1-9][0-9]*$" "$seq"

# --- Test 5: CLI stats ---
echo "Test 5: CLI stats"
result=$(python3 "$CLI" stats 2>&1)
check "stats shows segments" "Segments" "$result"
check "stats shows panes" "Panes" "$result"

# --- Test 6: CLI status ---
echo "Test 6: CLI status"
result=$(python3 "$CLI" status 2>&1)
check "status shows storage" "Storage" "$result"

# --- Test 7: CLI search ---
echo "Test 7: CLI search"
result=$(python3 "$CLI" search "test output 1" 2>&1)
check "search finds content" "matching" "$result"

# --- Summary ---
echo ""
echo "========================================"
echo "Results: $PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ] && echo "All tests passed!" || exit 1
