---
description: "Set, clear, or inspect the focus indicator for the current tmux pane"
argument-hint: "[venture:<slug> | plugin:<name> | clear | (no args = show status)]"
allowed-tools: [Bash, Read]
---

# /focus Command

Manage the focus indicator for the current tmux pane. Focus shows which venture/plugin
you're working on in the tmux tab bar.

## Routing

| Input | Action |
|-------|--------|
| `/focus` | Show current focus status with all signals |
| `/focus venture:<slug>` | Set venture focus (explicit override) |
| `/focus plugin:<name>` | Set plugin focus (explicit override) |
| `/focus <slug>` | Auto-detect: try venture first, then plugin |
| `/focus clear` | Remove explicit override, revert to auto-detection |

## Implementation

The arbiter daemon manages focus state. Communicate via socat over the Unix socket
at `~/.claude/local/tmux/arbiter.sock`.

Get the current pane ID:
```bash
PANE="${TMUX_PANE:-$(tmux display-message -p '#{pane_id}')}"
SOCK="$HOME/.claude/local/tmux/arbiter.sock"
```

### Status (no args)

```bash
RESULT=$(printf '{"type":"focus","action":"status","pane":"%s"}\n' "$PANE" \
  | socat -t 1 - "UNIX-CONNECT:$SOCK" 2>/dev/null)
```

Parse `RESULT` as JSON. Display a formatted table:

```
Focus: [emoji] [venture_key] + [plugin_key]

Signals:
  [key]              [source]      w=[weight]  age=[age]s  decayed=[decayed]
  ...
```

For signals with source `window_name` or `explicit`, show `(sticky)` instead of age/decayed.

### Set (venture:X or plugin:X)

If the argument contains `:`, use it directly as the key.

If NOT, auto-detect by checking `~/.claude/local/emoji/emoji_flat.json`:
1. Try `venture:{arg}` — if emoji exists, use it
2. Try `plugin:claude-{arg}` — if emoji exists, use it
3. Try `plugin:{arg}` — if emoji exists, use it
4. Check aliases.json ventures and plugins sections
5. If nothing matches, tell the user

```bash
printf '{"type":"focus","action":"set","pane":"%s","key":"%s"}\n' "$PANE" "$KEY" \
  | socat -t 1 - "UNIX-CONNECT:$SOCK" 2>/dev/null
```

Report: `Focus set: [key] [emoji] (explicit override — use /focus clear to revert)`

### Clear

```bash
printf '{"type":"focus","action":"clear","pane":"%s"}\n' "$PANE" \
  | socat -t 1 - "UNIX-CONNECT:$SOCK" 2>/dev/null
```

Report: `Focus cleared. Auto-detection active.` Then show current auto-detected focus.

## Key Validation

Before sending a `set` command, validate the key exists in emoji_flat.json:

```bash
python3 -c "
import json, sys
data = json.load(open('$HOME/.claude/local/emoji/emoji_flat.json'))
key = sys.argv[1]
if key in data:
    print(data[key])
else:
    print('')
" "$KEY"
```

If empty, the key has no assigned emoji. Warn the user but still set it (the signal
is valid even without an emoji — it participates in composition grammar).
