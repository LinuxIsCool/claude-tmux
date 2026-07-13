#!/usr/bin/env bash
# sync-cache.sh — Copy source to cache and restart arbiter.
# Run after ANY edit to hook.sh, tmux-arbiter.py, or other runtime files.
# Claude Code runs hooks from the cache, not the source.

set -euo pipefail

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VERSION=$(jq -r .version "$SRC/.claude-plugin/plugin.json")
MARKETPLACE="${CLAUDE_TMUX_MARKETPLACE:-claude-tmux}"
CACHE="$HOME/.claude/plugins/cache/$MARKETPLACE/claude-tmux/$VERSION"

if [ ! -d "$CACHE" ]; then
    echo "ERROR: Cache directory not found: $CACHE"
    echo "Run: claude plugin install claude-tmux@$MARKETPLACE"
    exit 1
fi

echo "Syncing $SRC → $CACHE"
rsync -a --exclude='.git' --exclude='.letta' --exclude='__pycache__' "$SRC/" "$CACHE/"
echo "Synced."

# Restart arbiter if running
if systemctl --user is-active --quiet claude-tmux-arbiter.service 2>/dev/null; then
    systemctl --user restart claude-tmux-arbiter.service
    echo "Arbiter restarted."
fi

echo "Done. New sessions will use updated hooks."
echo "NOTE: Existing resumed sessions keep their frozen hook registries."
