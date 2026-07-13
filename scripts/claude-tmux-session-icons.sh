#!/usr/bin/env bash
# Set @session_icon for each tmux session based on session name → persona mapping
# Called by claude-tmux-setup.sh and hook.sh (on SessionStart)
source "$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/lib/common.sh"

for sess in $(tmux list-sessions -F '#{session_name}' 2>/dev/null); do
    emoji=$(get_session_emoji "$sess" "✨")
    # Trailing colon forces session-level targeting. Without it, numeric session
    # names like "5" resolve to window index 5 in the current session instead.
    tmux set-option -t "${sess}:" @session_icon "$emoji" 2>/dev/null
done
