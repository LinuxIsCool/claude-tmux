#!/usr/bin/env bash
# stream-rotate.sh — Compress yesterday's tmux-stream JSONL file.
# Intended for daily cron/systemd timer: compress day-old files with zstd.
#
# Usage: bash stream-rotate.sh
#
# Only compresses files older than today. Never touches today's active file.
# Uses flock to prevent concurrent rotations.

STREAM_DIR="$HOME/.claude/local/tmux-stream"
LOCK_FILE="/tmp/stream-rotate.lock"

if [ ! -d "$STREAM_DIR" ]; then
    exit 0
fi

# Prevent concurrent rotations
exec 9>"$LOCK_FILE"
flock -n 9 || { echo "rotation already running"; exit 0; }

today=$(date +%Y-%m-%d)

for f in "$STREAM_DIR"/*.jsonl; do
    [ -f "$f" ] || continue
    basename=$(basename "$f" .jsonl)
    # Skip today's file
    if [ "$basename" = "$today" ]; then
        continue
    fi
    # Verify file still exists (another process may have compressed it)
    [ -f "$f" ] || continue
    # Compress with zstd at max compression
    if command -v zstd >/dev/null 2>&1; then
        zstd -19 --rm -q "$f" && echo "compressed: $f"
    fi
done
