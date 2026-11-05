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

The arbiter owns state and aggregates every pane in a window. A working sibling stays visible when an idle pane is selected. Hooks are thin forwarders; if the daemon is down, hooks fall back to direct tmux writes.

On Linux, native lifecycle readers also cover existing Claude Code and Codex sessions without restarting them. Claude's process registration supplies current activity. Codex's held root rollout and writer lock bind native turn boundaries to the actual CLI. These readers preserve working during silent provider waits. CPU inactivity does not override their state. Missing, ambiguous or damaged evidence displays a hollow muted circle (`unknown`).

Eight states are defined in `theme.json`: idle, unread, working, agents, asking, error, nudge and unknown. A finished response in an unfocused window shows lavender until you visit it. The aggregate prioritizes working and delegated work before input requests, errors and idle panes.

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

### Pi and Oh My Pi lifecycle

Install the metadata-only native extension from this checkout:

```bash
mkdir -p ~/.pi/agent/extensions ~/.omp/agent/extensions
cp scripts/pi-lifecycle-extension.ts ~/.pi/agent/extensions/legion-tmux.ts
cp scripts/pi-lifecycle-extension.ts ~/.omp/agent/extensions/legion-tmux.ts
```

Ordinary new sessions discover it automatically. For a custom agent directory, install it in that directory's `extensions/` folder. A launch using `--no-extensions` needs an explicit `--extension /path/to/pi-lifecycle-extension.ts`. Existing processes keep their loaded extensions and are never restarted by the arbiter.

The extension writes atomic lifecycle metadata under the configured agent directory. The reader checks process start time, PID namespace and session identity. Pi completion uses `agent_settled`; Oh My Pi uses its settled `agent_end` boundary and continuation flags. Message persistence alone does not establish turn completion, so sessions without native evidence remain unknown. Arbitrary extension-owned dialogs have no generic input-wait event and are not inferred from transcript text.

## Configuration

Defaults ship in `config.json` and `theme.json`. Machine-local overrides (never touched by updates) live at `~/.claude/local/tmux/config.json` and `~/.claude/local/tmux/theme.json`. Hot-reload with `kill -HUP $(cat ~/.claude/local/tmux/arbiter.pid)`.

Timing, decay half-lives, emoji slots, and the optional shared-vault person attribution are all documented in [CLAUDE.md](CLAUDE.md), which is the full architecture reference: state table, wire protocol, reconciliation behaviors, forensics, and troubleshooting.

## Status ownership and recovery

The [2026-11-04 false working incident](docs/incidents/2026-11-04-false-working-state.md)
records the failure chain, attribution limits, corrective commit and verification.

Native lifecycle metadata establishes which live process and session owns a
pane. The arbiter combines that evidence with validated hooks, persists a
derived cache, and projects pane and window options into tmux. The window
badge summarizes its panes. It does not establish whether an agent is busy.

The failure boundaries are:

| Boundary | Rule |
| --- | --- |
| Event routing | A pane number alone is insufficient. Require its native provider/session, live PID start time and pane ancestry. New envelopes also require the exact tmux socket and a current timestamp. |
| Hook precedence | A busy/dialog hook can cover native write latency for `hook_priority_secs` (60 seconds by default). It cannot keep an unchanged native idle reading green indefinitely. Native working state does not expire during quiet inference. |
| Recovery | Saved active badges are unvalidated cache entries. Read current native evidence before projecting them. Reject malformed records individually and clear entries whose execution has exited. |
| Transport | Forward bounded lifecycle metadata rather than tool bodies. Receiver rejections are counted and logged with session/process provenance. A hook always returns one valid response. |
| Testing | Use private homes, profiles, runtime directories and tmux sockets. The test harness rejects access to production files, services and Unix sockets. |
| Supervision | Start an installed user service through systemd. Direct daemon startup is reserved for installations without that unit. |

The absolute path of a custom tmux socket is retained and used with `-S`.
Window-focus messages carry the same socket identity, so another server's
`%0` cannot mark the production `%0` read.

Remaining limits are explicit. Native readers currently require Linux `/proc`;
this is not a cross-platform process collector. One arbiter monitors one tmux
server. Legacy Codex adapters lack the new source envelope and are accepted
only when provider/session has one native owner and the live process binding
still matches. They cannot supply sender timestamps for ordering checks.
Moving those adapters into a maintained source package and retiring legacy
transport is the next boundary to close. Pi/OMP sessions without their native
extension remain unknown. Adding a dashboard or database does not fill those
evidence gaps.

The event stream is not a durable task ledger. Native records reconstruct
activity after event loss or restart, while saved read/unread state is a
best-effort UI checkpoint. Future consumers should use a versioned projection
of these same validated observations rather than infer a second lifecycle
from CPU use, terminal text or window colors.

## Development

```bash
uv run --with pytest --with 'hypothesis>=6.150' pytest tests/ -v   # arbiter state machine
bun test                                                           # web topology viewer
bash tests/test_tombstone_gc.sh && bash tests/test_claude_restore.sh
```

Tests use disposable state and isolated tmux servers. Native Pi and Oh My Pi checks use a loopback test provider with a silent response interval, require no paid inference, and skip when the corresponding CLI is unavailable. Host-integration tests skip when optional data is absent.

## License

MIT
