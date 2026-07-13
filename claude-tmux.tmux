#!/usr/bin/env bash
# TPM entry point — injects format strings into tmux status bar
# Hook scripts are registered separately via plugin.json (Legion plugin)
CURRENT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
bash "$CURRENT_DIR/scripts/claude-tmux-setup.sh"
