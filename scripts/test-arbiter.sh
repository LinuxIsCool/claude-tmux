#!/usr/bin/env bash
# Isolated arbiter smoke test. Never connects to an operator arbiter or tmux.
# Run with a Python containing pytest and hypothesis (PYTHON overrides python3).
set -euo pipefail

repo_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
python_bin="${PYTHON:-python3}"
pytest_args=(-q)
for arg in "$@"; do
    case "$arg" in
        -v|--verbose) pytest_args=(-v) ;;
        --fast) ;;
        *) echo "Usage: $0 [-v|--verbose] [--fast]" >&2; exit 2 ;;
    esac
done
sandbox_dir=$(mktemp -d /tmp/ct-smoke.XXXXXXXX)
cleanup() {
    "$python_bin" -c 'import shutil, sys; shutil.rmtree(sys.argv[1])' "$sandbox_dir"
}
trap cleanup EXIT
mkdir -p "$sandbox_dir/home" "$sandbox_dir/tmp" "$sandbox_dir/tmux" "$sandbox_dir/runtime"
# Override inherited routing before Python imports any application modules.
env -u TMUX -u TMUX_PANE -u CLAUDE_TMUX_THEME -u DBUS_SESSION_BUS_ADDRESS \
    -u PI_CODING_AGENT_DIR -u OMP_CODING_AGENT_DIR -u CLAUDE_CONFIG_DIR \
    HOME="$sandbox_dir/home" TMPDIR="$sandbox_dir/tmp" TMUX_TMPDIR="$sandbox_dir/tmux" \
    XDG_RUNTIME_DIR="$sandbox_dir/runtime" CODEX_HOME="$sandbox_dir/home/.codex" \
    CLAUDE_TMUX_SOCKET="$sandbox_dir/tmux/unavailable.sock" PYTHONDONTWRITEBYTECODE=1 \
    "$python_bin" -B -m pytest -p no:cacheprovider "${pytest_args[@]}" \
    "$repo_dir/tests/test_arbiter_states.py" "$repo_dir/tests/test_lifecycle_reconcile.py" \
    "$repo_dir/tests/test_tmux_writer.py"
