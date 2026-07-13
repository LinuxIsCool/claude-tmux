---
name: claude-tmux
description: "Setup, configure, and troubleshoot claude-tmux activity indicators in tmux"
---

# /claude-tmux

Activity indicators in tmux window tabs showing Claude Code session state.

## Quick Setup

```bash
bash "${CLAUDE_PLUGIN_ROOT}/scripts/claude-tmux-setup.sh"
```

Ensure `claude-tmux@claude-tmux` is `true` in `~/.claude/settings.json` → `enabledPlugins`.

## Architecture

Hooks write state to **tmux pane options** (`@claude_state`, `@claude_color`).
Format strings read them with `#{@claude_state}` and `#[fg=#{@claude_color}]`.

**No files. No `#()` shell commands. No polling. Pure tmux native variables.**

### Why Pane Options, Not Files

| Approach | Problem |
|----------|---------|
| Flat files + `#(cat file)` | tmux does NOT reliably parse `#[fg=...]` style sequences from `#()` output in `window-status-format` |
| Flat files + `#(script)` | Same problem — `#()` output is treated as literal text for styling |
| **Pane options + `#{@var}`** | **Native tmux variable expansion — same code path as `#I`, `#W`, guaranteed to work** |

### State Machine

| Hook | State | Color | Character |
|------|-------|-------|-----------|
| `SessionStart` | Idle | Blue `#89b4fa` | ⬤ |
| `UserPromptSubmit` | Working | Green `#a6e3a1` | ⬤ |
| `Stop` | Idle | Blue `#89b4fa` | ⬤ |
| `SubagentStart` | Agents | Mauve `#cba6f7` | ◉ |
| `SubagentStop` | Working | Green `#a6e3a1` | ⬤ |
| `PermissionRequest` | Asking | Yellow `#f9e2af` | ? |
| `SessionEnd` | Clear | — | (removed) |

### Format String Injection

The setup script reads tmux-power's format and inserts before `#I`:

```
Before: " #I:#W#F "
After:  " #[fg=#{@claude_color}]#{@claude_state}#[fg=default] #I:#W#F "
```

**Critical**: Use `#[fg=default]` (not `#[default]`) after the indicator to restore
only foreground color without nuking tmux-power's background and bold attributes.

## Troubleshooting

### No indicator on a window
The Claude session in that window hasn't loaded the plugin yet. Either:
- Restart Claude in that pane (hooks register on session start)
- Manually set: `tmux set -p -t %PANE @claude_state "⬤" && tmux set -p -t %PANE @claude_color "#89b4fa"`

### Indicator doesn't change color
Hooks aren't firing. Check:
1. `claude-tmux@claude-tmux` is `true` in `~/.claude/settings.json`
2. Plugin is in marketplace.json
3. `$TMUX_PANE` is set in the Claude session: run `echo $TMUX_PANE`

### Colors wrong or invisible
- Ensure truecolor: `set -ga terminal-overrides ",*256col*:Tc"` in tmux.conf
- Test: `tmux set -p @claude_color "#ff0000" && tmux set -p @claude_state "⬤"` — should show red circle
- Contrast: idle color must contrast with status-bg (`#262626` for tmux-power snow)

### Format string corrupted after theme change
Re-run setup: `bash scripts/claude-tmux-setup.sh`
If already injected, remove old format first:
```bash
# Reset to tmux-power defaults, then re-inject
tmux source ~/.config/tmux/tmux.conf
bash scripts/claude-tmux-setup.sh
```

## Hard-Won Lessons

These are critical architectural facts discovered through empirical testing:

1. **tmux `#()` output is NOT parsed for `#[fg=...]` style sequences** in window-status-format.
   The code review said it should work. The documentation implies it works. It does NOT work
   reliably. Pane options (`#{@var}`) are the correct approach.

2. **`#[default]` resets ALL attributes** — foreground, background, bold, dim, everything.
   Inside tmux-power's styled tabs, this destroys the tab background. Use `#[fg=default]` instead.

3. **`#[fg=#{@claude_color}]` nested expansion DOES work** — tmux expands `#{@claude_color}`
   to a color value, then applies `#[fg=VALUE]`. This is the key to per-state coloring.

4. **`SessionStart` hook sets initial state** — the blue ⬤ appears the moment Claude launches,
   before the user types anything. Without this hook, new sessions have no indicator until first prompt.

5. **Plugin cache is the execution layer** — Claude Code runs hooks from `~/.claude/plugins/cache/`,
   NOT from the clone. Cache uses **versioned subdirectories** per release.
   Running sessions reference their version via `CLAUDE_PLUGIN_ROOT`. **Never `rsync --delete` the
   cache top-level** — this deletes versioned dirs and silently breaks all running sessions.
   Sync INTO the versioned dir: `rsync -a --exclude='.letta' SOURCE/ CACHE/$VERSION/`

6. **`tmux set -pu`** (with `-u` flag) unsets a pane option, cleanly removing the indicator
   on session end.

7. **Hooks are synchronous** — `tmux set -p` completes before the hook exits, so the indicator
   updates before Claude's next output reaches the terminal.

8. **WCAG contrast matters** — `#313244` on `#262626` is 1.2:1 (invisible). `#585b70` is 2.3:1
   (barely visible). `#89b4fa` is 7.2:1 (clear). Choose indicator colors with > 4.5:1 contrast.
