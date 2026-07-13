---
name: tmux-stream
description: >
  Search and browse terminal history from all tmux panes. Use when asked about
  past terminal output, errors, commands, or what was running at a specific time.
  Triggers: "what was the error", "show terminal history", "what did pane X do",
  "search terminal for", "tmux stream", "stream log", "stream search".
allowed-tools: Bash, Read, Glob
---

# tmux-stream

Search and browse the structured terminal history captured by tmux-stream.

## Data Location

Daily JSONL files: `~/.claude/local/tmux-stream/YYYY-MM-DD.jsonl`
Compressed archives: `~/.claude/local/tmux-stream/YYYY-MM-DD.jsonl.zst`

## CLI

The CLI is at `${CLAUDE_PLUGIN_ROOT}/scripts/tmux-stream-cli.py`.

```bash
# Show recent segments
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/tmux-stream-cli.py" log --since 2h

# Search for errors
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/tmux-stream-cli.py" search "error|failed|FAIL" --since 4h

# Search specific pane
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/tmux-stream-cli.py" log --pane %42 --since 1h

# Only errors
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/tmux-stream-cli.py" log --errors --since 4h

# Stats for today
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/tmux-stream-cli.py" stats

# System status
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/tmux-stream-cli.py" status

# Search a specific date
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/tmux-stream-cli.py" search "build" --date 2026-04-14
```

## Direct jq Queries

For complex queries, read the JSONL directly:

```bash
# All errors today
jq 'select(.exit_code != null and .exit_code != 0)' ~/.claude/local/tmux-stream/$(date -u +%Y-%m-%d).jsonl

# Segments from a specific session
jq 'select(.session=="legion-matt")' ~/.claude/local/tmux-stream/$(date -u +%Y-%m-%d).jsonl

# Search compressed archives
zstdcat ~/.claude/local/tmux-stream/2026-04-14.jsonl.zst | jq 'select(.output | test("pattern"))'
```

## Segment Schema

Each line is a JSON object with these fields:

| Field | Type | Description |
|-------|------|-------------|
| `pane` | string | tmux pane ID (e.g., `%42`) |
| `session` | string | tmux session name |
| `window` | string | Window identifier (e.g., `3:agents`) |
| `logical_id` | string | Human-readable: `session:window.pane_index` |
| `seq` | int | Per-pane monotonic counter |
| `ts` | string | ISO 8601 timestamp (UTC) |
| `command` | string? | Shell command (Fish-marked panes only) |
| `exit_code` | int? | Exit code (Fish-marked panes only) |
| `duration_ms` | int? | Command duration in milliseconds |
| `output` | string | Plaintext output (ANSI stripped) |
| `claude_session` | string? | Claude Code session ID |
| `persona` | string? | Persona emoji (e.g., `🪷`) |
| `tui` | bool? | True if TUI output detected |
| `continuation` | bool? | True if segment was split due to size/time limit |
| `type` | string? | Special segment type (e.g., `"gap"` for backpressure) |

## When to Use

- "What was the error I saw earlier?" → `search "error|fail|traceback" --since 4h`
- "Show me what pane 3 was doing at 2pm" → `log --pane %XX --since 6h` + filter by timestamp
- "How many commands did I run today?" → `stats`
- "Is the stream system healthy?" → `status`
- "What built failed yesterday?" → `search "error" --date YYYY-MM-DD`
