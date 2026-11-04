# claude-tmux

Activity indicators in tmux window tabs for Claude Code sessions.

## Architecture

The arbiter owns the lifecycle projection. Native, process-bound metadata
establishes execution ownership and turn state. Validated hooks cover write
latency and dialogs. Config hot-reloads via SIGHUP. See
[status ownership and recovery](README.md#status-ownership-and-recovery) for
the current invariants and limitations. CPU fallback details below describe
legacy compatibility paths, not authoritative native lifecycle state.

```
Claude Code fires hook
  → hook.sh (15-line socat forwarder)
    → ~/.claude/local/tmux/arbiter.sock
      → tmux-arbiter.py (Python asyncio daemon — owns all state)
        → ~/.claude/local/tmux/pane-states.json  (derived cache, atomic writes)
          → tmux set -p @claude_state/@claude_color/@claude_persona  (derived view)
            → window-status-format  →  🏗️⬤ 5:claude-tmux
```

**Fallback path** (daemon not running): hook.sh falls back to direct tmux writes via
the original bash state resolution in `lib/common.sh`. ~50ms vs ~5ms for the fast path.

**Signal priority:** native process/session ownership first. Hooks must match
that independently observed execution. Busy/dialog hooks have a bounded
60-second grace window. Native working state remains working during silent
inference, regardless of CPU activity. Unknown evidence remains unknown.

**Multi-signal activity detection:**
- CPU tick delta: /proc/pid/stat sampling, configurable window (default 100ms)
- Child process age: ps --ppid, <10s = tool executing
- Composite: either signal active = working; both inactive = idle
- Hysteresis: configurable holdoff (default 6s) prevents blue/green flicker
- Only runs for hookless panes (no events in 15s+)

**Split-pane support:**
- Pane options cascade to window-level via after-select-pane hook
- Format strings: pane @claude_state -> window @claude_window_state -> empty

**Read/unread indicator:**
- Stop on unfocused pane -> lavender (unread)
- User visits pane -> blue (idle/read)
- PaneRead synthetic event from tmux-window-state.sh
- Unread persists indefinitely until explicit PaneRead (no auto-clear)

**Dependency**: `socat` must be installed for the fast path (`pacman -S socat`).

## State Machine

All visual tokens (colors, glyphs, persona emoji) are defined in **`theme.json`** —
the single source of truth. `lib/common.sh` reads it via `jq`. To change any
color or glyph, edit `theme.json` and everything updates: hooks, indicators, docs.

The mapping chain: `hook event` -> `hook_to_state` -> `state` -> `semantic color` -> hex.

### Complete State Table (7 states)

| State | Color | Hex | Glyph | Meaning |
|-------|-------|-----|-------|---------|
| idle | blue | `#89b4fa` | ⬤ | Waiting for input (read) |
| unread | lavender | `#b4befe` | ⬤ | Response ready (not yet seen) |
| working | green | `#a6e3a1` | ⬤ | Claude generating |
| agents | mauve | `#cba6f7` | ◉ | Subagent active |
| asking | yellow | `#f9e2af` | ? | Permission dialog |
| error | red | `#f38ba8` | ! | Error / rate limit |
| nudge | grey-blue | `#6699cc` | ⬤ | Idle nudge |

### Complete Hook-to-State Mapping (14 events + Notification dispatch)

| Hook Event | State | Visual | Meaning |
|-----------|-------|--------|---------|
| SessionStart | idle | 🔵 ⬤ | Session started, awaiting input |
| UserPromptSubmit | working | 🟢 ⬤ | Claude processing user input |
| PreToolUse | working | 🟢 ⬤ | Tool call executing |
| PostToolUseFailure | error | 🔴 ! | Tool execution failed |
| Stop | idle | 🔵 ⬅ | Claude finished responding |
| StopFailure | error | 🔴 ! | Turn ended due to API error |
| PermissionRequest | asking | 🟡 ? | Permission dialog shown |
| SubagentStart | agents | 🟣 ◉ | Subagent spawned |
| SubagentStop | working | 🟢 ⬅ | Subagent done, parent resumes |
| TeammateIdle | idle | 🔵 ⬅ | Multi-agent teammate finished |
| TaskCompleted | idle | 🔵 ⬅ | Task marked complete |
| PreCompact | working | 🟢 ⬅ | Context compaction in progress |
| PostCompact | idle | 🔵 ⬅ | Compaction done |
| SessionEnd | clear | — | Session ended, indicator removed |

### Notification Dispatch

`Notification` events carry `notification_type` and are dispatched to different states:

| notification_type | State | Visual | Rationale |
|-------------------|-------|--------|-----------|
| `permission_prompt` | asking | 🟡 ? | User must approve a tool |
| `idle_prompt` | idle | 🔵 ⬅ | Claude done, waiting for input |
| `auth_success` | idle | 🔵 ⬅ | Informational |
| `elicitation_dialog` | asking | 🟡 ? | MCP server wants input |
| (unknown) | nudge | ⚪ ⬅ | Fallback |

### Events NOT Registered (No Visual Impact)

PostToolUse (redundant with PreToolUse), InstructionsLoaded, ConfigChange,
CwdChanged, FileChanged, WorktreeCreate/Remove, TaskCreated, Setup,
Elicitation/ElicitationResult.

## Arbiter Daemon

`scripts/tmux-arbiter.py` — Python asyncio daemon, zero external dependencies
(PEP 723 inline metadata, runs via `uv run`).

### Paths

| Resource | Path |
|----------|------|
| Unix socket | `~/.claude/local/tmux/arbiter.sock` |
| State file | `~/.claude/local/tmux/pane-states.json` |
| Log file | `~/.claude/local/tmux/arbiter.log` |
| systemd service | `~/.config/systemd/user/claude-tmux-arbiter.service` |

### Key Behaviors

**Startup guard** — Any `working`-state transition within **2 seconds** of `SessionStart`
is suppressed. Claude Code fires `PreToolUse` during its init sequence (loading plugins,
running hooks) which is indistinguishable from real tool use at hook level. The guard
eliminates spurious green-on-startup.

**Native reconciliation** — Native evidence is read every second. Busy/dialog hooks
can override older native metadata for at most `hook_priority_secs` (60 seconds by
default). Quiet native working turns stay working. Projection drift is repaired
every 3 seconds by default. Legacy panes without native evidence retain the
2-minute fallback timeout. `unread` persists until the user visits the pane.

**Subagent depth tracking** — `PaneState.subagent_depth` counts unmatched SubagentStart/
SubagentStop pairs. When `Stop` fires while depth > 0 (a subagent is still running),
the transition returns to `agents` instead of `idle`. This fixes the common pattern:
`SubagentStart` → `agents`, then `PermissionRequest` → `asking`, then `Stop` (permission
dialog dismissed) → back to `agents` instead of incorrectly going `idle`.

**Persona stickiness** — Persona emoji is stored in `pane-states.json`. On arbiter restart,
persona is restored from disk. Events without `agent_type` preserve the existing persona
rather than wiping it.

**@claude_voice isolation** — `TmuxWriter` explicitly never touches `@claude_voice`.
That option is owned exclusively by `voice_arbiter.py`.

**Compaction guard** — `SessionStart` with `source=compact` is a no-op. The session is
still active during compaction; the indicator should stay as-is.

### Wire Protocol

hook.sh sends newline-delimited JSON over the Unix socket:
```json
{
  "type": "event",
  "event": "PreToolUse",
  "pane": "%12",
  "source": {
    "socket_path": "/path/to/tmux.sock",
    "provider": "claude",
    "session_id": "session-id",
    "sent_at": 1790000000
  },
  "data": {"_provider": "claude", "session_id": "session-id"}
}
```

Accepted events are fire-and-forget. Rejections carry a diagnostic reason and
increment status counters. The producer discards transport responses and
returns exactly `{}` to Claude. Payloads contain bounded lifecycle metadata,
not full tool inputs or responses. Legacy Codex messages require a unique live
native provider/session binding, and a turn ID must match when supplied.

### Check / Stop

```bash
# Check daemon health
uv run scripts/tmux-arbiter.py --check

# Stop gracefully (also: systemctl --user stop claude-tmux-arbiter)
uv run scripts/tmux-arbiter.py --stop
```

### systemd Service

```bash
# Enable at login
systemctl --user enable claude-tmux-arbiter

# Manual control
systemctl --user start|stop|restart|status claude-tmux-arbiter

# Logs
journalctl --user -u claude-tmux-arbiter -f
```

The service is started unconditionally by `claude-tmux-setup.sh` at tmux startup if not
already running. `Restart=on-failure` with `RestartSec=3` handles crashes.

### Debug Logging

Opt-in state transition tracing:

```bash
tmux set -p @claude_tmux_debug "on"    # enable
tail -f /tmp/claude-tmux-debug.log     # watch
tmux set -pu @claude_tmux_debug        # disable
```

Each line: `HH:MM:SS.mmm EVENT %PANE_ID`

### Compaction Guard

When Claude Code compacts context, it fires `SessionStart` with `source=compact`. The hook
detects this and skips the indicator update — the session is still active, so the indicator
should stay on whatever state it was showing (typically working/blue).

### Persona Emoji

Events carrying `agent_type` (e.g. `SubagentStart`, `PostCompact`) trigger persona extraction.
The key after `:` (e.g. `claude-personas:matt` -> `matt`) is looked up in theme.json's `personas`
section. The emoji is set as `@claude_persona` tmux pane option and prepended to the indicator.

### Hook Emoji (@claude_hook)

Shows the most recent Claude Code hook event as emoji in the tmux tab. Uses a
**two-slot grammar**: event glyph (always) + refinement glyph (when context exists).

**Source**: `~/.claude/local/emoji/emoji_flat.json` (canonical emoji dictionary,
maintained by `claude-emoji` plugin). Auto-reloads on file change.

**Slot 1 (event)**: `event:{hook-type-kebab}` — one unique emoji per hook type.
**Slot 2 (refinement)**: `{category}:{value}` — tool name, notification type, etc.

Examples:
- `🔧📖` — PreToolUse + Read (tool use, reading a file)
- `🔧🐚` — PreToolUse + Bash (tool use, shell command)
- `💬` — UserPromptSubmit (no refinement)
- `🚀🪷` — SubagentStart + matt persona
- `🔔💤` — Notification + idle_prompt
- `🏁` — Stop (no refinement)

**Refinement map** (which hooks carry extra context):

| Hook | Context Field | Category |
|------|--------------|----------|
| PreToolUse / PostToolUse / PostToolUseFailure | `tool_name` | `tool:*` |
| PermissionRequest | `tool_name` | `tool:*` |
| Notification | `notification_type` | `notify:*` |
| SubagentStart / SubagentStop | `agent_type` | `persona:*` |
| SessionStart / Stop | `source` | `source:*` |
| SessionEnd | `reason` | `reason:*` |

**Segment position**: Defined by `segment_order` in theme.json. Hook emoji is rightmost —
it changes most frequently, reducing visual jitter on stable left segments.

```
Persona  State  Voice  Focus  HookEmoji  WinIdx
🍄       ⬤      🎤     ⚔️     🔧📖       5:session
```

### Focus Indicator (@claude_focus)

Shows which venture or plugin the session is working in, resolved from cwd on
SessionStart and CwdChanged events.

**Resolution priority:**
1. Venture from cwd — prefix match against `~/.claude/local/emoji/ventures.json`
2. Plugin from window name — `claude-*` windows match `plugin:*` emoji keys
3. Empty — no match, no indicator

**Source**: `ventures.json` maps venture slugs to path prefixes. Emoji from
`emoji_flat.json` via `venture:{slug}` or `plugin:{name}` keys.

Cleared on SessionEnd. Persists in `pane-states.json` across arbiter restarts.

### Theme-Aware Color Restore

The indicator fragment sets `fg` to the semantic color for the dot glyph. After the dot,
it must restore to whatever color the surrounding format context expects. For non-current
windows, `#[fg=default]` is correct (matches `status-fg`). For the **current** window,
`default` would clobber the theme's text color (e.g. snow white → grey). Instead,
`claude-tmux-setup.sh` extracts the last `fg=#rrggbb` from the current-format string
at injection time and restores to that. This works with any tmux-power theme.

### Session List

`claude-tmux-setup.sh` replaces the static session name (`✨ #S`) with a dynamic list
of all sessions using `#{S:template}`. The current session is highlighted in the theme's
text color (bold), others shown in grey. Colors from `theme.json` → `session_list`.

Session navigation: `Ctrl-Up` / `Ctrl-Down` switches between sessions (from `keys.conf`).

### Persona Session Icons

Sessions named `{prefix}{persona}` (prefix defaults to `legion-`, override with
`$CLAUDE_TMUX_SESSION_PREFIX`) show the persona's emoji in the session list. All other sessions show the default sparkle ✨.

```
🪷 legion-matt  ✨ 1  ✨ my-project  ✨ voice
```

Implementation:
- `scripts/claude-tmux-session-icons.sh` — iterates all sessions, calls `get_session_emoji()`
  from `lib/common.sh`, and sets `@session_icon` per-session option via `tmux set -s`
- `lib/common.sh` → `get_session_emoji()` — strips `legion-` prefix, looks up persona in
  theme.json, returns persona emoji or default `✨`
- `scripts/claude-tmux-setup.sh` — calls session-icons script after injecting status-left;
  format uses `#{?#{@session_icon},#{@session_icon} ,}` to show icon + space when set
- `hooks/hook.sh` — calls session-icons script in background on `SessionStart` so new
  sessions get their icon immediately (backgrounded to avoid hook latency)

### Self-Healing (pane-died)

A tmux native `pane-died` hook auto-clears stale indicators when a Claude process crashes.
Registered by `claude-tmux-setup.sh` on tmux startup. Only fires for panes that have
`@claude_state` set — no effect on non-Claude panes.

### Configuration (config.json)

Defaults ship in the repo's `config.json`; machine-local overrides live at
`~/.claude/local/tmux/config.json` (merged on top, key by key). `theme.json`
follows the same pattern: `$CLAUDE_TMUX_THEME`, then
`~/.claude/local/tmux/theme.json`, then the repo default.
Hot-reload: `kill -HUP $(cat ~/.claude/local/tmux/arbiter.pid)`

| Key | Default | Description |
|-----|---------|-------------|
| `startup_guard_secs` | 2.0 | Suppress working-state within N seconds of SessionStart |
| `stale_working_mins` | 2.0 | Clear working state after N minutes with no new event |
| ~~`stale_unread_mins`~~ | ~~30.0~~ | Removed — unread persists until PaneRead |
| `reconcile_interval` | 3.0 | Seconds between reconciliation ticks |
| `holdoff_secs` | 6.0 | Hysteresis before working -> idle on hookless panes |
| `hookless_threshold` | 15.0 | Seconds of no events before CPU detection kicks in |
| `hook_priority_secs` | 60.0 | Seconds to trust hook state before reconciler overrides |
| `cpu_sample_window` | 0.1 | Seconds between CPU samples |
| `cpu_activity_threshold` | 1 | Jiffies delta to consider "active" |
| `max_event_history` | 20 | Events retained per pane for forensics |
| `focus_stale_secs` | 1800.0 | Seconds before focus-state file considered stale |
| `shared_vault_path` | "" | Path prefix attributed to a person (empty = disabled) |
| `shared_vault_person` | "" | Person slug for shared-vault file paths |

### Forensics (Event History)

Every pane tracks its last 20 state transitions. Each entry records:
- Timestamp, event name, from/to states, source (`hook` or `reconcile`)

View via `--check`:
```bash
uv run scripts/tmux-arbiter.py --check
```

Output includes per-pane event timeline:
```
  %42 🪷: working (2.3s ago) src=hook
    14:22:01 SessionStart: (new)->idle [hook]
    14:22:03 UserPromptSubmit: idle->working [hook]
```

Reconciler events use synthetic names: `cpu_detect`, `cpu_idle`, `stale_clear`.
Full history (all 20 events) persists in `~/.claude/local/tmux/pane-states.json`.

## Files

| File | Purpose |
|------|---------|
| `theme.json` | Single source of truth — semantic colors, state mappings, glyphs, persona emoji |
| `config.json` | Optional tuning overrides — hot-reload via SIGHUP |
| `scripts/tmux-arbiter.py` | **Daemon** — asyncio state machine; owns all indicator state; hot-reloads theme.json and config.json |
| `lib/common.sh` | `resolve_hook()`, `set_indicator()`, `clear_indicator()`, `save_session_state()` — used by fallback path |
| `hooks/hook.sh` | **Thin forwarder** — socat to arbiter socket (fast path); full bash logic as fallback when daemon absent |
| `scripts/claude-tmux-setup.sh` | Inject format strings into tmux; start arbiter if not running; register pane-died self-healing hook |
| `scripts/tmux-window-state.sh` | Pane focus scanner — promotes pane state to window level, handles unread->read |
| `scripts/claude-tmux-session-icons.sh` | Set `@session_icon` per-session from theme.json persona map |
| `scripts/claude-restore.sh` | Resurrect wrapper — resumes Claude session by ID or falls back |
| `scripts/claude-save-sessions.sh` | Resurrect post-save hook — consolidates state files |
| `scripts/sync-cache.sh` | Copy source -> cache and restart arbiter (run after ANY edit) |
| `tests/test_arbiter_states.py` | Hypothesis stateful + unit tests for state machine |
| `claude-tmux.tmux` | TPM entry point |

## Cognitive Persistence

claude-tmux persists Claude session IDs across reboots so tmux-resurrect can resume conversations,
not just restart processes.

### How It Works

```
SessionStart -> hook.sh reads stdin JSON -> save_session_state() writes .state file
                                          ~/.local/share/tmux/claude-sessions/<session:window.pane>.state

prefix+Ctrl-s -> claude-save-sessions.sh consolidates .state files -> session-map.tsv
                (also prunes state files older than 7 days)

Reboot -> resurrect restores panes -> claude-restore.sh reads .state file
       -> exec claude --resume <session_id>  (falls back to --continue, then fresh)
```

### State File Format

```
SESSION_ID=85cdb12e-3d19-42b6-a7b5-b49a8e26070c
TMUX_SESSION=legion
WINDOW=4
PANE=1
TIMESTAMP=1742944800
```

Key: `session_name:window_index.pane_index` — stable across reboots as long as
window positions don't change (resurrect restores in order).

### tmux-resurrect Config

```bash
# In your tmux config (adjust to your clone path):
set -g @resurrect-processes '"~claude->~/claude-tmux/scripts/claude-restore.sh"'
set -g @resurrect-hook-post-save-layout '~/claude-tmux/scripts/claude-save-sessions.sh'
```

### Important Notes

- `SessionEnd` does NOT clear state files — they must survive for post-reboot restore
- `clear_session_state()` exists for intentional session close (not currently called automatically)
- State files accumulate; `claude-save-sessions.sh` prunes files older than 7 days
- State files are parsed with `grep/cut`, never `source` (injection safety)
- **The arbiter does NOT write resurrect state files** — that remains hook.sh's responsibility
  (fast path still calls `save_session_state()` for `SessionStart` events)

## Cache Sync Protocol

**CRITICAL: Claude Code runs hooks from the CACHE, not the live source directory.**

`CLAUDE_PLUGIN_ROOT` resolves to the versioned cache path at runtime:
`~/.claude/plugins/cache/<marketplace>/claude-tmux/<version>/`

This means: **edits to source files are invisible until synced to cache.**

After any edit to runtime files, either run `bash scripts/sync-cache.sh` (copies
source to cache and restarts the arbiter) or bump the version and reinstall:

```bash
claude plugin marketplace update claude-tmux
claude plugin install claude-tmux@claude-tmux
```

**Never `rsync --delete` the cache top-level.** Claude Code uses versioned
subdirectories. Running sessions reference their version's path via
`CLAUDE_PLUGIN_ROOT`.

### Resumed Sessions

Resumed sessions (`claude --resume`) **do fire all registered hooks**. What is
frozen is `CLAUDE_PLUGIN_ROOT`, which resolves to the **cache version installed
at original session creation**. This means:

- **Hooks fire normally** on resume
- **Plugin version is frozen** per session — after an upgrade, resumed sessions
  run the old hook scripts until a fresh session starts
- **New plugins** installed after session creation are NOT loaded
- **Only a fresh session** loads current plugin versions and cache paths

**Version mismatch is the real problem:** after a plugin upgrade, resumed
sessions run old hook scripts that may use stale protocols, paths, or data
formats. The reconciler compensates with CPU/child detection for basic
working/idle, but advanced states (agents, asking, unread) require current hooks.

## Testing

### Automated Tests (Hypothesis + pytest)

```bash
uv run --with pytest --with 'hypothesis>=6.150' pytest tests/ -v   # arbiter state machine
bun test                                                           # web topology
bash tests/test_tombstone_gc.sh && bash tests/test_claude_restore.sh
```

Coverage includes the arbiter state machine and randomized event sequences,
native collectors, event identity, real private tmux servers, bounded hook
transport, web topology, and sandboxed shell suites for tombstone GC and
session restore. Optional host-data tests skip when their data is absent.

### Manual Testing

All manual test commands read from `theme.json` — no hardcoded values.

```bash
T=theme.json

# Set a specific state (reads color + glyph from theme.json)
for state in working idle unread agents asking error; do
  color=$(jq -r --arg s "$state" '.states[$s].semantic as $sem | .semantic_colors[$sem]' "$T")
  glyph=$(jq -r --arg s "$state" '.states[$s].glyph' "$T")
  echo "$state: $glyph $color"
  # To apply to current pane:
  # tmux set -p @claude_state "$glyph" && tmux set -p @claude_color "$color"
done

# Apply a state to current pane
state=working
color=$(jq -r --arg s "$state" '.states[$s].semantic as $sem | .semantic_colors[$sem]' "$T")
glyph=$(jq -r --arg s "$state" '.states[$s].glyph' "$T")
tmux set -p @claude_state "$glyph" && tmux set -p @claude_color "$color"

# Clear
tmux set -pu @claude_state && tmux set -pu @claude_color && tmux set -pu @claude_persona
```

## Troubleshooting

### Indicators not showing

1. Check arbiter running: `uv run scripts/tmux-arbiter.py --check`
2. Check format strings injected: `tmux show-option -gv window-status-format | grep @claude_state`
3. Force re-inject: `bash scripts/claude-tmux-setup.sh`

### Wrong color / stuck state

1. Check event history: `uv run scripts/tmux-arbiter.py --check` — look at per-pane event log
2. Resumed sessions only get working/idle (no purple/yellow) — CPU detection limitation
3. Force reconcile: wait 3s. Nuclear reset: `rm ~/.claude/local/tmux/pane-states.json && systemctl --user restart claude-tmux-arbiter`

### Indicators disappear after chezmoi apply

Run `tmux source-file ~/.config/tmux/tmux.conf` — loads `plugins-dynamic.conf` which restores format strings.

### Green when idle / Blue when working

- Green-when-idle: stale-working from Ctrl+C (Stop never fires). Auto-clears after 2 minutes.
- Blue-when-working (resumed session): CPU detection runs every 3s. Pure API wait (no CPU, no children) is a fundamental limitation.

### Tuning timing

Edit `config.json`, then `kill -HUP $(cat ~/.claude/local/tmux/arbiter.pid)`. No restart needed.

## Known Limitations

1. **Resumed sessions cannot show purple/yellow/red.** CPU/child detection only gives working/idle.
2. **API wait detection is imperfect.** Pure "thinking" (no CPU, no children) appears idle.
3. **Stop never fires on Ctrl+C.** Stale-working timeout (configurable, default 2min) is the only remedy.
4. **Issue #10373** (SessionStart output discarded for new sessions). Workaround: `/clear`.
5. **Issue #34713** (false hook error labels). No fix available.
