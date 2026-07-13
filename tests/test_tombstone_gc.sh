#!/usr/bin/env bash
# test_tombstone_gc.sh — tests.
#
# Verifies claude-tombstone-gc.sh classifies state files correctly and
# is concurrency-safe / disable-able / idempotent.
#
# Run from anywhere:
#   bash tests/test_tombstone_gc.sh

set -uo pipefail

PLUGIN_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SCRIPT="$PLUGIN_DIR/scripts/claude-tombstone-gc.sh"

[ -x "$SCRIPT" ] || { echo "FAIL: $SCRIPT not executable"; exit 1; }

pass=0; fail=0
sandbox_root=$(mktemp -d -t claude-tombstone-test.XXXXXX)
trap 'rm -rf "$sandbox_root"' EXIT

# Helpers ---------------------------------------------------------------------

assert_file_exists() {
    local label="$1" path="$2"
    if [ -f "$path" ]; then
        echo "  ✓ $label"
        pass=$((pass + 1))
    else
        echo "  ✗ $label"
        echo "    expected file: $path"
        fail=$((fail + 1))
    fi
}

assert_file_absent() {
    local label="$1" path="$2"
    if [ ! -e "$path" ]; then
        echo "  ✓ $label"
        pass=$((pass + 1))
    else
        echo "  ✗ $label"
        echo "    forbidden file present: $path"
        fail=$((fail + 1))
    fi
}

assert_glob_count() {
    local label="$1" pattern="$2" expected="$3"
    # shellcheck disable=SC2086
    local count
    count=$(ls -1 $pattern 2>/dev/null | wc -l)
    if [ "$count" = "$expected" ]; then
        echo "  ✓ $label (count=$count)"
        pass=$((pass + 1))
    else
        echo "  ✗ $label"
        echo "    expected count: $expected, got: $count, pattern: $pattern"
        fail=$((fail + 1))
    fi
}

assert_log_contains() {
    local label="$1" needle="$2" log="$3"
    if [ -f "$log" ] && grep -qF -- "$needle" "$log"; then
        echo "  ✓ $label"
        pass=$((pass + 1))
    else
        echo "  ✗ $label"
        echo "    expected substring: $needle"
        if [ -f "$log" ]; then
            echo "    actual log:"
            sed 's/^/      /' "$log"
        else
            echo "    log file missing: $log"
        fi
        fail=$((fail + 1))
    fi
}

# Build a per-test sandbox. Mirrors the real layout: a projects ROOT
# containing one or more per-cwd project subdirectories.
make_sandbox() {
    local case_dir="$1"
    mkdir -p "$case_dir/state" \
             "$case_dir/projects/-home-user" \
             "$case_dir/projects/-home-user-some-repo" \
             "$case_dir/projects/-home-user-Workspace-project-v2" \
             "$case_dir/local-tmux"
    echo "$case_dir"
}

write_state() {
    local state_dir="$1" key="$2" session_id="$3"
    cat > "$state_dir/${key}.state" <<EOF
SESSION_ID=${session_id}
TMUX_SESSION=legion
WINDOW=1
PANE=0
TIMESTAMP=$(date +%s)
EOF
}

run_gc() {
    local case_dir="$1"
    shift
    STATE_DIR="$case_dir/state" \
    PROJECTS_ROOT="$case_dir/projects" \
    TOMBSTONE_DIR="$case_dir/state/.tombstones" \
    TOMBSTONE_META="$case_dir/state/.tombstones/tombstone-meta.jsonl" \
    LOCK_FILE="$case_dir/state/.lock" \
    RESTORE_LOG="$case_dir/local-tmux/restore.log" \
    "$@" \
    "$SCRIPT"
}

# -----------------------------------------------------------------------------
# Case 1: Valid SESSION_ID + JSONL exists -> kept
# -----------------------------------------------------------------------------
echo "Case 1: valid state file + alive JSONL in -home-user/ -> kept"
case_dir=$(make_sandbox "$sandbox_root/case1")
write_state "$case_dir/state" "legion:1.0" "11111111-aaaa-bbbb-cccc-000000000001"
touch "$case_dir/projects/-home-user/11111111-aaaa-bbbb-cccc-000000000001.jsonl"
run_gc "$case_dir"
assert_file_exists "state file kept (not tombstoned)" "$case_dir/state/legion:1.0.state"
assert_glob_count "no tombstones created" "$case_dir/state/.tombstones/legion:1.0.state.*" 0
assert_log_contains "telemetry: kept=1, moved=0" '"kept":1,"moved":0' "$case_dir/local-tmux/restore.log"

# -----------------------------------------------------------------------------
# Case 1b: JSONL lives in a NON-$HOME per-cwd project dir -> kept (cross-project lookup)
# This is the regression test for the 2026-04-29 root cause.
# -----------------------------------------------------------------------------
echo "Case 1b: alive JSONL in -home-user-some-repo/ -> kept (cross-project)"
case_dir=$(make_sandbox "$sandbox_root/case1b")
write_state "$case_dir/state" "legion:2.1" "fbac1b04-e7ad-4e43-8a34-5fc3037fa684"
touch "$case_dir/projects/-home-user-some-repo/fbac1b04-e7ad-4e43-8a34-5fc3037fa684.jsonl"
run_gc "$case_dir"
assert_file_exists "non-\$HOME pane's state file kept" "$case_dir/state/legion:2.1.state"
assert_glob_count "no tombstones created for cross-project alive JSONL" "$case_dir/state/.tombstones/legion:2.1.state.*" 0
assert_log_contains "telemetry: kept=1" '"kept":1,"moved":0' "$case_dir/local-tmux/restore.log"

# -----------------------------------------------------------------------------
# Case 2: Valid SESSION_ID + JSONL missing -> tombstoned (jsonl_missing)
# -----------------------------------------------------------------------------
echo "Case 2: valid SESSION_ID, JSONL deleted -> tombstoned with reason jsonl_missing"
case_dir=$(make_sandbox "$sandbox_root/case2")
write_state "$case_dir/state" "legion:2.1" "22222222-aaaa-bbbb-cccc-000000000002"
# JSONL deliberately absent
run_gc "$case_dir"
assert_file_absent "state file moved out of state dir" "$case_dir/state/legion:2.1.state"
assert_glob_count "exactly one tombstone created" "$case_dir/state/.tombstones/legion:2.1.state.*" 1
assert_log_contains "tombstone-meta records jsonl_missing reason" '"reason":"jsonl_missing"' "$case_dir/state/.tombstones/tombstone-meta.jsonl"
assert_log_contains "telemetry: jsonl_missing=1" '"jsonl_missing":1' "$case_dir/local-tmux/restore.log"

# -----------------------------------------------------------------------------
# Case 3: Empty SESSION_ID (pre-Fix-6 corruption) -> tombstoned (empty_session_id)
# -----------------------------------------------------------------------------
echo "Case 3: empty SESSION_ID -> tombstoned with reason empty_session_id"
case_dir=$(make_sandbox "$sandbox_root/case3")
cat > "$case_dir/state/legion:3.2.state" <<'EOF'
SESSION_ID=
TMUX_SESSION=legion
WINDOW=3
PANE=2
TIMESTAMP=1700000000
EOF
run_gc "$case_dir"
assert_file_absent "empty-id state file moved out" "$case_dir/state/legion:3.2.state"
assert_glob_count "exactly one tombstone created" "$case_dir/state/.tombstones/legion:3.2.state.*" 1
assert_log_contains "tombstone-meta records empty_session_id reason" '"reason":"empty_session_id"' "$case_dir/state/.tombstones/tombstone-meta.jsonl"
assert_log_contains "telemetry: empty_session_id=1" '"empty_session_id":1' "$case_dir/local-tmux/restore.log"

# -----------------------------------------------------------------------------
# Case 4: LEGION_TOMBSTONE_GC=0 -> no-op exit
# -----------------------------------------------------------------------------
echo "Case 4: LEGION_TOMBSTONE_GC=0 -> no scan, no moves"
case_dir=$(make_sandbox "$sandbox_root/case4")
write_state "$case_dir/state" "legion:4.0" "44444444-aaaa-bbbb-cccc-000000000004"
# JSONL absent -> would normally be tombstoned
LEGION_TOMBSTONE_GC=0 run_gc "$case_dir"
assert_file_exists "state file untouched when GC disabled" "$case_dir/state/legion:4.0.state"
assert_glob_count "no tombstones created when disabled" "$case_dir/state/.tombstones/legion:4.0.state.*" 0
# Also no telemetry log line written (script exits before any write)
if [ -f "$case_dir/local-tmux/restore.log" ]; then
    echo "  ✗ telemetry log should not exist when GC disabled"
    sed 's/^/      /' "$case_dir/local-tmux/restore.log"
    fail=$((fail + 1))
else
    echo "  ✓ no telemetry log written when GC disabled"
    pass=$((pass + 1))
fi

# -----------------------------------------------------------------------------
# Case 5: Concurrent invocation (flock held) -> second exits cleanly
# -----------------------------------------------------------------------------
echo "Case 5: concurrent invocation -> second exits without scanning"
case_dir=$(make_sandbox "$sandbox_root/case5")
write_state "$case_dir/state" "legion:5.0" "55555555-aaaa-bbbb-cccc-000000000005"
# JSONL absent -> would be tombstoned by a successful run
# Hold the lock externally, then run GC. flock -n should fail and the script
# must exit cleanly (rc=0) without classifying anything.
(
    exec 9>"$case_dir/state/.lock"
    flock -x 9
    # Now the lock is held. Invoke GC; it should bail.
    run_gc "$case_dir"
    rc=$?
    if [ "$rc" -eq 0 ]; then
        echo "  ✓ second invocation exited 0 while lock held"
        pass=$((pass + 1))
    else
        echo "  ✗ second invocation exited rc=$rc, expected 0"
        fail=$((fail + 1))
    fi
)
# State file must still exist — no scan happened
assert_file_exists "state file untouched while lock held by other process" "$case_dir/state/legion:5.0.state"
assert_glob_count "no tombstones created while lock held" "$case_dir/state/.tombstones/legion:5.0.state.*" 0

# -----------------------------------------------------------------------------
# Summary
# -----------------------------------------------------------------------------
echo
echo "Tombstone GC tests: $pass passed, $fail failed"
[ "$fail" -eq 0 ] && exit 0 || exit 1
