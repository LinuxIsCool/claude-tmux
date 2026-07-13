# claude-tmux

Live activity indicators in tmux window tabs for [Claude Code](https://claude.com/claude-code) sessions. Every window shows what its Claude is doing right now: working, idle, waiting on a permission, running subagents, or holding a response you haven't read yet. Sessions survive reboots: tmux-resurrect integration resumes the actual conversation, not just the process.

![window tab indicators](assets/indicators.svg)

*Illustration of the tab grammar using the shipped theme's real tokens: green working, blue idle, lavender unread, mauve subagents, yellow permission prompt, red error.*

## How it works

```
Claude Code fires hook
  → hook.sh (thin socat forwarder, ~5ms)
    → tmux-arbiter.py (asyncio daemon: owns all state)
      → pane-states.json (source of truth, atomic writes)
        → tmux pane options (@claude_state / @claude_color / @claude_persona)
          → window-status-format  →  ⬤ 5:my-project
```

The arbiter is a daemon-owned Moore state machine. Hooks are thin forwarders; if the daemon is down, hooks fall back to direct tmux writes. Multi-signal detection (CPU sampling + child-process age) covers resumed sessions whose hooks predate the current install. Hysteresis and reconciliation loops prevent flicker and stuck states.

Seven states, all defined in `theme.json` (semantic colors, glyphs, hook-to-state mapping): idle, unread, working, agents, asking, error, nudge. `unread` is the one that changes how you work: a finished response in an unfocused window shows lavender until you actually visit it.

Optional segments enrich the tabs when their data sources exist: persona emoji, hook-event emoji (two-slot grammar, e.g. 🔧📖 for "reading a file"), and a focus indicator resolving which project the session is working in. All degrade to nothing when absent.

## Install

Requirements: tmux, `jq`, [uv](https://docs.astral.sh/uv/). Optional: `socat` (fast hook path), [bun](https://bun.sh) (web topology viewer, dev only).

```bash
git clone https://github.com/LinuxIsCool/claude-tmux ~/claude-tmux
```

In Claude Code:

```
/plugin marketplace add ~/claude-tmux
/plugin install claude-tmux@claude-tmux
```

Wire it into tmux (in your tmux.conf, adjust the clone path):

```bash
run-shell "bash ~/claude-tmux/scripts/claude-tmux-setup.sh"
```

The setup script injects the indicator format strings, starts the arbiter if it isn't running, and registers the pane-died self-healing hook. For boot persistence, run the arbiter as a systemd user service:

```ini
[Service]
ExecStart=/usr/bin/env uv run /home/YOU/claude-tmux/scripts/tmux-arbiter.py
Restart=on-failure
RestartSec=3
```

### Session resurrection (optional)

With [tmux-resurrect](https://github.com/tmux-plugins/tmux-resurrect), Claude sessions resume their actual conversations after a reboot:

```bash
set -g @resurrect-processes '"~claude->~/claude-tmux/scripts/claude-restore.sh"'
set -g @resurrect-hook-post-save-layout '~/claude-tmux/scripts/claude-save-sessions.sh'
```

## Configuration

Defaults ship in `config.json` and `theme.json`. Machine-local overrides (never touched by updates) live at `~/.claude/local/tmux/config.json` and `~/.claude/local/tmux/theme.json`. Hot-reload with `kill -HUP $(cat ~/.claude/local/tmux/arbiter.pid)`.

Timing, decay half-lives, emoji slots, and the optional shared-vault person attribution are all documented in [CLAUDE.md](CLAUDE.md), which is the full architecture reference: state table, wire protocol, reconciliation behaviors, forensics, and troubleshooting.

## Development

```bash
uv run --with pytest --with 'hypothesis>=6.150' pytest tests/ -v   # arbiter state machine
bun test                                                           # web topology viewer
bash tests/test_tombstone_gc.sh && bash tests/test_claude_restore.sh
```

Four suites, 271 tests, all self-contained (no host state required; host-integration tests skip cleanly when optional data is absent).

## License

MIT
