#!/usr/bin/env bash
# test_claude_restore.sh — Regression tests for claude-restore.sh
#
# Verifies the 2026-04-24 fix:
#   Fix 4 — state-file lookup uses TMUX_PANE-pinned display-message
#   Fix 1 — fallback no longer collapses panes sharing $HOME
#
# Run from anywhere:
#   bash tests/test_claude_restore.sh
#
# Strategy: PATH-shim `tmux` and `claude` so the script's `exec` calls are
# captured to a log file instead of actually invoked. Override $HOME to
# point at a per-test sandbox so STATE_DIR/PROJECTS_DIR resolve there
# without needing to edit the script under test.

set -uo pipefail

PLUGIN_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SCRIPT="$PLUGIN_DIR/scripts/claude-restore.sh"

[ -x "$SCRIPT" ] || { echo "FAIL: $SCRIPT not executable"; exit 1; }

pass=0; fail=0
sandbox_root=$(mktemp -d -t claude-restore-test.XXXXXX)
trap 'rm -rf "$sandbox_root"' EXIT

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

assert_log_excludes() {
    local label="$1" needle="$2" log="$3"
    if [ -f "$log" ] && grep -qF -- "$needle" "$log"; then
        echo "  ✗ $label"
        echo "    forbidden substring present: $needle"
        echo "    actual log:"
        sed 's/^/      /' "$log"
        fail=$((fail + 1))
    else
        echo "  ✓ $label"
        pass=$((pass + 1))
    fi
}

# Build a per-test sandbox: fake $HOME with state + projects dirs, plus
# PATH mocks for tmux and claude.
make_sandbox() {
    local case_dir="$1" tmux_responses="$2"
    mkdir -p "$case_dir/home/.local/share/tmux/claude-sessions"
    mkdir -p "$case_dir/home/.claude/projects/-home-user"
    mkdir -p "$case_dir/mocks"

    # tmux mock: responds to display-message lookups from a key=value file
    cat > "$case_dir/mocks/tmux" <<'EOF'
#!/usr/bin/env bash
get() { grep "^$1=" "$TMUX_RESPONSES_FILE" 2>/dev/null | head -1 | cut -d= -f2-; }
args="$*"
case "$args" in
    *"#{session_name}"*) get session_name; exit 0 ;;
    *"#{window_index}"*) get window_index; exit 0 ;;
    *"#{pane_index}"*)   get pane_index;   exit 0 ;;
    *"#{pane_id}"*)      get pane_id;      exit 0 ;;
esac
echo "tmux mock: unhandled args: $args" >&2
exit 1
EOF
    chmod +x "$case_dir/mocks/tmux"

    # claude mock: log argv to $CLAUDE_RUN_LOG, exit clean
    cat > "$case_dir/mocks/claude" <<'EOF'
#!/usr/bin/env bash
echo "CLAUDE_INVOKED args=$*" >> "$CLAUDE_RUN_LOG"
exit 0
EOF
    chmod +x "$case_dir/mocks/claude"

    cp "$tmux_responses" "$case_dir/tmux_responses.txt"
}

# Run the script-under-test with a sandboxed environment.
# $1 = case_dir, $2 = TMUX_PANE value (empty to unset), $3 = cwd to run from
run_restore() {
    local case_dir="$1" tmux_pane="$2" run_cwd="$3"
    local args=()
    if [ -n "$tmux_pane" ]; then
        args+=(--tmux-pane "$tmux_pane")
    fi

    # Use env to control variables explicitly. Note: HOME override makes the
    # script's STATE_DIR / PROJECTS_DIR resolve into the sandbox.
    if [ -n "$tmux_pane" ]; then
        env -i \
            HOME="$case_dir/home" \
            PATH="$case_dir/mocks:/usr/bin:/bin" \
            TMUX_PANE="$tmux_pane" \
            TMUX_RESPONSES_FILE="$case_dir/tmux_responses.txt" \
            CLAUDE_RUN_LOG="$case_dir/run.log" \
            ${LEGION_RESTORE_TELEMETRY:+LEGION_RESTORE_TELEMETRY="$LEGION_RESTORE_TELEMETRY"} \
            bash -c "cd '$run_cwd' && bash '$SCRIPT'" 2>&1 || true
    else
        env -i \
            HOME="$case_dir/home" \
            PATH="$case_dir/mocks:/usr/bin:/bin" \
            TMUX_RESPONSES_FILE="$case_dir/tmux_responses.txt" \
            CLAUDE_RUN_LOG="$case_dir/run.log" \
            ${LEGION_RESTORE_TELEMETRY:+LEGION_RESTORE_TELEMETRY="$LEGION_RESTORE_TELEMETRY"} \
            bash -c "cd '$run_cwd' && bash '$SCRIPT'" 2>&1 || true
    fi
    touch "$case_dir/run.log"   # ensure exists for assertions
}

# ---- Test 1: state file exists, valid JSONL → --resume <id> --------------

echo "Test 1: state file exists → claude --resume <id>"
case_dir=$sandbox_root/t1
SESSION_ID="abc12345-6789-1111-2222-333333333333"

mkdir -p "$case_dir"
cat > "$case_dir/tmux_responses_src.txt" <<EOF
session_name=legion
window_index=5
pane_index=1
pane_id=%42
EOF
make_sandbox "$case_dir" "$case_dir/tmux_responses_src.txt"

# Pre-create the state file at the EXPECTED key, plus the JSONL it points to
cat > "$case_dir/home/.local/share/tmux/claude-sessions/legion:5.1.state" <<EOF
SESSION_ID=${SESSION_ID}
TMUX_SESSION=legion
WINDOW=5
PANE=1
TIMESTAMP=1234567890
EOF
touch "$case_dir/home/.claude/projects/-home-user/${SESSION_ID}.jsonl"

# cwd = $HOME (sandboxed) so the project-marker fallback would NOT fire
run_restore "$case_dir" "%42" "$case_dir/home"

assert_log_contains "Test 1: claude was invoked"     "CLAUDE_INVOKED" "$case_dir/run.log"
assert_log_contains "Test 1: --resume flag present"  "--resume"       "$case_dir/run.log"
assert_log_contains "Test 1: correct session id"     "$SESSION_ID"    "$case_dir/run.log"
assert_log_excludes "Test 1: --continue NOT used"    "--continue"     "$case_dir/run.log"

# ---- Test 2: no state file, cwd=$HOME → fresh, no --continue (Fix 1) -----

echo
echo "Test 2: no state file, cwd=\$HOME → fresh claude (no --continue)"
case_dir=$sandbox_root/t2
mkdir -p "$case_dir"
cat > "$case_dir/tmux_responses_src.txt" <<EOF
session_name=legion
window_index=99
pane_index=1
pane_id=%99
EOF
make_sandbox "$case_dir" "$case_dir/tmux_responses_src.txt"
# (no .state file written)

run_restore "$case_dir" "%99" "$case_dir/home"

assert_log_contains "Test 2: claude was invoked"     "CLAUDE_INVOKED" "$case_dir/run.log"
assert_log_excludes "Test 2: --continue NOT used"    "--continue"     "$case_dir/run.log"
assert_log_excludes "Test 2: --resume NOT used"      "--resume"       "$case_dir/run.log"

# ---- Test 3: no state file, project dir with .git → --continue ok --------

echo
echo "Test 3: no state file, cwd has .git/ → claude --continue"
case_dir=$sandbox_root/t3
mkdir -p "$case_dir"
cat > "$case_dir/tmux_responses_src.txt" <<EOF
session_name=legion
window_index=99
pane_index=2
pane_id=%100
EOF
make_sandbox "$case_dir" "$case_dir/tmux_responses_src.txt"

# Project dir lives OUTSIDE $HOME, has .git/ marker
project_dir="$case_dir/work/repo"
mkdir -p "$project_dir/.git"

run_restore "$case_dir" "%100" "$project_dir"

assert_log_contains "Test 3: claude was invoked"     "CLAUDE_INVOKED" "$case_dir/run.log"
assert_log_contains "Test 3: --continue used"        "--continue"     "$case_dir/run.log"

# ---- Test 4: TMUX_PANE unset → falls back to display-message + works -----

echo
echo "Test 4: TMUX_PANE unset → still resolves pane via display-message"
case_dir=$sandbox_root/t4
SESSION_ID2="def67890-aaaa-bbbb-cccc-dddddddddddd"
mkdir -p "$case_dir"
cat > "$case_dir/tmux_responses_src.txt" <<EOF
session_name=legion
window_index=7
pane_index=3
pane_id=%17
EOF
make_sandbox "$case_dir" "$case_dir/tmux_responses_src.txt"
cat > "$case_dir/home/.local/share/tmux/claude-sessions/legion:7.3.state" <<EOF
SESSION_ID=${SESSION_ID2}
TMUX_SESSION=legion
WINDOW=7
PANE=3
TIMESTAMP=1234567890
EOF
touch "$case_dir/home/.claude/projects/-home-user/${SESSION_ID2}.jsonl"

# TMUX_PANE empty → triggers inner fallback to display-message
run_restore "$case_dir" "" "$case_dir/home"

assert_log_contains "Test 4: claude was invoked"     "CLAUDE_INVOKED" "$case_dir/run.log"
assert_log_contains "Test 4: --resume flag present"  "--resume"       "$case_dir/run.log"
assert_log_contains "Test 4: correct session id"     "$SESSION_ID2"   "$case_dir/run.log"

# ---- Test 5: pyproject.toml is also a project marker --------------------

echo
echo "Test 5: cwd has pyproject.toml → --continue ok"
case_dir=$sandbox_root/t5
mkdir -p "$case_dir"
cat > "$case_dir/tmux_responses_src.txt" <<EOF
session_name=legion
window_index=99
pane_index=5
pane_id=%101
EOF
make_sandbox "$case_dir" "$case_dir/tmux_responses_src.txt"
project_dir="$case_dir/work/pyproj"
mkdir -p "$project_dir"
echo '[project]' > "$project_dir/pyproject.toml"

run_restore "$case_dir" "%101" "$project_dir"

assert_log_contains "Test 5: --continue used"        "--continue"     "$case_dir/run.log"

# ---- Test 6: state file exists but JSONL gone → fall through to fallback -

echo
echo "Test 6: state file points at missing JSONL → fall through (no --resume of dead id)"
case_dir=$sandbox_root/t6
DEAD_SESSION_ID="00000000-0000-0000-0000-000000000000"
mkdir -p "$case_dir"
cat > "$case_dir/tmux_responses_src.txt" <<EOF
session_name=legion
window_index=8
pane_index=1
pane_id=%50
EOF
make_sandbox "$case_dir" "$case_dir/tmux_responses_src.txt"
cat > "$case_dir/home/.local/share/tmux/claude-sessions/legion:8.1.state" <<EOF
SESSION_ID=${DEAD_SESSION_ID}
TMUX_SESSION=legion
WINDOW=8
PANE=1
TIMESTAMP=1234567890
EOF
# JSONL intentionally NOT created — simulates session pruned

run_restore "$case_dir" "%50" "$case_dir/home"

assert_log_contains "Test 6: claude was invoked"     "CLAUDE_INVOKED" "$case_dir/run.log"
assert_log_excludes "Test 6: dead session not resumed" "$DEAD_SESSION_ID" "$case_dir/run.log"

# ---- Test 7: resurrect-snapshot fallback (Fix 2, 2026-04-27) -------------
# State file missing for pane, but the latest resurrect file records that
# this pane was launched with `--resume <id>`. claude-restore should extract
# that id and resume it instead of falling to fresh.

echo
echo "Test 7: state file missing, resurrect file has --resume <id> → reuse it"
case_dir=$sandbox_root/t7
RECOVERED_ID="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
mkdir -p "$case_dir"
cat > "$case_dir/tmux_responses_src.txt" <<EOF
session_name=legion
window_index=12
pane_index=1
pane_id=%70
EOF
make_sandbox "$case_dir" "$case_dir/tmux_responses_src.txt"

# Create the JSONL the recovered id will resume into
touch "$case_dir/home/.claude/projects/-home-user/${RECOVERED_ID}.jsonl"

# Write a fake resurrect file matching the structure a real host produces
mkdir -p "$case_dir/home/.local/share/tmux/resurrect"
cat > "$case_dir/home/.local/share/tmux/resurrect/tmux_resurrect_20260424T000000.txt" <<EOF
window	legion	12	1	0,80x24,0,0,1	1
pane	legion	12	1	:	1	✳ claude-personas:matt	:/home/user	1	claude	:claude --dangerously-skip-permissions --agent claude-personas:matt --resume ${RECOVERED_ID}
EOF
# (tab-delimited; the heredoc preserves the embedded \t through the script
# because we wrote actual tab chars below.)
# Replace the literal "TAB" markers with real tabs — easier than embedding:
python3 -c "
import re
p = '$case_dir/home/.local/share/tmux/resurrect/tmux_resurrect_20260424T000000.txt'
content = open(p).read()
# already tab-delimited from heredoc since it preserves whitespace, but be safe:
fixed = re.sub(r' {3,}', '\t', content)  # any run of 3+ spaces -> tab (heuristic)
open(p, 'w').write(content)  # leave as-is
" 2>/dev/null
# Actually easier: just write with explicit tabs:
printf 'pane\tlegion\t12\t1\t:\t1\t* claude-personas:matt\t:/home/user\t1\tclaude\t:claude --dangerously-skip-permissions --agent claude-personas:matt --resume %s\n' \
    "$RECOVERED_ID" > "$case_dir/home/.local/share/tmux/resurrect/tmux_resurrect_20260424T000000.txt"

# No state file at legion:12.1.state → forces resurrect fallback path

run_restore "$case_dir" "%70" "$case_dir/home"

assert_log_contains "Test 7: claude was invoked"        "CLAUDE_INVOKED"  "$case_dir/run.log"
assert_log_contains "Test 7: --resume from resurrect"   "--resume"        "$case_dir/run.log"
assert_log_contains "Test 7: correct recovered id"      "$RECOVERED_ID"   "$case_dir/run.log"
assert_log_excludes "Test 7: --continue NOT used"       "--continue"      "$case_dir/run.log"

# ---- Test 8: resurrect-snapshot match for OTHER pane should NOT match ----
# Defensive: ensure the awk filter requires session+window+pane all match,
# not just substring on the line. This prevents cross-pane id leakage.

echo
echo "Test 8: resurrect file has --resume for DIFFERENT pane → no false match"
case_dir=$sandbox_root/t8
WRONG_ID="ffffffff-ffff-ffff-ffff-ffffffffffff"
mkdir -p "$case_dir"
cat > "$case_dir/tmux_responses_src.txt" <<EOF
session_name=legion
window_index=99
pane_index=99
pane_id=%999
EOF
make_sandbox "$case_dir" "$case_dir/tmux_responses_src.txt"
mkdir -p "$case_dir/home/.local/share/tmux/resurrect"
# A pane line for legion:5.1 (NOT our 99.99) carrying --resume WRONG_ID.
printf 'pane\tlegion\t5\t1\t:\t1\t* claude-personas:matt\t:/home/user\t1\tclaude\t:claude --resume %s\n' \
    "$WRONG_ID" > "$case_dir/home/.local/share/tmux/resurrect/tmux_resurrect_20260424T000000.txt"

run_restore "$case_dir" "%999" "$case_dir/home"

assert_log_excludes "Test 8: WRONG_ID not used"        "$WRONG_ID"      "$case_dir/run.log"

# ---- Test 9: dedupe on save (Fix 3) — verify save_session_state behavior -

echo
echo "Test 9: save_session_state removes duplicate SESSION_ID at other keys"
case_dir=$sandbox_root/t9
DUP_ID="dddddddd-1111-2222-3333-444444444444"
state_dir="$case_dir/home/.local/share/tmux/claude-sessions"
mkdir -p "$state_dir"

# Pre-populate two stale state files at OLD keys, both holding DUP_ID
cat > "$state_dir/legion:1.1.state" <<EOF
SESSION_ID=${DUP_ID}
TMUX_SESSION=legion
WINDOW=1
PANE=1
TIMESTAMP=100
EOF
cat > "$state_dir/legion:2.2.state" <<EOF
SESSION_ID=${DUP_ID}
TMUX_SESSION=legion
WINDOW=2
PANE=2
TIMESTAMP=200
EOF
# And one unrelated state file that should be untouched
OTHER_ID="11111111-2222-3333-4444-555555555555"
cat > "$state_dir/legion:5.5.state" <<EOF
SESSION_ID=${OTHER_ID}
TMUX_SESSION=legion
WINDOW=5
PANE=5
TIMESTAMP=500
EOF

# Build the tmux mock for save_session_state's display calls
cat > "$case_dir/tmux_responses_src.txt" <<EOF
session_name=legion
window_index=7
pane_index=1
pane_id=%80
EOF
make_sandbox "$case_dir" "$case_dir/tmux_responses_src.txt"

# Source common.sh and call save_session_state directly
PLUGIN_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
env -i \
    HOME="$case_dir/home" \
    PATH="$case_dir/mocks:/usr/bin:/bin" \
    TMUX_RESPONSES_FILE="$case_dir/tmux_responses.txt" \
    bash -c "source '$PLUGIN_DIR/lib/common.sh' 2>/dev/null
             save_session_state '%80' '$DUP_ID'" 2>/dev/null || true

# After save: legion:7.1 should exist with DUP_ID; legion:1.1 and legion:2.2
# should be GONE (deduped); legion:5.5 should be UNTOUCHED.
new_file="$state_dir/legion:7.1.state"
old1="$state_dir/legion:1.1.state"
old2="$state_dir/legion:2.2.state"
unrelated="$state_dir/legion:5.5.state"

if [ -f "$new_file" ] && grep -qFx "SESSION_ID=${DUP_ID}" "$new_file"; then
    echo "  ✓ Test 9: new state file at current key written"
    pass=$((pass + 1))
else
    echo "  ✗ Test 9: new state file at legion:7.1.state missing"
    fail=$((fail + 1))
fi

if [ ! -e "$old1" ] && [ ! -e "$old2" ]; then
    echo "  ✓ Test 9: stale duplicate state files removed"
    pass=$((pass + 1))
else
    echo "  ✗ Test 9: stale duplicates remain — $([ -e $old1 ] && echo $old1) $([ -e $old2 ] && echo $old2)"
    fail=$((fail + 1))
fi

if [ -f "$unrelated" ] && grep -qFx "SESSION_ID=${OTHER_ID}" "$unrelated"; then
    echo "  ✓ Test 9: unrelated state file untouched"
    pass=$((pass + 1))
else
    echo "  ✗ Test 9: unrelated state file disturbed"
    fail=$((fail + 1))
fi

# ---- Test 10: empty SESSION_ID guard (Fix 6) — no zero-byte writes -------

echo
echo "Test 10: save_session_state with empty session_id writes no file"
case_dir=$sandbox_root/t10
state_dir="$case_dir/home/.local/share/tmux/claude-sessions"
mkdir -p "$state_dir"

cat > "$case_dir/tmux_responses_src.txt" <<EOF
session_name=legion
window_index=10
pane_index=1
pane_id=%90
EOF
make_sandbox "$case_dir" "$case_dir/tmux_responses_src.txt"

env -i \
    HOME="$case_dir/home" \
    PATH="$case_dir/mocks:/usr/bin:/bin" \
    TMUX_RESPONSES_FILE="$case_dir/tmux_responses.txt" \
    bash -c "source '$PLUGIN_DIR/lib/common.sh' 2>/dev/null
             save_session_state '%90' ''" 2>/dev/null || true

# Nothing should have been written
written_files=$(find "$state_dir" -name '*.state' -type f 2>/dev/null | wc -l)
if [ "$written_files" -eq 0 ]; then
    echo "  ✓ Test 10: no state file written for empty session_id"
    pass=$((pass + 1))
else
    echo "  ✗ Test 10: $written_files state file(s) written for empty session_id"
    ls -la "$state_dir"
    fail=$((fail + 1))
fi

# Also verify NO zero-byte files exist (size guard works even if a state
# file got created by some other path)
zero_files=$(find "$state_dir" -name '*.state' -size 0 2>/dev/null | wc -l)
if [ "$zero_files" -eq 0 ]; then
    echo "  ✓ Test 10: no zero-byte state files exist"
    pass=$((pass + 1))
else
    echo "  ✗ Test 10: $zero_files zero-byte state file(s) found"
    fail=$((fail + 1))
fi

# ---- Test 11: telemetry — step-1 resume writes a JSON Lines record ------
#
# the session-continuity work. Reuses Test 1's setup (state file
# + alive JSONL) and asserts the restore.log line shape.

echo
echo "Test 11: state file exists → restore.log records step:1 outcome:resumed"
case_dir=$sandbox_root/t11
SESSION_ID="11111111-aaaa-bbbb-cccc-111111111111"
mkdir -p "$case_dir"
cat > "$case_dir/tmux_responses_src.txt" <<EOF
session_name=legion
window_index=11
pane_index=0
pane_id=%111
EOF
make_sandbox "$case_dir" "$case_dir/tmux_responses_src.txt"
cat > "$case_dir/home/.local/share/tmux/claude-sessions/legion:11.0.state" <<EOF
SESSION_ID=${SESSION_ID}
TMUX_SESSION=legion
WINDOW=11
PANE=0
TIMESTAMP=1234567890
EOF
touch "$case_dir/home/.claude/projects/-home-user/${SESSION_ID}.jsonl"
run_restore "$case_dir" "%111" "$case_dir/home"
assert_log_contains "Test 11: restore.log step=1"      '"step":1'         "$case_dir/home/.claude/local/tmux/restore.log"
assert_log_contains "Test 11: outcome=resumed"         '"outcome":"resumed"' "$case_dir/home/.claude/local/tmux/restore.log"
assert_log_contains "Test 11: session_id details"      "${SESSION_ID:0:8}" "$case_dir/home/.claude/local/tmux/restore.log"
assert_log_contains "Test 11: pane key"                'legion:11.0'      "$case_dir/home/.claude/local/tmux/restore.log"

# ---- Test 12: full fall-through to step-4 fresh -------------------------

echo
echo "Test 12: no state, no resurrect, cwd=\$HOME → restore.log records step:4 fresh"
case_dir=$sandbox_root/t12
mkdir -p "$case_dir"
cat > "$case_dir/tmux_responses_src.txt" <<EOF
session_name=legion
window_index=12
pane_index=0
pane_id=%112
EOF
make_sandbox "$case_dir" "$case_dir/tmux_responses_src.txt"
# No state file, no resurrect, cwd=$HOME → straight to step 4
run_restore "$case_dir" "%112" "$case_dir/home"
assert_log_contains "Test 12: restore.log step=4"      '"step":4'        "$case_dir/home/.claude/local/tmux/restore.log"
assert_log_contains "Test 12: outcome=fresh"           '"outcome":"fresh"' "$case_dir/home/.claude/local/tmux/restore.log"
assert_log_contains "Test 12: pane key"                'legion:12.0'     "$case_dir/home/.claude/local/tmux/restore.log"
assert_log_excludes "Test 12: NO step-1 line"          '"step":1'        "$case_dir/home/.claude/local/tmux/restore.log"

# ---- Test 13: LEGION_RESTORE_TELEMETRY=0 → no log lines written --------

echo
echo "Test 13: LEGION_RESTORE_TELEMETRY=0 → no restore.log written"
case_dir=$sandbox_root/t13
SESSION_ID="13131313-aaaa-bbbb-cccc-131313131313"
mkdir -p "$case_dir"
cat > "$case_dir/tmux_responses_src.txt" <<EOF
session_name=legion
window_index=13
pane_index=0
pane_id=%113
EOF
make_sandbox "$case_dir" "$case_dir/tmux_responses_src.txt"
cat > "$case_dir/home/.local/share/tmux/claude-sessions/legion:13.0.state" <<EOF
SESSION_ID=${SESSION_ID}
TMUX_SESSION=legion
WINDOW=13
PANE=0
TIMESTAMP=1234567890
EOF
touch "$case_dir/home/.claude/projects/-home-user/${SESSION_ID}.jsonl"
LEGION_RESTORE_TELEMETRY=0 run_restore "$case_dir" "%113" "$case_dir/home"
# Restore should still succeed (claude invoked).
assert_log_contains "Test 13: claude still invoked"  "CLAUDE_INVOKED" "$case_dir/run.log"
assert_log_contains "Test 13: --resume still used"   "--resume"       "$case_dir/run.log"
# But restore.log must NOT exist (or must be empty).
if [ ! -e "$case_dir/home/.claude/local/tmux/restore.log" ]; then
    echo "  ✓ Test 13: restore.log absent when telemetry disabled"
    pass=$((pass + 1))
else
    if [ ! -s "$case_dir/home/.claude/local/tmux/restore.log" ]; then
        echo "  ✓ Test 13: restore.log empty when telemetry disabled"
        pass=$((pass + 1))
    else
        echo "  ✗ Test 13: restore.log has content despite telemetry disabled"
        sed 's/^/      /' "$case_dir/home/.claude/local/tmux/restore.log"
        fail=$((fail + 1))
    fi
fi

# ---- Summary -------------------------------------------------------------

echo
echo "=================================="
echo "claude-restore.sh tests: $pass passed, $fail failed"
echo "=================================="
[ "$fail" -eq 0 ]
