#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""tmux-arbiter — Claude Code indicator state machine for Legion's tmux environment.

Owns @claude_state / @claude_color / @claude_persona / @claude_hook pane options.
Hooks forward bounded events via Unix socket. Native process/session evidence
binds those events before the arbiter updates its cache and tmux projection.

Architecture:
    hook.sh (bash, fire-and-forget) → arbiter.sock → TmuxArbiter (Python)
        + process-bound native lifecycle → pane-states.json (derived cache)
        → tmux set -p @claude_state / @claude_color / @claude_persona / @claude_hook (view)

Native reconciliation runs every second. Projection repair runs at the
configured interval (3s by default); busy hook precedence lasts at most 60s.

Startup guard: suppresses working-state events within 5s of SessionStart
to eliminate the green-on-init false positive from PreToolUse during init.

Persona is sticky: preserved across events that don't carry agent_type.
Only updated when a new agent_type arrives in an event payload.

Never touches @claude_voice — that option is owned by voice_arbiter.py.

@claude_hook shows the last hook event emoji (two-slot grammar from emoji_flat.json).

Usage:
    uv run scripts/tmux-arbiter.py              # start daemon
    uv run scripts/tmux-arbiter.py --check      # status check
    uv run scripts/tmux-arbiter.py --stop       # graceful stop
"""
from __future__ import annotations

import asyncio
import json
import math
import os
import signal
import subprocess
import re
import shlex
import sys
import time
from dataclasses import asdict, dataclass, field, fields, replace
from datetime import datetime as _dt, timezone as _tz
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))
from event_identity import MAX_EVENT_BYTES, process_is_current, rejection_reason

# ---------------------------------------------------------------------------
# Path constants
# ---------------------------------------------------------------------------

TMUX_DIR       = Path("~/.claude/local/tmux").expanduser()
ARBITER_SOCKET = TMUX_DIR / "arbiter.sock"
ARBITER_PID    = TMUX_DIR / "arbiter.pid"
ARBITER_LOG    = TMUX_DIR / "arbiter.log"
STATE_PATH     = TMUX_DIR / "pane-states.json"

# Theme resolution: $CLAUDE_TMUX_THEME, then the machine-local override,
# then the theme shipped with the plugin (same order as lib/common.sh).
_PLUGIN_THEME = Path(__file__).resolve().parent.parent / "theme.json"
_LOCAL_THEME = Path(
    os.environ.get("CLAUDE_TMUX_THEME", "")
    or Path.home() / ".claude" / "local" / "tmux" / "theme.json"
)
THEME_PATH = _LOCAL_THEME if _LOCAL_THEME.is_file() else _PLUGIN_THEME

# Config: plugin defaults at ../config.json, machine-local overrides at
# ~/.claude/local/tmux/config.json (local wins, key by key). Personal values
# (e.g. shared_vault_person) belong in the local file, not the repo.
CONFIG_PATH = Path(__file__).resolve().parent.parent / "config.json"
LOCAL_CONFIG_PATH = Path.home() / ".claude" / "local" / "tmux" / "config.json"

# ---------------------------------------------------------------------------
# tmux socket resolution
# ---------------------------------------------------------------------------
# A bare `tmux` client takes its socket from $TMUX and, when that is unset,
# falls back to the socket named "default". Hook scripts run inside a pane and
# inherit a correct $TMUX, so they were never affected. This daemon is started
# by systemd with a bare environment. When the sessions moved onto
# `tmux -L persist` (tmux-persist.service, added so tmux survives a compositor
# crash), every call here began connecting to /tmp/tmux-$UID/default — a server
# that does not exist. Nothing raised: the option writes failed, pane_exists()
# reported every live pane as dead, and the tab indicators silently went blank
# while the log still showed correctly-computed emoji. Resolve it explicitly.

def _resolve_tmux_socket() -> tuple[str, str]:
    """Pick which tmux server to talk to. Returns (socket_name, reason).

    An empty socket name means "invoke tmux bare and let it decide".
    """
    override = os.environ.get("CLAUDE_TMUX_SOCKET", "").strip()
    if override:
        return override, "CLAUDE_TMUX_SOCKET"

    # Launched inside tmux — inherit that server. $TMUX is "<path>,<pid>,<session>".
    tmux_env = os.environ.get("TMUX", "").strip()
    if tmux_env:
        return tmux_env.rsplit(",", 2)[0], "$TMUX"

    # Daemon case: no ambient tmux. Ask the filesystem which servers exist.
    sock_dir = Path(os.environ.get("TMUX_TMPDIR") or "/tmp") / f"tmux-{os.getuid()}"
    try:
        live = sorted(p.name for p in sock_dir.iterdir() if p.is_socket())
    except OSError:
        live = []
    if len(live) == 1:
        return live[0], f"only socket in {sock_dir}"
    for preferred in ("persist", "default"):
        if preferred in live:
            return preferred, f"preferred among {live}"
    return "", f"unresolved (found {live or 'no sockets'})"


TMUX_SOCKET, TMUX_SOCKET_REASON = _resolve_tmux_socket()
TMUX_CMD: list[str] = ["tmux"] + (
    ["-S" if Path(TMUX_SOCKET).is_absolute() else "-L", TMUX_SOCKET] if TMUX_SOCKET else []
)


class TmuxUnreachable(RuntimeError):
    """The tmux server could not be contacted.

    Distinct from "tmux answered and said no". Reconciliation must treat this as
    "I don't know" and change nothing, never as "every pane is gone".
    """


@dataclass
class Config:
    """Tuning constants for the arbiter. Loaded from config.json, hot-reloaded on SIGHUP."""
    startup_guard_secs: float = 2.0
    stale_working_mins: float = 2.0
    # stale_unread_mins removed — unread persists until explicit PaneRead
    reconcile_interval: float = 3.0
    holdoff_secs: float = 6.0
    hookless_threshold: float = 15.0
    hook_priority_secs: float = 60.0
    cpu_sample_window: float = 0.1
    cpu_activity_threshold: int = 1
    max_log_bytes: int = 1_000_000
    max_event_history: int = 20
    focus_stale_secs: float = 1800.0
    focus_decay_half_life_file: float = 120.0   # seconds — file path signal half-life
    focus_decay_half_life_cwd: float = 600.0    # seconds — cwd signal half-life
    focus_decay_half_life_intent: float = 300.0  # seconds — agent_type intent half-life
    hook_emoji_slots: int = 2  # 1 = refinement-when-available-else-event, 2 = event+refinement
    shared_vault_path: str = ""    # e.g. "~/Documents/Notes/Shared" — empty disables
    shared_vault_person: str = ""  # person slug attributed to shared-vault file paths

    @classmethod
    def load(cls) -> "Config":
        """Load from config.json, filtering to known fields. Returns defaults on error."""
        try:
            raw: dict = {}
            for path in (CONFIG_PATH, LOCAL_CONFIG_PATH):
                if path.exists():
                    raw.update(json.loads(path.read_text()))
            known = {f.name for f in fields(cls)}
            filtered = {k: v for k, v in raw.items() if k in known}
            return cls(**filtered)
        except Exception as e:
            _log(f"config load failed: {e} — using defaults")
        return cls()

# Legacy module-level aliases — used by _log(), standalone detection functions,
# and any code outside the TmuxArbiter class.
_DEFAULT_CONFIG = Config()
STARTUP_GUARD_SECS = _DEFAULT_CONFIG.startup_guard_secs
STALE_WORKING_MINS = _DEFAULT_CONFIG.stale_working_mins
RECONCILE_INTERVAL = _DEFAULT_CONFIG.reconcile_interval
MAX_LOG_BYTES      = _DEFAULT_CONFIG.max_log_bytes
FOCUS_STATE_FILE   = Path.home() / ".claude" / "local" / "voice" / "focus-state"

# Stream capture
STREAM_HANDLER     = Path(__file__).resolve().parent / "stream-handler.py"
STREAM_DIR         = Path("~/.claude/local/tmux-stream").expanduser()

# ---------------------------------------------------------------------------
# EmojiLookup — read-only emoji_flat.json cache
# ---------------------------------------------------------------------------

EMOJI_FLAT_PATH = Path("~/.claude/local/emoji/emoji_flat.json").expanduser()


def _emoji_wcwidth(s: str) -> int:
    """Calculate display width of a string, skipping variation selectors and ZWJ.

    Uses unicodedata.east_asian_width() — available in stdlib, no external dep.
    EAW 'W' or 'F' → 2 columns, everything else → 1 column.
    Variation selectors and ZWJ are zero-width.
    """
    import unicodedata
    width = 0
    for ch in s:
        cp = ord(ch)
        if cp in (0xFE0E, 0xFE0F, 0x200D):
            continue  # zero-width
        eaw = unicodedata.east_asian_width(ch)
        if eaw in ("W", "F"):
            width += 2
        else:
            width += 1
    return width


class EmojiLookup:
    """Read-only emoji_flat.json cache with auto-reload on mtime change.

    Used to resolve hook event types and tool names to display emoji.
    Gracefully degrades: missing file or corrupt JSON → empty fallback.

    Two retrieval modes:
      get()      — strips VS16, returns raw width (1 or 2 columns)
      get_tmux() — strips VS16 + pads to exactly 2 columns for consistent spacing
    """

    def __init__(self, path: str | None = None) -> None:
        self.path = Path(path) if path else EMOJI_FLAT_PATH
        self._cache: dict[str, str] = {}
        self._mtime: float = 0.0

    def get(self, key: str, fallback: str = "") -> str:
        """Look up emoji by key. Strips VS16. Returns raw width (may be 1 or 2 col).

        Use get_tmux() instead when writing to tmux options for consistent spacing.
        """
        self._maybe_reload()
        val = self._cache.get(key, fallback)
        return val.replace("\uFE0F", "") if val else val

    def get_tmux(self, key: str, fallback: str = "", target_width: int = 2) -> str:
        """Look up emoji and normalize to exactly target_width columns for tmux.

        Strips VS16, then pads with spaces if the stripped emoji is narrower than
        target_width. This ensures ALL emoji occupy the same number of columns
        in tmux status bars — no overlap, no inconsistent spacing.

        Text-default emoji (⚔ 🕸 🖥 etc.) are 1 col after VS16 strip → padded to 2.
        Emoji-default emoji (🔧 🎤 🌟 etc.) are already 2 col → returned as-is.
        Empty/fallback → returned as-is (no padding on empty).
        """
        val = self.get(key, fallback)
        if not val:
            return val
        w = _emoji_wcwidth(val)
        if w < target_width:
            val = val + " " * (target_width - w)
        return val

    def _maybe_reload(self) -> None:
        """Reload from disk if mtime changed. Never raises."""
        try:
            mt = self.path.stat().st_mtime
            if mt != self._mtime:
                with open(self.path) as f:
                    self._cache = json.load(f)
                self._mtime = mt
        except (OSError, json.JSONDecodeError, ValueError):
            pass  # keep existing cache on error


VENTURES_PATH = Path("~/.claude/local/emoji/ventures.json").expanduser()
ALIASES_PATH = Path("~/.claude/local/emoji/aliases.json").expanduser()
VENTURES_DIR = Path("~/.claude/local/ventures/active").expanduser()

# Domain fallback paths — well-known directories when no plugin/venture match.
# More specific prefixes first. Checked only as last resort.
_DOMAIN_PATHS: list[tuple[str, str]] = [
    (str(Path("~/Documents").expanduser()), "domain:documents"),
    (str(Path("~/docs").expanduser()), "domain:documents"),
    (str(Path("~/.config").expanduser()), "domain:config"),
    (str(Path("~/.claude/image-cache").expanduser()), "domain:config"),
    (str(Path("~/.claude/projects").expanduser()), "domain:config"),
    (str(Path("~/Workspace").expanduser()), "domain:workspace"),
]


@dataclass
class FocusSignal:
    """A timestamped focus signal with source-dependent weight."""
    key: str            # "venture:legion" | "plugin:claude-voice"
    weight: float       # base weight before decay
    timestamp: float    # time.time() when observed
    source: str         # "window_name" | "file_path" | "cwd" | "explicit"


@dataclass
class FocusResult:
    """Result of focus resolution — the best 1-2 emoji pair."""
    slot1: str = ""      # primary signal key (usually venture)
    slot2: str = ""      # secondary signal key (person > action > plugin)
    emoji: str = ""      # composed emoji string (1-2 emoji, width-normalized)


class IntentClassifier:
    """Maps agent_type and skill_name to action:* intent keys.

    Uses suffix-based pattern matching on agent_type and direct lookup on skill_name.
    Returns empty string when no confident classification is possible.
    """

    _AGENT_SUFFIX_MAP: dict[str, str] = {
        "code-reviewer": "action:review",
        "code-explorer": "action:research",
        "code-architect": "action:plan",
        "code-simplifier": "action:refactor",
        "researcher": "action:research",
        "taskmaster": "action:plan",
        "scribe": "action:write",
        "archivist": "action:research",
        "chronicler": "action:write",
        "harvester": "action:ops",
        "optimizer": "action:refactor",
        "navigator": "action:plan",
        "analyst": "action:research",
        "transcriber": "action:ops",
        "narrator": "action:write",
        "oneirologist": "action:ops",
        "archaeologist": "action:research",
        "janitor": "action:ops",
        "fantasia": "action:write",
        "categorizer": "action:ops",
        "quartermaster": "action:ops",
        "witness": "action:research",
        "memory-architect": "action:plan",
        "dashboard": "action:ops",
    }

    _AGENT_PREFIX_MAP: dict[str, str] = {
        "feature-dev": "action:build",
        "pr-review-toolkit": "action:review",
        "plugin-dev": "action:build",
        "code-simplifier": "action:refactor",
    }

    _AGENT_SKIP = frozenset({"general-purpose", "Explore", "Plan"})

    _SKILL_MAP: dict[str, str] = {
        "brainstorming": "action:plan",
        "writing-plans": "action:plan",
        "subagent-driven-development": "action:build",
        "executing-plans": "action:build",
        "test-driven-development": "action:build",
        "systematic-debugging": "action:fix",
        "requesting-code-review": "action:review",
        "receiving-code-review": "action:review",
        "finishing-a-development-branch": "action:ops",
        "verification-before-completion": "action:review",
        "using-git-worktrees": "action:ops",
        "writing-skills": "action:build",
        "checkpoint": "action:ops",
    }

    def from_agent_type(self, agent_type: str) -> str:
        """Classify intent from SubagentStart agent_type."""
        if not agent_type or agent_type in self._AGENT_SKIP:
            return ""
        suffix = agent_type.split(":")[-1] if ":" in agent_type else agent_type
        if suffix in self._AGENT_SUFFIX_MAP:
            return self._AGENT_SUFFIX_MAP[suffix]
        prefix = agent_type.split(":")[0] if ":" in agent_type else ""
        if prefix in self._AGENT_PREFIX_MAP:
            return self._AGENT_PREFIX_MAP[prefix]
        return ""

    def from_skill_name(self, skill_name: str) -> str:
        """Classify intent from Skill invocation name."""
        if not skill_name:
            return ""
        if skill_name in self._SKILL_MAP:
            return self._SKILL_MAP[skill_name]
        if ":" in skill_name:
            bare = skill_name.split(":")[-1]
            if bare in self._SKILL_MAP:
                return self._SKILL_MAP[bare]
        return ""


class FocusEngine:
    """Two-slot focus composition engine.

    Tracks venture (context) and plugin (activity) signals independently.
    Composes them into 1-2 emoji for @claude_focus using grammar rules:

      Rule 1 (Single): Only one signal present → single emoji
      Rule 2 (Compound): Different venture + plugin → venture_emoji + plugin_emoji
      Rule 3 (Collapse): Same context (e.g. legion venture + legion plugin) → plugin emoji only

    All resolve methods return (venture_key, plugin_key) tuples.
    compose() applies the grammar to produce the final emoji string.
    All emoji output uses get_tmux() for consistent 2-column width.
    """

    DECAY_HALF_LIFE_FILE = 120.0    # file path activity: focus drifts in ~5 min
    DECAY_HALF_LIFE_CWD = 600.0     # cwd baseline: slow fade over ~25 min
    DECAY_HALF_LIFE_INTENT = 300.0  # agent_type intent: 5 min half-life
    _NO_DECAY_SOURCES = frozenset({"window_name", "explicit", "skill"})
    DECAY_FLOOR = 0.01  # Minimum decayed weight to consider a signal alive

    # Information priority per signal category (higher = preferred for slot 2)
    _INFO_PRIORITY = {
        "person": 4,    # rarest, most specific when present
        "plugin": 3,    # window identity — "what system is this?"
        "action": 2,    # transient activity type (hook emoji already shows this)
        "domain": 1,    # well-known directory fallback (weakest)
        "venture": 1,   # context (always slot 1 when present)
    }

    _SOURCE_WEIGHTS = {
        "window_name": 10.0,
        "file_path": 5.0,
        "cwd": 2.0,
        "explicit": 100.0,
        "skill": 8.0,
        "agent_type": 8.0,
    }

    def __init__(self, emoji: EmojiLookup) -> None:
        self.emoji = emoji
        self.intent = IntentClassifier()
        self._ventures: dict[str, list[str]] = {}
        self._ventures_mtime: float = 0.0
        self._signals: dict[str, dict[str, FocusSignal]] = {}  # pane_id -> {key -> signal}
        self._aliases_ventures: dict[str, str] = {}
        self._aliases_plugins: dict[str, str] = {}
        self._aliases_ventures_lower: dict[str, str] = {}
        self._aliases_plugins_lower: dict[str, str] = {}
        self._aliases_path: Path = ALIASES_PATH
        self._aliases_mtime: float = 0.0
        self._canonical_ventures: set[str] = set()
        self._ventures_dir: Path = VENTURES_DIR
        self._canonical_mtime: float = 0.0

    def _decayed_weight(self, sig: FocusSignal, now: float | None = None) -> float:
        """Apply exponential decay to a signal's weight.

        Formula: effective = base * 2^(-age / half_life)
        Window name and explicit signals never decay.
        """
        if sig.source in self._NO_DECAY_SOURCES:
            return sig.weight
        if now is None:
            now = time.time()
        age = max(0.0, now - sig.timestamp)
        if age == 0.0:
            return sig.weight
        half_life = (
            self.DECAY_HALF_LIFE_CWD if sig.source == "cwd"
            else self.DECAY_HALF_LIFE_INTENT if sig.source == "agent_type"
            else self.DECAY_HALF_LIFE_FILE
        )
        return sig.weight * (2.0 ** (-age / half_life))

    def clear_pane(self, pane_id: str) -> None:
        """Remove all signals for a pane (called on SessionEnd)."""
        self._signals.pop(pane_id, None)

    def clear_explicit(self, pane_id: str) -> None:
        """Remove only explicit signals for a pane (reverts to auto-detection)."""
        pane_signals = self._signals.get(pane_id)
        if pane_signals:
            dead = [k for k, sig in pane_signals.items() if sig.source == "explicit"]
            for k in dead:
                del pane_signals[k]

    def get_focus_status(self, pane_id: str, now: float | None = None) -> dict:
        """Return structured focus status for a pane."""
        if now is None:
            now = time.time()
        result = self.resolve_focus(pane_id, now=now)
        signals = []
        pane_signals = self._signals.get(pane_id, {})
        for key, sig in pane_signals.items():
            w = self._decayed_weight(sig, now=now)
            signals.append({
                "key": sig.key,
                "source": sig.source,
                "weight": round(sig.weight, 2),
                "decayed_weight": round(w, 4),
                "age_secs": round(now - sig.timestamp, 1),
            })
        signals.sort(key=lambda s: s["decayed_weight"], reverse=True)
        return {
            "slot1": result.slot1,
            "slot2": result.slot2,
            "emoji": result.emoji,
            "signals": signals,
        }

    def prune_stale(self, now: float | None = None) -> int:
        """Remove signals that have decayed below DECAY_FLOOR. Returns count removed."""
        if now is None:
            now = time.time()
        removed = 0
        empty_panes = []
        for pane_id, sigs in self._signals.items():
            dead_keys = [
                k for k, sig in sigs.items()
                if self._decayed_weight(sig, now=now) < self.DECAY_FLOOR
            ]
            for k in dead_keys:
                del sigs[k]
                removed += 1
            if not sigs:
                empty_panes.append(pane_id)
        for pid in empty_panes:
            del self._signals[pid]
        return removed

    def observe(self, source: str, key: str, pane_id: str, now: float | None = None) -> None:
        """Record a focus signal for a pane.

        If the key already exists for this pane, update timestamp.
        If the new source has higher base weight, also update source+weight.
        """
        if now is None:
            now = time.time()
        weight = self._SOURCE_WEIGHTS.get(source, 5.0)
        pane_signals = self._signals.setdefault(pane_id, {})
        existing = pane_signals.get(key)
        if existing is None or weight >= existing.weight:
            pane_signals[key] = FocusSignal(key=key, weight=weight, timestamp=now, source=source)
        elif existing is not None:
            # Lower-weight source still refreshes timestamp
            existing.timestamp = now

    def resolve_focus(self, pane_id: str, now: float | None = None) -> FocusResult:
        """Resolve current focus using N-category ranking.

        Collects best signal per category, picks most informative 1-2 pair.
        Slot 1: venture (when present), else highest info-priority signal.
        Slot 2: highest info-priority remaining signal (person > action > plugin).

        Returns FocusResult with slot1, slot2, emoji.
        """
        if now is None:
            now = time.time()
        pane_signals = self._signals.get(pane_id)
        if not pane_signals:
            return FocusResult()

        # Step 1: Find best signal per category
        best_by_category: dict[str, tuple[float, str]] = {}
        for key, sig in pane_signals.items():
            w = self._decayed_weight(sig, now=now)
            if w < self.DECAY_FLOOR:
                continue
            category = key.split(":")[0] if ":" in key else ""
            if category not in self._INFO_PRIORITY:
                continue
            current = best_by_category.get(category, (0.0, ""))
            if w > current[0]:
                best_by_category[category] = (w, key)

        if not best_by_category:
            return FocusResult()

        # Step 2: Pick slot 1 — venture if present, else highest info-priority
        slot1 = ""
        if "venture" in best_by_category:
            slot1 = best_by_category["venture"][1]

        if not slot1:
            best_cat = max(
                best_by_category.keys(),
                key=lambda c: self._INFO_PRIORITY.get(c, 0),
            )
            slot1 = best_by_category[best_cat][1]
            emoji = self.compose(slot1, "")
            return FocusResult(slot1=slot1, emoji=emoji)

        # Step 3: Pick slot 2 — highest info-priority non-venture
        slot2 = ""
        remaining = {c: v for c, v in best_by_category.items() if c != "venture"}
        if remaining:
            best_cat = max(
                remaining.keys(),
                key=lambda c: self._INFO_PRIORITY.get(c, 0),
            )
            slot2 = remaining[best_cat][1]

        emoji = self.compose(slot1, slot2)
        return FocusResult(slot1=slot1, slot2=slot2, emoji=emoji)

    def compose(self, slot1_key: str, slot2_key: str) -> str:
        """Apply composition grammar to produce focus emoji string.

        Single-emoji output: picks the most specific signal.
        Slot 2 (person > action > plugin > domain) is preferred when present
        because it's always more specific than slot 1 (venture).
        Falls back to slot 1 when slot 2 is empty.

        Returns composed emoji string (0 or 2 columns).
        """
        if not slot1_key and not slot2_key:
            return ""

        slot1_emoji = self.emoji.get_tmux(slot1_key) if slot1_key else ""
        slot2_emoji = self.emoji.get_tmux(slot2_key) if slot2_key else ""

        # Prefer slot 2 (more specific) when available
        if slot2_key and slot2_emoji.strip():
            return slot2_emoji
        return slot1_emoji

    def resolve_window_name(self, window_name: str) -> tuple[str, str]:
        """Extract venture and plugin signals from window name.

        Resolution priority:
          1. Exact plugin match: 'claude-voice' → plugin:claude-voice
          2. Plugin alias: 'voice' → plugin:claude-voice
          3. Exact venture slug in ventures.json: 'acme-corp' → venture:acme-corp
          4. Venture alias: 'acme' or 'AC' → venture:acme-corp
          5. Fuzzy prefix match against ventures.json slugs

        Returns (venture_key, plugin_key).
        """
        if not window_name:
            return ("", "")

        self._load_aliases()
        venture_key = ""
        plugin_key = ""

        # 1. Exact plugin. Codex windows share the claude-* plugin ontology:
        # codex-legion-logging -> plugin:claude-logging.
        plugin_name = window_name
        if window_name.startswith("codex-legion-"):
            plugin_name = "claude-" + window_name.removeprefix("codex-legion-")
        elif window_name.startswith("codex-"):
            plugin_name = "claude-" + window_name.removeprefix("codex-")
        if plugin_name.startswith("claude-"):
            if self.emoji.get(f"plugin:{plugin_name}"):
                plugin_key = f"plugin:{plugin_name}"

        # 2. Plugin alias: short name → claude-{full} (case-insensitive)
        if not plugin_key:
            alias_plugin = (self._aliases_plugins.get(window_name)
                           or self._aliases_plugins_lower.get(window_name.lower()))
            if alias_plugin and self.emoji.get(f"plugin:{alias_plugin}"):
                plugin_key = f"plugin:{alias_plugin}"

        # 3. Exact venture slug in ventures.json (case-insensitive)
        self._maybe_reload()
        if window_name in self._ventures:
            venture_key = f"venture:{window_name}"
        else:
            wn_lower = window_name.lower()
            for slug in self._ventures:
                if slug.lower() == wn_lower:
                    venture_key = f"venture:{slug}"
                    break

        # 4. Venture alias: short/legacy name → canonical slug (case-insensitive)
        if not venture_key:
            alias_venture = (self._aliases_ventures.get(window_name)
                            or self._aliases_ventures_lower.get(window_name.lower()))
            if alias_venture:
                venture_key = f"venture:{alias_venture}"

        # 4.5. Exact match against canonical venture IDs (from markdown files)
        if not venture_key:
            self._load_canonical_ventures()
            if window_name in self._canonical_ventures:
                venture_key = f"venture:{window_name}"
            elif window_name.lower() in self._canonical_ventures:
                venture_key = f"venture:{window_name.lower()}"

        # 5. Fuzzy prefix match against ventures.json slugs
        if not venture_key:
            wn_lower = window_name.lower()
            for slug in self._ventures:
                if slug.startswith(wn_lower) or wn_lower.startswith(slug.split("-")[0]):
                    venture_key = f"venture:{slug}"
                    break

        # claude-* plugin windows are legion context by default
        if plugin_key and not venture_key:
            venture_key = "venture:legion"

        return (venture_key, plugin_key)

    def resolve_file_path(self, file_path: str) -> tuple[str, str]:
        """Extract venture and plugin signals from a file path.

        Returns (venture_key, plugin_key).
        Falls back to domain:* for well-known directories when no plugin match.
        """
        if not file_path:
            return ("", "")

        venture_key = ""
        plugin_key = ""

        plugin = self.extract_plugin(file_path)
        if plugin:
            plugin_key = f"plugin:{plugin}"

        venture = self.extract_venture(file_path)
        if venture:
            venture_key = f"venture:{venture}"

        # Domain fallback — only when no plugin match
        if not plugin_key:
            for prefix, domain_key in _DOMAIN_PATHS:
                if file_path.startswith(prefix):
                    plugin_key = domain_key
                    break

        return (venture_key, plugin_key)

    def resolve_cwd(self, cwd: str) -> tuple[str, str]:
        """Extract venture signal from cwd. Returns (venture_key, "")."""
        if not cwd:
            return ("", "")
        venture = self.extract_venture(cwd)
        if venture:
            return (f"venture:{venture}", "")
        return ("", "")

    def extract_plugin(self, path: str) -> str | None:
        """Auto-detect plugin name from file path. Zero config needed."""
        if "/plugins/" in path:
            parts = path.split("/plugins/")
            if len(parts) > 1:
                name = parts[-1].split("/")[0]
                if name and name != "local" and not name.startswith("."):
                    return name

        local_prefix = str(Path("~/.claude/local/").expanduser())
        if path.startswith(local_prefix):
            remainder = path[len(local_prefix):].lstrip("/")
            name = remainder.split("/")[0]
            if name and not name.startswith("."):
                candidate = f"claude-{name}"
                if self.emoji.get(f"plugin:{candidate}"):
                    return candidate

        return None

    def extract_venture(self, path: str) -> str | None:
        """Match path against ventures.json prefixes."""
        self._maybe_reload()
        for slug, patterns in self._ventures.items():
            for pattern in patterns:
                if path.startswith(pattern):
                    return slug
        return None

    _SYSTEM_VENTURE_SLUGS = frozenset({"legion", "personal"})

    def validate_ventures(self) -> list[str]:
        """Compare ventures.json slugs against canonical venture IDs.
        Returns list of warning strings. Empty = all good."""
        self._maybe_reload()
        self._load_aliases()
        self._load_canonical_ventures()
        warnings = []

        for slug in self._ventures:
            if slug in self._SYSTEM_VENTURE_SLUGS:
                continue
            if slug not in self._canonical_ventures:
                resolved = self._aliases_ventures.get(slug)
                if resolved and resolved in self._canonical_ventures:
                    continue
                warnings.append(
                    f"ventures.json slug '{slug}' not found in active ventures "
                    f"(~/.claude/local/ventures/active/{slug}.md missing)"
                )

        for vid in self._canonical_ventures:
            if vid not in self._ventures and vid not in self._SYSTEM_VENTURE_SLUGS:
                if vid not in [self._aliases_ventures.get(k) for k in self._ventures]:
                    warnings.append(
                        f"active venture '{vid}' has no path mapping in ventures.json"
                    )

        return warnings

    def _load_canonical_ventures(self) -> None:
        """Load canonical venture IDs from active venture markdown filenames.
        Venture ID = filename without .md extension. Cached by dir mtime."""
        try:
            mt = self._ventures_dir.stat().st_mtime
            if mt != self._canonical_mtime:
                self._canonical_ventures = {
                    f.stem for f in self._ventures_dir.glob("*.md")
                    if f.is_file() and not f.name.startswith(".")
                }
                self._canonical_mtime = mt
        except OSError:
            if not self._canonical_ventures:
                self._canonical_ventures = set()

    def _load_aliases(self) -> None:
        """Load aliases.json with mtime cache. Graceful on missing/broken file."""
        try:
            mt = self._aliases_path.stat().st_mtime
            if mt != self._aliases_mtime:
                with open(self._aliases_path) as f:
                    raw = json.load(f)
                self._aliases_ventures = raw.get("ventures", {})
                self._aliases_plugins = raw.get("plugins", {})
                # Case-insensitive lookup (lowercase keys → original values)
                self._aliases_ventures_lower = {k.lower(): v for k, v in self._aliases_ventures.items()}
                self._aliases_plugins_lower = {k.lower(): v for k, v in self._aliases_plugins.items()}
                self._aliases_mtime = mt
        except (OSError, json.JSONDecodeError, ValueError):
            if not self._aliases_ventures and not self._aliases_plugins:
                self._aliases_ventures = {}
                self._aliases_plugins = {}
                self._aliases_ventures_lower = {}
                self._aliases_plugins_lower = {}

    def _maybe_reload(self) -> None:
        """Reload ventures.json on mtime change."""
        try:
            mt = VENTURES_PATH.stat().st_mtime
            if mt != self._ventures_mtime:
                with open(VENTURES_PATH) as f:
                    self._ventures = json.load(f)
                self._ventures_mtime = mt
        except (OSError, json.JSONDecodeError):
            pass


def hook_to_event_key(hook_type: str) -> str:
    """CamelCase hook type → emoji DB key.

    Examples:
        'PreToolUse' → 'event:pre-tool-use'
        'SessionStart' → 'event:session-start'
        'Stop' → 'event:stop'
    """
    kebab = re.sub(r'(?<!^)(?=[A-Z])', '-', hook_type).lower()
    return f"event:{kebab}"


# Refinement context map — which hooks carry extra context and how to resolve it.
_REFINEMENT_MAP: dict[str, tuple[str, str]] = {
    "PreToolUse":         ("tool_name",         "tool"),
    "PostToolUse":        ("tool_name",         "tool"),
    "PostToolUseFailure": ("tool_name",         "tool"),
    "PermissionRequest":  ("tool_name",         "tool"),
    "Notification":       ("notification_type", "notify"),
    "SubagentStart":      ("agent_type",        "persona"),
    "SubagentStop":       ("agent_type",        "persona"),
    "SessionStart":       ("source",            "source"),
    "SessionEnd":         ("reason",            "reason"),
    "Stop":               ("source",            "source"),
}


def refinement_key(hook_type: str, event_data: dict) -> str | None:
    """Extract optional refinement key from hook event data.

    Returns an emoji DB key like 'tool:read' or 'persona:matt',
    or None if the hook type has no refinement or the field is empty.
    """
    spec = _REFINEMENT_MAP.get(hook_type)
    if not spec:
        return None
    field_name, category = spec
    value = event_data.get(field_name, "")
    if not value:
        return None
    # Handle compound values like 'claude-personas:matt' → 'matt'
    if ":" in value and category == "persona":
        value = value.split(":")[-1]
    return f"{category}:{value.lower()}"


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def _log(msg: str) -> None:
    ts = time.strftime("%Y-%m-%dT%H:%M:%S")
    line = f"{ts} [tmux-arbiter] {msg}\n"
    try:
        ARBITER_LOG.parent.mkdir(parents=True, exist_ok=True)
        if ARBITER_LOG.exists() and ARBITER_LOG.stat().st_size > MAX_LOG_BYTES:
            ARBITER_LOG.rename(ARBITER_LOG.with_suffix(".log.1"))
        with open(ARBITER_LOG, "a") as f:
            f.write(line)
    except OSError:
        pass
    sys.stderr.write(line)


# ---------------------------------------------------------------------------
# Data types
# CPU activity detection — threshold in jiffies per sample window.
# A claude process at idle consumes 0 jiffies in 200ms.
# A working claude process consumes 3-10+ jiffies in 200ms.
CPU_ACTIVITY_THRESHOLD = 1
CPU_SAMPLE_WINDOW = 0.1  # seconds between samples


def detect_pane_cpu_active(pane_id: str) -> bool | None:
    """Detect if a claude process in a pane is actively using CPU.

    Samples /proc/<pid>/stat twice with a 200ms gap. If CPU time delta
    exceeds threshold, the process is working. Returns None if detection
    fails (pane doesn't exist, can't find PID, etc.).
    """
    try:
        pid_r = subprocess.run(
            [*TMUX_CMD, "display-message", "-p", "-t", pane_id, "#{pane_pid}"],
            capture_output=True, timeout=0.5,
        )
        if pid_r.returncode != 0:
            return None
        shell_pid = pid_r.stdout.decode().strip()
        if not shell_pid:
            return None
        # Find the claude child process
        children = subprocess.run(
            ["ps", "--ppid", shell_pid, "-o", "pid", "--no-headers"],
            capture_output=True, timeout=0.5,
        )
        claude_pid = children.stdout.decode().strip().split("\n")[0].strip()
        if not claude_pid:
            return None
        stat_path = f"/proc/{claude_pid}/stat"
        # Sample 1
        with open(stat_path) as f:
            fields = f.read().split()
        t1 = int(fields[13]) + int(fields[14])  # utime + stime
        time.sleep(CPU_SAMPLE_WINDOW)
        # Sample 2
        with open(stat_path) as f:
            fields = f.read().split()
        t2 = int(fields[13]) + int(fields[14])
        return (t2 - t1) > CPU_ACTIVITY_THRESHOLD
    except Exception:
        return None


def detect_tool_execution(pane_id: str) -> bool | None:
    """Detect if a claude pane is executing a tool by checking for recent child processes.

    MCP servers have ages of hours (persistent). Tool-spawned shells have ages of
    seconds (ephemeral). If any grandchild (child of claude, not of shell) is < 10
    seconds old, a tool is executing. Returns None if detection fails.
    """
    try:
        pid_r = subprocess.run(
            [*TMUX_CMD, "display-message", "-p", "-t", pane_id, "#{pane_pid}"],
            capture_output=True, timeout=0.5,
        )
        if pid_r.returncode != 0:
            return None
        shell_pid = pid_r.stdout.decode().strip()
        if not shell_pid:
            return None
        # Get claude PID (direct child of shell)
        children_r = subprocess.run(
            ["ps", "--ppid", shell_pid, "-o", "pid", "--no-headers"],
            capture_output=True, timeout=0.5,
        )
        claude_pid = children_r.stdout.decode().strip().split("\n")[0].strip()
        if not claude_pid:
            return None
        # Get claude's children with elapsed time
        grandchildren = subprocess.run(
            ["ps", "--ppid", claude_pid, "-o", "etimes", "--no-headers"],
            capture_output=True, timeout=0.5,
        )
        for line in grandchildren.stdout.decode().splitlines():
            line = line.strip()
            if not line:
                continue
            age_seconds = int(line)
            if age_seconds < 10:
                return True  # Recent child = tool executing
        return False
    except Exception:
        return None


# ---------------------------------------------------------------------------


def detect_pane_persona(pane_id: str) -> str:
    """Detect persona key from --agent flag in the Claude process running in a pane.

    Walks pane_pid → child processes looking for:
        claude --agent claude-personas:<key>

    Returns the persona key (e.g. "matt", "legion") or "" if not found.
    This is used during absorption and Phase 1.5 restoration to auto-populate
    persona without requiring a SubagentStart event to have fired.
    """
    try:
        pid_r = subprocess.run(
            [*TMUX_CMD, "display-message", "-p", "-t", pane_id, "#{pane_pid}"],
            capture_output=True, timeout=0.5,
        )
        if pid_r.returncode != 0:
            return ""
        shell_pid = pid_r.stdout.decode().strip()
        if not shell_pid:
            return ""
        children = subprocess.run(
            ["ps", "--ppid", shell_pid, "-o", "args", "--no-headers"],
            capture_output=True, timeout=0.5,
        )
        for line in children.stdout.decode().splitlines():
            m = re.search(r"--agent\s+claude-personas:(\S+)", line)
            if m:
                return m.group(1)
    except Exception:
        pass
    return ""


# ---------------------------------------------------------------------------

@dataclass
class PaneState:
    """Complete indicator state for one tmux pane."""
    pane_id: str             # e.g. "%42" — tmux's permanent pane identifier
    state_name: str          # "working"|"idle"|"agents"|"asking"|"error"|"nudge"|"clear"
    color: str               # resolved hex color e.g. "#a6e3a1"
    glyph: str               # resolved glyph e.g. "⬤"
    persona: str             # emoji string e.g. "🪷" or "" if not set
    updated_at: float        # unix timestamp of last state transition
    session_start_at: float  # unix timestamp of last SessionStart (for startup guard)
                             # 0.0 = never received SessionStart (safe sentinel)
    subagent_depth: int = 0  # count of unmatched SubagentStart events.
                             # > 0 means a subagent is still running.
                             # Used to return to agents state after Stop (e.g. permission dialog).
    last_working_at: float = 0.0  # last time working was detected (for hysteresis holdoff)
    last_source: str = ""         # "hook" or "reconcile" — who set the current state
    event_history: list = field(default_factory=list)  # last 20 events for forensic debugging
    hook_emoji: str = ""  # last hook event emoji (slot1 + optional slot2) for @claude_hook
    focus_emoji: str = ""  # composed focus string for @claude_focus (1-2 emoji)
    focus_venture: str = ""  # venture signal key (e.g. "venture:legion") or ""
    focus_plugin: str = ""   # plugin signal key (e.g. "plugin:claude-voice") or ""
    window_name: str = ""    # cached tmux window name (queried on SessionStart)
    focus_signals: list = field(default_factory=list)  # list of FocusSignal dicts for persistence
    provider: str = "claude"  # event adapter that owns lifecycle semantics
    active_agent_count: int = 0  # Codex subagents are an orthogonal decoration
    lifecycle_source: str = ""  # process-bound native lifecycle reader
    lifecycle_identity: str = ""  # PID/start/session, protects pane reuse
    lifecycle_unread: bool = False  # survives temporary native-reader failure


# ---------------------------------------------------------------------------
# ThemeLoader
# ---------------------------------------------------------------------------

class ThemeLoader:
    """Loads and caches theme.json. Hot-reloads when mtime changes."""

    def __init__(self, path: Path = THEME_PATH) -> None:
        self.path = path
        self._data: dict = {}
        self._mtime: float = 0.0
        self._load()

    def _load(self) -> None:
        """Load theme.json if it has changed since last load."""
        try:
            mtime = self.path.stat().st_mtime
            if mtime != self._mtime:
                self._data = json.loads(self.path.read_text())
                self._mtime = mtime
                _log(f"theme loaded: {self.path.name}")
        except Exception as e:
            _log(f"theme load failed: {e}")
            # Keep existing data on failure — don't clobber a working cache

    def resolve_event(self, event: str) -> tuple[str, str, str]:
        """Map hook event name → (state_name, color, glyph).

        "clear" is a special action state (SessionEnd) — no color or glyph.
        Returns ("error", "#f38ba8", "!") if event unknown or theme broken.
        """
        self._load()
        try:
            state_name = self._data["hook_to_state"].get(event, "error")
            if state_name == "clear":
                return ("clear", "", "")  # SessionEnd — clear indicators, no visuals
            return self._resolve_state(state_name)
        except (KeyError, TypeError):
            return ("error", "#f38ba8", "!")

    def resolve_notification(self, notification_type: str) -> tuple[str, str, str]:
        """Map notification_type → (state_name, color, glyph).

        Falls back to "nudge" if type unknown.
        """
        self._load()
        try:
            state_name = (
                self._data.get("notification_states", {})
                .get(notification_type, "nudge")
            )
            return self._resolve_state(state_name)
        except (KeyError, TypeError):
            return self._resolve_state("nudge")

    def resolve_state(self, state_name: str) -> tuple[str, str, str]:
        """Resolve state name → (state_name, color, glyph). Public API."""
        self._load()
        return self._resolve_state(state_name)

    def _resolve_state(self, state_name: str) -> tuple[str, str, str]:
        """Resolve state name → (state_name, color, glyph)."""
        try:
            state = self._data["states"][state_name]
            semantic = state["semantic"]
            color = self._data["semantic_colors"][semantic]
            glyph = state["glyph"]
            return (state_name, color, glyph)
        except (KeyError, TypeError):
            return ("error", "#f38ba8", "!")

    def get_persona_emoji(self, persona_key: str, emoji_lookup: "EmojiLookup | None" = None) -> str:
        """Look up persona emoji, normalized to 2 columns for tmux.

        Primary: emoji_flat.json via get_tmux(). Fallback: theme.json.
        """
        # Primary: emoji_flat.json (canonical source, width-normalized)
        if emoji_lookup:
            val = emoji_lookup.get_tmux(f"persona:{persona_key}")
            if val.strip():  # get_tmux may return padded spaces
                return val
        # Fallback: theme.json (not width-normalized — legacy path)
        self._load()
        try:
            return self._data["personas"].get(persona_key, {}).get("emoji", "")
        except (KeyError, TypeError):
            return ""


# ---------------------------------------------------------------------------
# TmuxWriter
# ---------------------------------------------------------------------------

class TmuxWriter:
    """Project indicator changes in one batch. Failures are logged, never raised."""

    # Active work must remain visible even when the selected sibling is idle.
    _PRIORITY = {"working": 70, "agents": 60, "asking": 50, "error": 40,
                 "unread": 30, "unknown": 0, "nudge": 20, "idle": 10}
    _FIELDS = ("state_name", "state", "color", "persona", "hook", "focus", "agents")
    _theme = ThemeLoader()

    @staticmethod
    def _run(cmd: list[str]) -> bool:
        """Run a tmux command. Returns True on success."""
        try:
            result = subprocess.run(cmd, capture_output=True, timeout=0.3)
            if result.returncode != 0:
                _log(f"tmux cmd failed: {' '.join(cmd[:5])!r}: "
                     f"{result.stderr.decode(errors='replace').strip()}")
            return result.returncode == 0
        except Exception as e:
            _log(f"tmux cmd failed: {' '.join(cmd[:5])!r}: {e}")
            return False

    @classmethod
    def _snapshot(cls, t: str) -> tuple[dict, dict] | None:
        """Read the projection and every sibling from the target's window."""
        marker = "__claude_window_options__"
        siblings_marker = "__claude_sibling_options__"
        pane_format = "\t".join(["#{pane_id}"] + ["#{@claude_" + f + "}" for f in cls._FIELDS])
        try:
            result = subprocess.run(
                [*TMUX_CMD, "show-options", "-p", "-t", t, ";",
                 "display-message", "-p", "-t", t, marker, ";",
                 "show-options", "-w", "-t", t, ";",
                 "display-message", "-p", "-t", t, siblings_marker, ";",
                 "list-panes", "-t", t, "-F", pane_format],
                capture_output=True, text=True, timeout=0.3,
            )
            if result.returncode != 0:
                _log(f"tmux option read failed for {t}: {result.stderr.strip()}")
                return
            pane_text, separator, window_text = result.stdout.partition(marker + "\n")
            window_text, siblings_separator, sibling_text = window_text.partition(siblings_marker + "\n")
            if not separator or not siblings_separator:
                _log(f"tmux option read incomplete for {t}")
                return
            current = {}
            for scope, text in (("p", pane_text), ("w", window_text)):
                for line in text.splitlines():
                    if line.startswith("@"):
                        name, value = shlex.split(line)
                        current[scope, name] = value
            siblings = {}
            for line in sibling_text.splitlines():
                parts = line.split("\t")
                if len(parts) == len(cls._FIELDS) + 1:
                    siblings[parts[0]] = dict(zip(cls._FIELDS, parts[1:]))
            return current, siblings
        except Exception as e:
            _log(f"tmux option read failed for {t}: {e}")
            return

    @classmethod
    def _window_options(cls, siblings: dict) -> list[tuple]:
        """Keep active delegation visible across every sibling in a window."""
        # Old hook fallbacks only write color/glyph. Resolve those through the
        # current theme rather than treating every legacy pane as idle.
        colors = {"#a6e3a1": "working", "#cba6f7": "agents", "#f9e2af": "asking",
                  "#f38ba8": "error", "#b4befe": "unread", "#89b4fa": "idle",
                  "#6699cc": "nudge", "#6c7086": "unknown"}
        try:
            theme = json.loads(THEME_PATH.read_text())
            for name, state in theme["states"].items():
                colors[theme["semantic_colors"][state["semantic"]]] = name
        except (OSError, ValueError, KeyError, TypeError):
            pass

        def rank(item):
            pane_id, options = item
            name = options.get("state_name") or colors.get(options.get("color"), "unknown")
            # Lower stable pane ID wins ties, regardless of focus/list ordering.
            delegated = bool(options.get("agents")) or name == "agents"
            return (-int(delegated), -cls._PRIORITY.get(name, 20), int(pane_id.lstrip("%")))

        candidates = [item for item in siblings.items() if item[1].get("state")]
        winner = dict(min(candidates, key=rank)[1]) if candidates else {}
        if winner.get("agents") or winner.get("state_name") == "agents":
            state, color, glyph = cls._theme.resolve_state("agents")
            winner.update(state_name=state, state=glyph, color=color)
        return [("w", "@claude_window_" + field, winner.get(field) or None)
                for field in cls._FIELDS]

    @classmethod
    def _apply(cls, t: str, current: dict, desired: list[tuple]) -> None:
        """Batch only changed options so stable polling never redraws clients."""
        cmd = list(TMUX_CMD)
        for scope, name, value in desired:
            if current.get((scope, name)) == value:
                continue
            if len(cmd) > len(TMUX_CMD):
                cmd.append(";")
            cmd.extend(["set", f"-{scope}" + ("u" if value is None else ""), "-t", t, name])
            if value is not None:
                cmd.append(value)
        if len(cmd) > len(TMUX_CMD):
            cls._run(cmd)

    @classmethod
    def write_state(cls, pane: PaneState) -> None:
        """Update the pane and its window's highest-priority lifecycle badge.

        NEVER touches voice options, including the voice window aggregate.
        Empty hook emoji preserves the last event. Empty persona clears it.
        """
        t = pane.pane_id
        snapshot = cls._snapshot(t)
        if snapshot is None:
            return
        current, siblings = snapshot
        agent_glyph = cls._theme.resolve_state("agents")[2]
        agents = (agent_glyph if pane.active_agent_count == 1 else f"{agent_glyph}{pane.active_agent_count}") \
            if pane.active_agent_count > 0 else None
        desired = [
            ("p", "@claude_state_name", pane.state_name),
            ("p", "@claude_state", pane.glyph),
            ("p", "@claude_color", pane.color),
            ("p", "@claude_persona", pane.persona or None),
            ("p", "@claude_focus", pane.focus_emoji or None),
            ("p", "@claude_agents", agents),
        ]
        if pane.hook_emoji:
            desired.append(("p", "@claude_hook", pane.hook_emoji))
        projected = dict(siblings.get(t, {}))
        for scope, name, value in desired:
            projected[name.removeprefix("@claude_")] = value
        siblings[t] = projected
        cls._apply(t, current, desired + cls._window_options(siblings))

    @classmethod
    def refresh_window(cls, pane_id: str) -> None:
        """Recompute after focus or layout changes without changing pane state."""
        snapshot = cls._snapshot(pane_id)
        if snapshot is not None:
            current, siblings = snapshot
            cls._apply(pane_id, current, cls._window_options(siblings))

    @classmethod
    def clear_state(cls, pane_id: str) -> None:
        """Clear lifecycle options and recompute the badge from live siblings."""
        snapshot = cls._snapshot(pane_id)
        if snapshot is None:
            return
        current, siblings = snapshot
        siblings.pop(pane_id, None)
        desired = [("p", "@claude_" + field, None) for field in cls._FIELDS]
        cls._apply(pane_id, current, desired + cls._window_options(siblings))

    @staticmethod
    def pane_exists(pane_id: str) -> bool:
        """Check if a tmux pane ID is currently valid.

        Raises TmuxUnreachable if the server could not be contacted at all.
        "tmux said no such pane" and "I could not ask tmux" are different
        answers; collapsing them into False let a connection failure read as
        "every pane died" and silently wipe the entire pane table.
        """
        try:
            result = subprocess.run(
                [*TMUX_CMD, "display-message", "-p", "-t", pane_id, "#{pane_id}"],
                capture_output=True, timeout=0.3,
            )
        except Exception as e:
            raise TmuxUnreachable(f"{type(e).__name__}: {e}") from e
        if result.returncode == 0:
            return bool(result.stdout.strip())
        err = result.stderr.decode("utf-8", "replace").strip()
        if "error connecting" in err or "no server running" in err:
            raise TmuxUnreachable(err)
        return False


# ---------------------------------------------------------------------------
# StreamManager — pipe-pane lifecycle for tmux-stream
# ---------------------------------------------------------------------------

class StreamManager:
    """Manages pipe-pane attachment for all tmux panes.

    Attaches `stream-handler.py` to each pane via tmux pipe-pane.
    The handler segments output into JSONL. This class only manages
    the pipe lifecycle — it does NOT process content.

    pipe-pane behavior:
        - One pipe per pane (new replaces old)
        - Survives tmux detach/reattach
        - Dies when handler process dies (no auto-reconnect)
        - Output only (-O flag)
    """

    def __init__(self) -> None:
        self._attached: set[str] = set()  # pane IDs with active pipes
        self._enabled = STREAM_HANDLER.exists()
        if not self._enabled:
            _log("stream: disabled (stream-handler.py not found)")

    def attach(self, pane_id: str) -> bool:
        """Attach pipe-pane to a pane. Returns True on success.

        Detaches any existing pipe first to ensure a clean handoff —
        the old handler gets EOF, flushes, and exits before the new one starts.
        """
        if not self._enabled or pane_id in self._attached:
            return False
        try:
            # Detach any existing pipe (old handler gets EOF and exits cleanly)
            subprocess.run(
                [*TMUX_CMD, "pipe-pane", "-t", pane_id],
                capture_output=True, timeout=0.5,
            )
            # -O = output only. python3 -u = unbuffered (critical for timely segments).
            # Shell-quote paths to prevent injection.
            cmd = f"python3 -u {shlex.quote(str(STREAM_HANDLER))} {shlex.quote(pane_id)}"
            result = subprocess.run(
                [*TMUX_CMD, "pipe-pane", "-O", "-t", pane_id, cmd],
                capture_output=True, timeout=1.0,
            )
            if result.returncode == 0:
                self._attached.add(pane_id)
                _log(f"stream: attached pipe to {pane_id}")
                return True
            else:
                _log(f"stream: pipe-pane failed for {pane_id}: {result.stderr.decode().strip()}")
                return False
        except Exception as e:
            _log(f"stream: attach error for {pane_id}: {e}")
            return False

    def capture_initial(self, pane_id: str) -> None:
        """One-time capture-pane for content that existed before pipe was attached.

        Writes directly to the JSONL file as an 'initial' segment.
        """
        if not self._enabled:
            return
        try:
            result = subprocess.run(
                [*TMUX_CMD, "capture-pane", "-p", "-S", "-", "-J", "-t", pane_id],
                capture_output=True, timeout=2.0,
            )
            if result.returncode == 0:
                content = result.stdout.decode(errors="replace").strip()
                if content:
                    STREAM_DIR.mkdir(parents=True, exist_ok=True)
                    record = {
                        "pane": pane_id,
                        "session": "",
                        "window": "",
                        "seq": -1,  # special: initial capture
                        "ts": _dt.now(_tz.utc).isoformat(),
                        "command": None,
                        "exit_code": None,
                        "output": content,
                        "initial_capture": True,
                    }
                    path = STREAM_DIR / f"{_dt.now(_tz.utc).strftime('%Y-%m-%d')}.jsonl"
                    with open(path, "a") as f:
                        f.write(json.dumps(record, ensure_ascii=False) + "\n")
                    _log(f"stream: captured initial scrollback for {pane_id} ({len(content)} chars)")
        except Exception as e:
            _log(f"stream: initial capture failed for {pane_id}: {e}")

    def detach(self, pane_id: str) -> None:
        """Remove a pane from the attached set (pane died, arbiter stopping)."""
        self._attached.discard(pane_id)

    def attach_all_panes(self) -> int:
        """Attach pipes to all existing tmux panes. Called on arbiter start.

        Returns count of newly attached panes.
        """
        if not self._enabled:
            return 0
        count = 0
        try:
            result = subprocess.run(
                [*TMUX_CMD, "list-panes", "-a", "-F", "#{pane_id}"],
                capture_output=True, timeout=2.0,
            )
            if result.returncode == 0:
                for line in result.stdout.decode().splitlines():
                    pane_id = line.strip()
                    if pane_id and pane_id not in self._attached:
                        self.capture_initial(pane_id)
                        if self.attach(pane_id):
                            count += 1
        except Exception as e:
            _log(f"stream: attach_all error: {e}")
        if count:
            _log(f"stream: attached {count} panes on startup")
        return count

    def reconcile(self, live_panes: set[str]) -> int:
        """Ensure all live panes have pipes. Re-attach crashed handlers.

        Called periodically from the arbiter's reconciliation loop.
        Returns count of corrections.
        """
        if not self._enabled:
            return 0
        corrections = 0

        # Remove dead panes from attached set
        dead = self._attached - live_panes
        for pane_id in dead:
            self._attached.discard(pane_id)

        # Attach pipes to panes missing them
        for pane_id in live_panes:
            if pane_id not in self._attached:
                if self.attach(pane_id):
                    corrections += 1

        return corrections

    @property
    def attached_count(self) -> int:
        return len(self._attached)


# ---------------------------------------------------------------------------
# TmuxArbiter — core state machine
# ---------------------------------------------------------------------------

class TmuxArbiter:
    """Owns indicator state for all tracked tmux panes."""

    def __init__(self) -> None:
        self.config = Config.load()
        self.theme = ThemeLoader()
        self.emoji = EmojiLookup()
        self.focus = FocusEngine(self.emoji)
        self.focus.DECAY_HALF_LIFE_FILE = self.config.focus_decay_half_life_file
        self.focus.DECAY_HALF_LIFE_CWD = self.config.focus_decay_half_life_cwd
        self.focus.DECAY_HALF_LIFE_INTENT = self.config.focus_decay_half_life_intent
        self.panes: dict[str, PaneState] = {}
        self.stream = StreamManager()
        # Read current native lifecycle without restarting existing agents.
        from agent_lifecycle import Collector as HarnessCollector, read_processes
        from codex_lifecycle import Collector as CodexCollector
        self.lifecycle_collectors = [HarnessCollector(), CodexCollector()]
        self.lifecycle_process_reader = read_processes
        self.lifecycle_bindings = {}
        self.rejected_events = {}
        self._load()
        self.reconcile_lifecycle()
        # Validate venture configuration on startup
        warnings = self.focus.validate_ventures()
        for w in warnings:
            _log(f"venture-warning: {w}")
        # Attach stream pipes to all existing panes after state is loaded
        self.stream.attach_all_panes()

    # ---- State persistence ------------------------------------------------

    def _persist(self) -> None:
        """Atomic write of all pane states to disk."""
        data = {pid: asdict(ps) for pid, ps in self.panes.items()}
        tmp = STATE_PATH.with_suffix(".tmp")
        try:
            STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(json.dumps(data, indent=2))
            tmp.rename(STATE_PATH)
        except Exception as e:
            _log(f"persist failed: {e}")

    def _load(self) -> None:
        """Load a UI checkpoint, deferring projection until native validation.

        Also absorbs pre-existing Claude panes (those with @claude_state set in tmux)
        that are not in the state file — e.g. sessions started before the arbiter ran.
        Legacy rows start unknown and cannot restore an active badge on their own.
        """
        # Phase 1: restore from state file
        if STATE_PATH.exists():
            try:
                data = json.loads(STATE_PATH.read_text())
                restored = 0
                skipped = 0
                corrected = 0
                for pid, ps_dict in data.items():
                    try:
                        if not re.fullmatch(r"%\d+", pid) or not isinstance(ps_dict, dict):
                            raise ValueError("invalid pane record")
                        known = {f.name for f in fields(PaneState)}
                        pane = PaneState(**{k: v for k, v in ps_dict.items() if k in known})
                        if (pane.pane_id != pid or not math.isfinite(pane.updated_at)
                                or not isinstance(pane.focus_signals, list)
                                or any(not isinstance(getattr(pane, key), int)
                                       or getattr(pane, key) < 0 for key in
                                       ("active_agent_count", "subagent_depth"))
                                or any(not isinstance(getattr(pane, key), str) for key in
                                       ("state_name", "color", "glyph", "persona",
                                        "lifecycle_source", "lifecycle_identity"))):
                            raise ValueError("invalid pane identity or metadata")
                    except (TypeError, ValueError):
                        skipped += 1
                        _log(f"state load: skipped invalid record {pid!r}")
                        continue
                    if TmuxWriter.pane_exists(pid):
                        # Phase 1.5: correct stale/mismatched states at load time.
                        _, idle_color, idle_glyph = self.theme.resolve_event("SessionStart")
                        if pane.state_name == "idle" and pane.color != idle_color:
                            # Color mismatch on idle pane — normalize to canonical idle color.
                            pane = replace(pane, color=idle_color, glyph=idle_glyph)
                            corrected += 1
                        # If persona is unknown, detect from --agent flag in Claude process.
                        if not pane.persona:
                            persona_key = detect_pane_persona(pid)
                            if persona_key:
                                emoji = self.theme.get_persona_emoji(persona_key, self.emoji)
                                if emoji:
                                    pane = replace(pane, persona=emoji)
                                    corrected += 1
                                    _log(f"phase1.5: detected persona {persona_key} ({emoji}) for {pid}")
                        if pane.state_name in ("working", "agents", "asking"):
                            state, color, glyph = self.theme.resolve_state("unknown")
                            pane = replace(pane, state_name=state, color=color, glyph=glyph,
                                           last_source="reconcile", active_agent_count=0,
                                           subagent_depth=0)
                        if not pane.lifecycle_source:
                            state, color, glyph = self.theme.resolve_state("unknown")
                            pane = replace(pane, state_name=state, color=color, glyph=glyph,
                                           lifecycle_source="saved-cache-unvalidated",
                                           active_agent_count=0, subagent_depth=0)
                        self.panes[pid] = pane
                        # Restore focus signals into FocusEngine
                        for sig_dict in ps_dict.get("focus_signals", []):
                            try:
                                sig = FocusSignal(**sig_dict)
                                self.focus._signals.setdefault(pid, {})[sig.key] = sig
                            except (TypeError, KeyError):
                                pass
                        # Cache rows are not projected here. The initial native
                        # scan validates the execution before restoring badges.
                        restored += 1
                    else:
                        skipped += 1
                extra = f", {corrected} color-corrected" if corrected else ""
                _log(f"state loaded: {restored} restored, {skipped} skipped (dead panes){extra}")
                if corrected:
                    self._persist()  # write corrected colors back to state file
            except TmuxUnreachable as e:
                _log(f"state load ABORTED — tmux unreachable on socket "
                     f"{TMUX_SOCKET or '(default)'}: {e}. State file kept intact; "
                     f"not starting fresh.")
            except Exception as e:
                _log(f"state load failed: {e} — starting fresh")

        # Phase 2: absorb pre-existing Claude panes not in the state file.
        # These are panes with @claude_state set (from old hook.sh or previous arbiter run)
        # that we don't yet track. Adopt them with their current tmux options.
        try:
            result = subprocess.run(
                [*TMUX_CMD, "list-panes", "-a", "-F", "#{pane_id} #{@claude_state}"],
                capture_output=True, timeout=2.0,
            )
            if result.returncode == 0:
                absorbed = 0
                now = time.time()
                for line in result.stdout.decode().splitlines():
                    parts = line.split(" ", 1)
                    if len(parts) != 2:
                        continue
                    pid, state_opt = parts[0], parts[1].strip()
                    if not state_opt or pid in self.panes:
                        continue  # no indicator set, or already tracked
                    # Read current tmux options to build PaneState
                    # Note: @claude_color is intentionally NOT read — we always use the
                    # canonical idle color (stale hook.sh writes are discarded).
                    persona_r = subprocess.run(
                        [*TMUX_CMD, "show-options", "-pv", "-t", pid, "@claude_persona"],
                        capture_output=True, timeout=0.3,
                    )
                    # Use @claude_persona if set; otherwise detect from --agent process flag.
                    persona = persona_r.stdout.decode().strip()
                    if not persona:
                        persona_key = detect_pane_persona(pid)
                        if persona_key:
                            persona = self.theme.get_persona_emoji(persona_key, self.emoji)
                            if persona:
                                _log(f"absorption: detected persona {persona_key} ({persona}) for {pid}")
                    # Always use the proper idle color — do NOT preserve stale @claude_color
                    # from old hook.sh writes (which may be green/working from a stuck state).
                    state_name, resolved_color, resolved_glyph = self.theme.resolve_state("unknown")
                    pane = PaneState(
                        pane_id=pid,
                        state_name=state_name,
                        color=resolved_color,
                        glyph=resolved_glyph,  # use canonical idle glyph, not stale tmux option
                        persona=persona,
                        updated_at=now,
                        session_start_at=0.0,  # safe: no startup guard on adopted panes
                        lifecycle_source="saved-cache-unvalidated",
                    )
                    self.panes[pid] = pane
                    absorbed += 1
                if absorbed:
                    _log(f"absorbed {absorbed} pre-existing Claude pane(s) not in state file")
                    self._persist()
        except Exception as e:
            _log(f"pane absorption failed: {e}")

    # ---- Event handling ---------------------------------------------------

    def apply_event(self, event: str, pane_id: str, data: dict) -> None:
        """Apply a hook event to the state machine.

        This is the main entry point for all events forwarded from hook.sh.
        All guards, state transitions, and persona resolution happen here.
        """
        if not pane_id or pane_id == "_global":
            return

        prev = self.panes.get(pane_id)
        provider = data.get("_provider") or (prev.provider if prev else "claude")
        if event != "PaneRead" and prev and prev.lifecycle_source:
            session = data.get("session_id")
            if (provider != prev.provider or
                    (session and session != prev.lifecycle_identity.rsplit(":", 1)[-1])):
                _log(f"drop {event} on {pane_id}: hook does not match native execution")
                return

        # Guard 0: PaneRead — synthetic event from tmux-window-state.sh.
        # Transitions unread → idle. Not a Claude Code hook event.
        if event == "PaneRead":
            pane = self.panes.get(pane_id)
            if pane and pane.state_name == "unread":
                state_name, color, glyph = self.theme.resolve_state("idle")
                history = self._record_event(pane, "PaneRead", "unread", state_name, "hook")
                new_pane = replace(pane, state_name=state_name, color=color,
                                   glyph=glyph, updated_at=time.time(),
                                   lifecycle_unread=False,
                                   last_source="hook", event_history=history)
                self.panes[pane_id] = new_pane
                TmuxWriter.write_state(new_pane)
                self._persist()
                _log(f"read: {pane_id} unread → idle")
            return

        # Guard 1: Compaction — SessionStart(source=compact) is a no-op.
        # The session is still active; don't reset to idle.
        if event == "SessionStart" and data.get("source") == "compact":
            _log(f"compaction guard: skipping SessionStart(compact) on {pane_id}")
            return

        # Guard 2: Notification dispatch — polymorphic, dispatch by notification_type
        if event == "Notification":
            ntype = data.get("notification_type", "")
            state_name, color, glyph = self.theme.resolve_notification(ntype)
        else:
            state_name, color, glyph = self.theme.resolve_event(event)

        # Guard 3: Clear state (SessionEnd) — wipe indicators and remove from store
        if state_name == "clear":
            _log(f"clear: {pane_id} [{event}]")
            TmuxWriter.clear_state(pane_id)
            self.focus.clear_pane(pane_id)
            self.panes.pop(pane_id, None)
            self._persist()
            return

        # Guard 4: Startup noise suppression.
        # PreToolUse and UserPromptSubmit both fire during Claude's init sequence
        # (loading plugins, running hooks). Suppress ALL working-state transitions
        # within STARTUP_GUARD_SECS of SessionStart.
        if state_name == "working" and self._in_startup_window(pane_id):
            _log(f"startup guard: suppressed {event}→working on {pane_id}")
            return

        # Persona resolution — sticky: preserve existing persona unless new agent_type
        existing_persona = prev.persona if prev else ""
        new_persona = existing_persona  # default: keep what's there

        if agent_type := data.get("agent_type", ""):
            persona_key = agent_type.split(":")[-1]
            resolved = self.theme.get_persona_emoji(persona_key, self.emoji)
            if resolved:
                new_persona = resolved
                _log(f"persona: {pane_id} → {persona_key} ({resolved})")
        elif not new_persona and event == "SessionStart":
            # On SessionStart with no existing persona, detect from --agent process flag.
            # This bootstraps persona for new sessions before SubagentStart fires.
            persona_key = detect_pane_persona(pane_id)
            if persona_key:
                resolved = self.theme.get_persona_emoji(persona_key, self.emoji)
                if resolved:
                    new_persona = resolved
                    _log(f"persona: {pane_id} → {persona_key} ({resolved}) [detected from process]")

        # Session start tracking — record timestamp for startup guard
        session_start_at = prev.session_start_at if prev else 0.0
        if event == "SessionStart":
            session_start_at = time.time()

        # Subagent depth tracking — count unmatched SubagentStart/SubagentStop pairs.
        # This lets Stop return to agents state instead of idle when a subagent is active.
        # Real scenario: SubagentStart → PermissionRequest → Stop → should return to agents.
        prev_depth = prev.subagent_depth if prev else 0
        new_depth = prev_depth
        if event == "SubagentStart":
            new_depth = prev_depth + 1
        elif event == "SubagentStop":
            new_depth = max(0, prev_depth - 1)
        elif event in ("SessionStart", "SessionEnd"):
            new_depth = 0  # session boundary resets depth
        if provider == "codex" and "_active_agent_count" in data:
            # The Codex adapter de-duplicates by stable agent_id, so its set
            # cardinality is authoritative even when stops arrive out of order.
            new_depth = max(0, int(data.get("_active_agent_count", 0)))

        # Codex delegation is not a mutually-exclusive state: the parent can
        # continue reasoning or using tools while children run. Keep the base
        # state working and expose delegation through @claude_agents.
        if provider == "codex" and event in ("SubagentStart", "SubagentStop"):
            state_name, color, glyph = self.theme.resolve_event("UserPromptSubmit")

        # Guard 5: Subagent active → preserve agents state.
        # When subagent_depth > 0, the parent has dispatched agents that haven't
        # returned yet. Two sub-guards:
        #
        # 5a: PreToolUse/UserPromptSubmit → working: The subagent is using tools,
        #     but the user cares that an AGENT is running, not that a tool fired.
        #     Stay purple.
        #
        # 5b: Stop/TeammateIdle/TaskCompleted → idle: A sub-turn ended but the
        #     agent tree is still active. Return to agents, not idle.
        if provider != "codex" and new_depth > 0 and state_name == "working" and event in ("PreToolUse", "UserPromptSubmit"):
            state_name, color, glyph = self.theme.resolve_event("SubagentStart")
            # Don't log — this fires very frequently

        if provider != "codex" and state_name == "idle" and event in ("Stop", "TeammateIdle", "TaskCompleted", "PostCompact") and new_depth > 0:
            state_name, color, glyph = self.theme.resolve_event("SubagentStart")
            _log(f"subagent guard: {pane_id} Stop→idle overridden to agents (depth={new_depth})")

        # Guard 5c: Bypass permissions → no actual dialog shown.
        # PermissionRequest fires even in bypassPermissions mode, but no user
        # prompt appears. Stay on current state instead of transitioning to asking.
        if event == "PermissionRequest" and data.get("permission_mode") == "bypassPermissions":
            if prev:
                state_name, color, glyph = prev.state_name, prev.color, prev.glyph
                _log(f"bypass guard: {pane_id} PermissionRequest suppressed (bypassPermissions, staying {state_name})")
            # If no prev state, fall through to asking — safe default

        # Guard 6: Unread — when a turn ends and the pane is NOT focused,
        # show "unread" (lavender) instead of "idle" (blue). The user hasn't
        # seen the response yet. Transitions to idle when user visits the pane.
        if state_name == "idle" and event in ("Stop", "TeammateIdle", "TaskCompleted"):
            focused = self._get_focused_pane()
            if focused and pane_id != focused:
                unread = self.theme.resolve_state("unread")
                if unread[0]:  # state exists in theme
                    state_name, color, glyph = unread
                    _log(f"unread: {pane_id} Stop→unread (focused={focused})")

        # Record event history before constructing new state
        prev_name = prev.state_name if prev else "(new)"
        if prev:
            history = self._record_event(prev, event, prev_name, state_name, "hook")
        else:
            history = [{
                "t": round(time.time(), 1),
                "event": event,
                "from": prev_name,
                "to": state_name,
                "src": "hook",
            }]

        # Hook emoji resolution — two-slot grammar: event + optional refinement
        # Each slot normalized to 2 columns for consistent tmux spacing
        event_key = hook_to_event_key(event)
        slot1 = self.emoji.get_tmux(event_key)
        slot2 = ""
        ref_key = refinement_key(event, data)
        if ref_key:
            slot2 = self.emoji.get_tmux(ref_key)
        if self.config.hook_emoji_slots == 1:
            hook_emoji_val = slot2 if slot2 else slot1
        else:
            hook_emoji_val = slot1 + slot2

        # Focus resolution — decay-aware two-slot composition
        prev_window = prev.window_name if prev else ""
        cached_window = prev_window

        if event in ("SessionStart", "CwdChanged"):
            # Query and cache window name
            try:
                result = subprocess.run(
                    [*TMUX_CMD, "display-message", "-t", pane_id, "-p", "#{window_name}"],
                    capture_output=True, text=True, timeout=0.2,
                )
                cached_window = result.stdout.strip()
            except Exception:
                pass

            # Window name → observe venture + plugin signals
            wn_v, wn_p = self.focus.resolve_window_name(cached_window)
            if wn_v:
                self.focus.observe("window_name", wn_v, pane_id)
            if wn_p:
                self.focus.observe("window_name", wn_p, pane_id)

            # cwd → observe venture baseline
            cwd = data.get("cwd", "")
            if cwd:
                cwd_v, _ = self.focus.resolve_cwd(cwd)
                if cwd_v:
                    self.focus.observe("cwd", cwd_v, pane_id)

        elif event == "PreToolUse":
            # Lazy-load window name if not cached
            if not cached_window:
                try:
                    result = subprocess.run(
                        [*TMUX_CMD, "display-message", "-t", pane_id, "-p", "#{window_name}"],
                        capture_output=True, text=True, timeout=0.2,
                    )
                    cached_window = result.stdout.strip()
                except Exception:
                    pass

            # Always observe window name — ensures signals exist even after
            # arbiter restart when _signals was empty but window_name was cached
            if cached_window:
                wn_v, wn_p = self.focus.resolve_window_name(cached_window)
                if wn_v:
                    self.focus.observe("window_name", wn_v, pane_id)
                if wn_p:
                    self.focus.observe("window_name", wn_p, pane_id)

            # File path → observe venture + plugin signals
            tool_input = data.get("tool_input", {})
            if isinstance(tool_input, dict):
                file_path = tool_input.get("file_path", "")
                if file_path:
                    fp_v, fp_p = self.focus.resolve_file_path(file_path)
                    if fp_p:
                        self.focus.observe("file_path", fp_p, pane_id)
                    if fp_v:
                        self.focus.observe("file_path", fp_v, pane_id)

        # Intent classification from SubagentStart
        if event == "SubagentStart":
            agent_type = data.get("agent_type", "")
            action = self.focus.intent.from_agent_type(agent_type)
            if action:
                self.focus.observe("agent_type", action, pane_id)

        # Intent classification from Skill invocation (sticky — session-level)
        if event == "PreToolUse" and data.get("tool_name") == "Skill":
            tool_input = data.get("tool_input", {})
            if isinstance(tool_input, dict):
                skill_name = tool_input.get("skill", "")
                if skill_name:
                    action = self.focus.intent.from_skill_name(skill_name)
                    if action:
                        self.focus.observe("skill", action, pane_id)

        # Person detection from shared vault file paths
        if event == "PreToolUse":
            tool_input = data.get("tool_input", {})
            if isinstance(tool_input, dict):
                file_path = tool_input.get("file_path", "")
                vault = self.config.shared_vault_path
                person = self.config.shared_vault_person
                if file_path and vault and person and \
                        file_path.startswith(str(Path(vault).expanduser())):
                    self.focus.observe("file_path", f"person:{person}", pane_id)

        # Resolve via decay-weighted scoring
        focus_result = self.focus.resolve_focus(pane_id)
        focus_val = focus_result.emoji
        focus_venture = focus_result.slot1 if focus_result.slot1.startswith("venture:") else ""
        focus_plugin = ""
        for key in (focus_result.slot1, focus_result.slot2):
            if key.startswith("plugin:"):
                focus_plugin = key
                break
        prev_focus_val = prev.focus_emoji if prev else ""
        if focus_val != prev_focus_val:
            _log(f"focus: {pane_id} v={focus_venture} p={focus_plugin} → {focus_val}")

        # Apply transition
        now = time.time()
        new_state = PaneState(
            pane_id=pane_id,
            state_name=state_name,
            color=color,
            glyph=glyph,
            persona=new_persona,
            updated_at=now,
            session_start_at=session_start_at,
            subagent_depth=new_depth,
            last_working_at=now if state_name == "working" else (prev.last_working_at if prev else 0.0),
            last_source="hook",
            event_history=history,
            hook_emoji=hook_emoji_val,
            focus_emoji=focus_val,
            focus_venture=focus_venture,
            focus_plugin=focus_plugin,
            window_name=cached_window,
            focus_signals=[asdict(s) for s in self.focus._signals.get(pane_id, {}).values()],
            provider=provider,
            active_agent_count=new_depth if provider == "codex" else 0,
            lifecycle_source=prev.lifecycle_source if prev else "",
            lifecycle_identity=prev.lifecycle_identity if prev else "",
            lifecycle_unread=state_name == "unread",
        )

        _log(f"transition: {pane_id} {prev_name} → {state_name} [{event}] depth={new_depth}")

        self.panes[pane_id] = new_state
        TmuxWriter.write_state(new_state)
        self._persist()

    # ---- Startup guard ----------------------------------------------------

    def _in_startup_window(self, pane_id: str) -> bool:
        """True if within STARTUP_GUARD_SECS of SessionStart on this pane.

        Returns False if pane not known or session_start_at is 0.0 (sentinel).
        """
        pane = self.panes.get(pane_id)
        if not pane or pane.session_start_at == 0.0:
            return False
        return (time.time() - pane.session_start_at) < self.config.startup_guard_secs

    def _record_event(self, pane: PaneState, event: str, from_state: str,
                      to_state: str, source: str) -> list:
        """Append an event to the pane's history, bounded to max_event_history."""
        max_h = self.config.max_event_history
        history = list(pane.event_history) if pane.event_history else []
        history.append({
            "t": round(time.time(), 1),
            "event": event,
            "from": from_state,
            "to": to_state,
            "src": source,
        })
        if len(history) > max_h:
            history = history[-max_h:]
        return history

    def _get_focused_pane(self) -> str:
        """Read the currently focused pane ID from voice arbiter's focus-state file.

        Returns empty string if file doesn't exist, can't be read, or is stale
        (> focus_stale_secs, meaning voice arbiter is probably down). Without this
        guard, a stale file would cause ALL Stop events to produce 'unread'.
        """
        try:
            if not FOCUS_STATE_FILE.exists():
                return ""
            age = time.time() - FOCUS_STATE_FILE.stat().st_mtime
            if age > self.config.focus_stale_secs:
                return ""
            return FOCUS_STATE_FILE.read_text().strip()
        except Exception:
            return ""

    # ---- Reconciliation ---------------------------------------------------

    def validate_hook(self, message: dict) -> str | None:
        """Bind incoming hooks to native evidence, never to a saved badge."""
        pane_id = message.get("pane", "")
        binding = self.lifecycle_bindings.get(pane_id) if isinstance(pane_id, str) else None
        if binding is None:
            self.reconcile_lifecycle()
            binding = self.lifecycle_bindings.get(pane_id) if isinstance(pane_id, str) else None
        # Check structure and native session/provider before spending any IPC.
        reason = rejection_reason({k: v for k, v in message.items() if k != "source"},
                                  binding, "", time.time(), self.config.hook_priority_secs)
        if reason:
            return reason
        try:
            result = subprocess.run(
                [*TMUX_CMD, "display-message", "-p", "-t", pane_id,
                 "#{pane_pid}\t#{socket_path}"],
                capture_output=True, text=True, timeout=0.3,
            )
            shell_pid, socket_path = result.stdout.strip().split("\t")
            if result.returncode or not process_is_current(binding, int(shell_pid)):
                return "execution-mismatch"
        except (OSError, ValueError, subprocess.TimeoutExpired):
            return "binding-unavailable"
        # A legacy adapter has no server envelope. Its session must have one
        # unambiguous native owner, in addition to the live ancestry check.
        matches = sum(row.get("provider") == binding["provider"]
                      and row.get("session_id") == binding["session_id"]
                      for row in self.lifecycle_bindings.values())
        if matches != 1:
            return "ambiguous-session"
        return rejection_reason(message, binding, socket_path, time.time(),
                                self.config.hook_priority_secs)

    def reconcile_lifecycle(self) -> int:
        """Project native, process-bound state. Quiet inference remains working.

        An unreadable tmux inventory establishes no process exits. Missing
        lifecycle evidence is unknown, never evidence that an agent is idle.
        """
        collectors = getattr(self, "lifecycle_collectors", ())
        if not collectors:
            return 0
        try:
            result = subprocess.run(
                [*TMUX_CMD, "list-panes", "-a", "-F",
                 "#{pane_id}\t#{pane_pid}\t#{pane_current_command}\t#{pane_title}"],
                capture_output=True, text=True, timeout=1,
            )
            if result.returncode:
                return 0
            inventory = {}
            for line in result.stdout.splitlines():
                values = line.split("\t", 3)
                pane_id, pid, command = values[:3]
                inventory[pane_id] = {"pid": int(pid), "command": command,
                                      "title": values[3] if len(values) > 3 else ""}
        except (OSError, ValueError, subprocess.TimeoutExpired):
            return 0

        observed = {}
        failed = False
        try:
            reader = getattr(self, "lifecycle_process_reader", None)
            processes = reader() if reader else None
        except Exception as exc:
            _log(f"lifecycle process reader failed: {type(exc).__name__}")
            failed = True
        for collector in (() if failed else collectors):
            try:
                snapshot = collector.poll(inventory, processes=processes) if reader else collector.poll(inventory)
                observed.update(snapshot)
            except Exception as exc:
                _log(f"lifecycle reader failed: {type(exc).__name__}")
                failed = True
        for pane_id, metadata in inventory.items():
            command = metadata["command"]
            if not failed and pane_id not in observed and command in {"claude", "codex", "pi", "omp"}:
                process = processes.get(metadata["pid"]) if reader and not failed else None
                observed[pane_id] = {"provider": command, "state": "unknown",
                    "source": "native-metadata-unavailable", "process_id": metadata["pid"],
                    "process_start": process.start if process else "", "session_id": ""}
        self.lifecycle_bindings = {} if failed else observed
        now = time.time()
        focused = self._get_focused_pane()
        corrections = 0
        for pane_id, evidence in observed.items():
            if pane_id not in inventory:
                continue
            prev = self.panes.get(pane_id)
            provider = evidence["provider"]
            native_state = evidence.get("state", "unknown")
            if native_state not in ("working", "idle", "asking", "error", "unknown"):
                native_state = "unknown"
            identity = ":".join(str(evidence.get(key, "")) for key in
                                ("process_id", "process_start", "session_id"))
            same_execution = bool(prev and prev.provider == provider
                                  and prev.lifecycle_identity == identity)
            state = native_state
            hook_override = False
            native_updated = evidence.get("updated_at", evidence.get("observed_at")) or 0
            # A hook can expose a dialog before the transcript persists it.
            if (same_execution and prev.last_source == "hook"
                    and ((prev.state_name in ("working", "agents", "asking")
                          and now - prev.updated_at < self.config.hook_priority_secs)
                         or provider == "claude" and prev.state_name in ("idle", "unread", "error"))
                    and native_updated <= prev.updated_at):
                state = prev.state_name
                hook_override = True
            elif (same_execution and state == "unknown" and prev.last_source == "hook"
                    and now - prev.updated_at < self.config.hook_priority_secs):
                state = prev.state_name
                hook_override = True
            elif same_execution and native_state == "idle":
                if prev.state_name == "unread" or prev.lifecycle_unread:
                    state = "idle" if pane_id == focused else "unread"
                elif prev.state_name in ("working", "agents", "asking") and pane_id != focused:
                    state = "unread"
            _, color, glyph = self.theme.resolve_state(state)
            source = evidence.get("source", "native")
            active_agent_count = (prev.active_agent_count if prev and same_execution
                                  and state == "working" else 0)
            if provider == "codex" and "child_count_known" in evidence:
                active_agent_count = (max(0, int(evidence.get("active_agent_count", 0)))
                                      if evidence["child_count_known"] else 0)
            if (prev and prev.state_name == state and prev.lifecycle_identity == identity
                    and prev.provider == provider
                    and prev.lifecycle_source == source and prev.color == color and prev.glyph == glyph
                    and prev.active_agent_count == active_agent_count):
                continue
            history = self._record_event(prev, "native_lifecycle", prev.state_name, state, "reconcile") if prev else []
            if prev:
                pane = replace(prev, state_name=state, color=color, glyph=glyph,
                               updated_at=prev.updated_at if hook_override else now,
                               provider=provider, last_source="hook" if hook_override else "reconcile",
                               lifecycle_source=source, lifecycle_identity=identity,
                               lifecycle_unread=(state == "unread" or (same_execution
                                   and state == "unknown" and prev.lifecycle_unread and pane_id != focused)),
                               event_history=history,
                               subagent_depth=prev.subagent_depth if state == "working" and same_execution else 0,
                               active_agent_count=active_agent_count)
            else:
                pane = PaneState(pane_id, state, color, glyph,
                                 self.theme.get_persona_emoji(provider, self.emoji), now, 0,
                                 provider=provider, last_source="reconcile",
                                 lifecycle_source=source, lifecycle_identity=identity,
                                 active_agent_count=active_agent_count)
            self.panes[pane_id] = pane
            TmuxWriter.write_state(pane)
            corrections += 1
        for pane_id, prev in list(self.panes.items()):
            if not prev.lifecycle_source or pane_id in observed:
                continue
            if failed:
                # A failed reader does not prove an exit or a quiet model.
                _, color, glyph = self.theme.resolve_state("unknown")
                if prev.state_name != "unknown" or prev.active_agent_count:
                    pane = replace(prev, state_name="unknown", color=color, glyph=glyph, updated_at=now,
                                   active_agent_count=0)
                    self.panes[pane_id] = pane
                    TmuxWriter.write_state(pane)
                    corrections += 1
            else:
                # The pane may remain as a shell after its agent exits.
                self.panes.pop(pane_id)
                self.focus.clear_pane(pane_id)
                if pane_id in inventory:
                    TmuxWriter.clear_state(pane_id)
                corrections += 1
        if corrections:
            self._persist()
        return corrections

    def reconcile(self) -> int:
        """Check all tracked panes for drift. Returns count of corrections.

        Also re-syncs all live panes to tmux unconditionally — heals divergence
        caused by old hook.sh versions writing directly to tmux (bypassing the
        arbiter) or any other source writing to pane options.

        Phase 3: discover untracked panes running claude that the arbiter doesn't
        know about (e.g. hooks never fired, $TMUX_PANE was empty, session resumed
        without triggering SessionStart). These are auto-registered as idle with
        persona detected from the process args.
        """
        now = time.time()
        corrections = 0

        # Snapshot keys — panes dict may be modified during iteration
        for pane_id in list(self.panes.keys()):
            pane = self.panes[pane_id]
            pane_changed = False

            # Remove panes that no longer exist in tmux (crashed, closed).
            # If tmux itself is unreachable we know nothing about any pane —
            # abandon the whole pass rather than declaring them all dead.
            try:
                alive = TmuxWriter.pane_exists(pane_id)
            except TmuxUnreachable as e:
                _log(f"reconcile: ABORTED — tmux unreachable on socket "
                     f"{TMUX_SOCKET or '(default)'}: {e}. State left untouched.")
                return corrections
            if not alive:
                _log(f"reconcile: removing dead pane {pane_id}")
                del self.panes[pane_id]
                self.stream.detach(pane_id)
                corrections += 1
                continue

            if pane.lifecycle_source:
                # Native lifecycle is authoritative for these panes. Repair
                # projection drift without CPU-based working/idle guesses.
                TmuxWriter.write_state(pane)
                continue

            # Clear stale working/agents states.
            # - working/agents: Stop/SubagentStop didn't fire (Ctrl+C). 2min timeout.
            # - unread: NEVER auto-cleared. Persists until user sends PaneRead.
            # For working: only clear hooked panes (hookless panes use CPU detection).
            # For agents: always clear regardless (no CPU fallback).
            event_age = now - pane.updated_at
            hookless = event_age > self.config.hookless_threshold
            stale_timeout = self.config.stale_working_mins
            should_check_stale = pane.provider != "codex" and (
                (pane.state_name == "agents")  # always check — no CPU fallback
                or (pane.state_name == "working" and not hookless)  # working: only hooked panes
            )
            if should_check_stale:
                age_mins = event_age / 60
                if age_mins > stale_timeout:
                    old_state = pane.state_name
                    state_name, color, glyph = self.theme.resolve_event("Stop")
                    history = self._record_event(pane, "stale_clear", old_state, state_name, "reconcile")
                    pane = replace(
                        pane,
                        state_name=state_name,
                        color=color,
                        glyph=glyph,
                        updated_at=now,
                        event_history=history,
                        subagent_depth=0,  # reset — SubagentStop was lost
                    )
                    _log(
                        f"reconcile: stale {old_state} cleared on {pane_id} "
                        f"(age={age_mins:.1f}min, depth reset)"
                    )
                    self.panes[pane_id] = pane
                    pane_changed = True
                    corrections += 1

            # --- Phase 4: Multi-signal activity detection for hookless panes ---
            #
            # Resumed sessions have frozen hook registries — no events arrive.
            # Use multiple signals to detect working vs idle:
            #   1. CPU tick delta (general activity — fast, cheap)
            #   2. Recent child processes (tool execution — reliable for Bash)
            #
            # Guards:
            #   - Only runs for panes with no hook events in 15+ seconds
            #   - Never overrides a recent hook-driven state (< 60s old)
            #   - Never overrides "agents" state (subagent depth is authoritative)
            #   - Hysteresis: 6s holdoff before transitioning working → idle
            #
            event_age = now - pane.updated_at
            hookless = event_age > self.config.hookless_threshold

            if hookless and pane.last_source == "hook" and event_age < self.config.hook_priority_secs:
                pass  # Hook set this state recently — trust it, don't override
            elif hookless and pane.provider != "codex" and pane.state_name not in ("agents",):
                cpu_active = detect_pane_cpu_active(pane_id)
                tool_running = detect_tool_execution(pane_id)

                # Composite signal: either CPU active OR tool running = working
                is_working = (cpu_active is True) or (tool_running is True)
                is_idle = (cpu_active is False) and (tool_running is not True)

                if is_working and pane.state_name == "idle":
                    state_name, color, glyph = self.theme.resolve_event("UserPromptSubmit")
                    history = self._record_event(pane, "cpu_detect", pane.state_name, state_name, "reconcile")
                    pane = replace(pane, state_name=state_name, color=color,
                                   glyph=glyph, updated_at=now,
                                   last_working_at=now, last_source="reconcile",
                                   event_history=history)
                    self.panes[pane_id] = pane
                    pane_changed = True
                    _log(f"reconcile: detected working on {pane_id}"
                         f" (cpu={cpu_active}, tool={tool_running})")
                    corrections += 1
                elif is_working and pane.state_name == "working":
                    # Still working — refresh in-memory timestamps only.
                    # Do not rewrite tmux options when the visible state is unchanged.
                    # Frequent no-op tmux writes cause redraw churn that clears native
                    # terminal selection in tmux panes.
                    pane = replace(
                        pane,
                        updated_at=now,
                        last_working_at=now,
                        last_source="reconcile",
                    )
                    self.panes[pane_id] = pane
                elif is_idle and pane.state_name == "working" and pane.subagent_depth == 0:
                    # Hysteresis: don't transition to idle if we were working < 6s ago
                    if (now - pane.last_working_at) < self.config.holdoff_secs:
                        pass  # Skip — check again next cycle
                    else:
                        state_name, color, glyph = self.theme.resolve_event("Stop")
                        history = self._record_event(pane, "cpu_idle", pane.state_name, state_name, "reconcile")
                        pane = replace(pane, state_name=state_name, color=color,
                                       glyph=glyph, updated_at=now,
                                       last_source="reconcile",
                                       event_history=history)
                        self.panes[pane_id] = pane
                        pane_changed = True
                        _log(f"reconcile: detected idle on {pane_id}"
                             f" (cpu={cpu_active}, tool={tool_running})")
                        corrections += 1

            # Re-resolve focus — decay may have changed the winner since last event
            focus_result = self.focus.resolve_focus(pane_id)
            if focus_result.emoji != pane.focus_emoji:
                pane = replace(
                    pane,
                    focus_emoji=focus_result.emoji,
                    focus_venture=focus_result.slot1 if focus_result.slot1.startswith("venture:") else "",
                    focus_plugin=next((k for k in (focus_result.slot1, focus_result.slot2) if k.startswith("plugin:")), ""),
                    focus_signals=[asdict(s) for s in self.focus._signals.get(pane_id, {}).values()],
                )
                self.panes[pane_id] = pane
                pane_changed = True
                corrections += 1

            if pane_changed:
                TmuxWriter.write_state(pane)

        # --- Phase 3: discover untracked claude panes ---
        # Scan all tmux panes for claude processes not yet tracked by the arbiter.
        # This catches sessions where hooks never fired (missing $TMUX_PANE, resume
        # bugs, etc.) and auto-registers them with correct persona.
        try:
            r = subprocess.run(
                [*TMUX_CMD, "list-panes", "-a", "-F", "#{pane_id}\t#{pane_current_command}"],
                capture_output=True, timeout=1.0,
            )
            if r.returncode == 0:
                for line in r.stdout.decode().splitlines():
                    parts = line.split("\t", 1)
                    if len(parts) != 2:
                        continue
                    pane_id, cmd = parts
                    if cmd != "claude" or pane_id in self.panes:
                        continue
                    # Untracked claude pane — register it
                    state_name, color, glyph = self.theme.resolve_event("SessionStart")
                    persona_key = detect_pane_persona(pane_id)
                    persona_emoji = self.theme.get_persona_emoji(persona_key, self.emoji) if persona_key else ""
                    new_pane = PaneState(
                        pane_id=pane_id,
                        state_name=state_name,
                        color=color,
                        glyph=glyph,
                        persona=persona_emoji,
                        updated_at=now,
                        session_start_at=now,
                    )
                    self.panes[pane_id] = new_pane
                    TmuxWriter.write_state(new_pane)
                    _log(f"reconcile: discovered untracked claude pane {pane_id}"
                         f"{f' ({persona_key})' if persona_key else ''}")
                    corrections += 1
        except Exception:
            pass  # Non-critical — will retry next cycle

        if corrections > 0:
            self._persist()

        # Stream pipe reconciliation — ensure all live panes have pipes attached.
        # Runs every reconcile cycle. Re-attaches pipes for crashed handlers.
        try:
            all_tmux_panes: set[str] = set()
            r = subprocess.run(
                [*TMUX_CMD, "list-panes", "-a", "-F", "#{pane_id}"],
                capture_output=True, timeout=1.0,
            )
            if r.returncode == 0:
                all_tmux_panes = {line.strip() for line in r.stdout.decode().splitlines() if line.strip()}
            stream_corrections = self.stream.reconcile(all_tmux_panes)
            corrections += stream_corrections
        except Exception:
            pass  # Non-critical — will retry next cycle

        # Prune decayed focus signals
        self.focus.prune_stale()

        return corrections

    # ---- Status -----------------------------------------------------------

    def get_status(self) -> dict:
        """Return structured status dict for --check and IPC queries."""
        now = time.time()
        return {
            "panes": {
                pid: {
                    "state": ps.state_name,
                    "color": ps.color,
                    "glyph": ps.glyph,
                    "persona": ps.persona,
                    "age_seconds": round(now - ps.updated_at, 1),
                    "startup_guard_active": self._in_startup_window(pid),
                    "subagent_depth": ps.subagent_depth,
                    "provider": ps.provider,
                    "active_agent_count": ps.active_agent_count,
                    "last_source": ps.last_source,
                    "event_history": (ps.event_history or [])[-5:],
                }
                for pid, ps in self.panes.items()
            },
            "pane_count": len(self.panes),
            "stream_pipes": self.stream.attached_count,
            "theme_path": str(THEME_PATH),
            "state_path": str(STATE_PATH),
            "socket_path": str(ARBITER_SOCKET),
            "rejected_events": getattr(self, "rejected_events", {}),
        }


# ---------------------------------------------------------------------------
# ArbiterServer — asyncio wrapper
# ---------------------------------------------------------------------------

class ArbiterServer:
    """Async Unix socket server wrapping TmuxArbiter."""

    def __init__(self) -> None:
        self.arbiter = TmuxArbiter()
        self._running = False

    async def start(self) -> None:
        """Start the arbiter. Blocks until shutdown signal."""
        TMUX_DIR.mkdir(parents=True, exist_ok=True)

        # Clean up stale socket from previous run
        ARBITER_SOCKET.unlink(missing_ok=True)

        ARBITER_PID.write_text(str(os.getpid()))
        self._running = True

        server = await asyncio.start_unix_server(
            self._handle_client, path=str(ARBITER_SOCKET), limit=MAX_EVENT_BYTES
        )

        _log(
            f"tmux-arbiter ready (pid={os.getpid()}, "
            f"panes={len(self.arbiter.panes)}, socket={ARBITER_SOCKET})"
        )
        _log(
            f"tmux server: {TMUX_SOCKET or '(bare/default)'} "
            f"[{TMUX_SOCKET_REASON}] — cmd={' '.join(TMUX_CMD)}"
        )

        _setup_signals(self)

        try:
            async with server:
                await self._main_loop()
        finally:
            self._shutdown()

    async def _main_loop(self) -> None:
        """Main loop — runs reconciliation on a timer."""
        _reconcile_count = 0
        last_reconcile = 0.0
        while self._running:
            await asyncio.sleep(min(1.0, self.arbiter.config.reconcile_interval))
            if not self._running:
                break
            self.arbiter.reconcile_lifecycle()
            if time.monotonic() - last_reconcile < self.arbiter.config.reconcile_interval:
                continue
            last_reconcile = time.monotonic()
            corrections = self.arbiter.reconcile()
            _reconcile_count += 1
            if corrections > 0 or _reconcile_count % 100 == 0:
                _log(
                    f"reconcile #{_reconcile_count}: {corrections} corrections, "
                    f"{len(self.arbiter.panes)} panes tracked"
                )

    async def _handle_client(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        """Handle one client connection.

        Protocol: client sends one JSON line, server processes it.
        For event messages (from hook.sh): fire-and-forget, no response.
        For status/control commands: send one JSON response line.
        """
        try:
            raw = await asyncio.wait_for(reader.readline(), timeout=1.0)
            if not raw:
                return
            msg = json.loads(raw.decode().strip())
            if not isinstance(msg, dict):
                _log("rejected IPC message: expected object")
                return
            response = await self._dispatch(msg)
            if response is not None:
                writer.write((json.dumps(response) + "\n").encode())
                await writer.drain()
        except asyncio.TimeoutError:
            pass  # Client connected but sent nothing — ignore silently
        except json.JSONDecodeError as e:
            _log(f"bad JSON from client: {e}")
        except Exception as e:
            _log(f"client handler error: {e}")
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass

    async def _dispatch(self, msg: dict) -> Optional[dict]:
        """Route a message to the appropriate handler.

        Returns None for fire-and-forget event messages (from hook.sh).
        Returns a dict for status/control commands (--check, --stop via IPC).
        """
        msg_type = msg.get("type", "")

        if msg_type == "event":
            # Fire-and-forget from hook.sh
            # Format: {"type":"event","event":"UserPromptSubmit","pane":"%42","data":{...}}
            reason = self.arbiter.validate_hook(msg)
            if reason:
                self.arbiter.rejected_events[reason] = self.arbiter.rejected_events.get(reason, 0) + 1
                data = msg.get("data") if isinstance(msg.get("data"), dict) else {}
                source = msg.get("source") if isinstance(msg.get("source"), dict) else {}
                _log(f"drop event={msg.get('event')!r} pane={msg.get('pane')!r} "
                     f"reason={reason} session={data.get('session_id')!r} "
                     f"socket={source.get('socket_path')!r}")
                return {"type": "event_rejected", "reason": reason}
            binding = self.arbiter.lifecycle_bindings[msg["pane"]]
            _log(f"bound event={msg['event']} pane={msg['pane']} "
                 f"provider={binding['provider']} session={binding['session_id']} "
                 f"process={binding['process_id']}:{binding['process_start']}")
            self.arbiter.apply_event(
                event=msg.get("event", ""),
                pane_id=msg.get("pane", ""),
                data=msg.get("data", {}),
            )
            return None  # no response — client (socat) already closed

        elif msg_type == "status":
            return self.arbiter.get_status()

        elif msg_type == "window_refresh":
            pane_id = msg.get("pane", "")
            if not isinstance(pane_id, str) or not re.fullmatch(r"%\d+", pane_id):
                return {"type": "error", "message": "invalid pane"}
            try:
                result = subprocess.run(
                    [*TMUX_CMD, "display-message", "-p", "-t", pane_id, "#{socket_path}"],
                    capture_output=True, text=True, timeout=0.3,
                )
                if result.returncode or msg.get("socket_path") != result.stdout.strip():
                    return {"type": "error", "message": "tmux server mismatch"}
            except (OSError, subprocess.TimeoutExpired):
                return {"type": "error", "message": "tmux binding unavailable"}
            if msg.get("mark_read") is True:
                self.arbiter.apply_event("PaneRead", pane_id, {})
            TmuxWriter.refresh_window(pane_id)
            return {"type": "window_refresh_ack"}

        elif msg_type == "shutdown":
            _log("shutdown via IPC")
            self._running = False
            return {"type": "shutdown_ack"}

        elif msg_type == "focus":
            action = msg.get("action", "status")
            pane_id = msg.get("pane", "")

            if action == "set":
                key = msg.get("key", "")
                if key and pane_id:
                    self.arbiter.focus.observe("explicit", key, pane_id)
                    result = self.arbiter.focus.resolve_focus(pane_id)
                    emoji = result.emoji
                    if pane_id in self.arbiter.panes:
                        pane = self.arbiter.panes[pane_id]
                        self.arbiter.panes[pane_id] = replace(
                            pane, focus_venture=result.slot1 if result.slot1.startswith("venture:") else "",
                            focus_plugin=next((k for k in (result.slot1, result.slot2) if k.startswith("plugin:")), ""),
                            focus_emoji=result.emoji,
                            focus_signals=[asdict(s) for s in self.arbiter.focus._signals.get(pane_id, {}).values()],
                        )
                        TmuxWriter.write_state(self.arbiter.panes[pane_id])
                        self.arbiter._persist()
                    _log(f"focus set: {pane_id} -> {key} (explicit)")
                    return {"type": "focus_ack", "action": "set", "key": key, "emoji": emoji}
                return {"type": "error", "message": "missing key or pane"}

            elif action == "clear":
                if pane_id:
                    self.arbiter.focus.clear_explicit(pane_id)
                    result = self.arbiter.focus.resolve_focus(pane_id)
                    emoji = result.emoji
                    if pane_id in self.arbiter.panes:
                        pane = self.arbiter.panes[pane_id]
                        self.arbiter.panes[pane_id] = replace(
                            pane, focus_venture=result.slot1 if result.slot1.startswith("venture:") else "",
                            focus_plugin=next((k for k in (result.slot1, result.slot2) if k.startswith("plugin:")), ""),
                            focus_emoji=result.emoji,
                            focus_signals=[asdict(s) for s in self.arbiter.focus._signals.get(pane_id, {}).values()],
                        )
                        TmuxWriter.write_state(self.arbiter.panes[pane_id])
                        self.arbiter._persist()
                    _log(f"focus clear: {pane_id} (reverted to auto)")
                    return {"type": "focus_ack", "action": "clear", "emoji": emoji}
                return {"type": "error", "message": "missing pane"}

            else:  # status (default)
                if pane_id:
                    status = self.arbiter.focus.get_focus_status(pane_id)
                    return {"type": "focus_status", **status}
                return {"type": "error", "message": "missing pane"}

        else:
            _log(f"unknown message type: {msg_type!r}")
            return {"type": "error", "message": f"unknown type: {msg_type!r}"}

    def _shutdown(self) -> None:
        """Clean up socket and PID file on exit."""
        _log("shutting down")
        try:
            ARBITER_SOCKET.unlink(missing_ok=True)
            ARBITER_PID.unlink(missing_ok=True)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Signal handlers
# ---------------------------------------------------------------------------

def _setup_signals(server: ArbiterServer) -> None:
    """Register SIGTERM/SIGINT for graceful shutdown and SIGHUP for config reload."""
    loop = asyncio.get_running_loop()

    def handle_shutdown(sig: int) -> None:
        _log(f"signal {sig} received — shutting down")
        server._running = False

    def handle_reload(_sig: int) -> None:
        old = server.arbiter.config
        server.arbiter.config = Config.load()
        server.arbiter.focus.DECAY_HALF_LIFE_FILE = server.arbiter.config.focus_decay_half_life_file
        server.arbiter.focus.DECAY_HALF_LIFE_CWD = server.arbiter.config.focus_decay_half_life_cwd
        server.arbiter.focus.DECAY_HALF_LIFE_INTENT = server.arbiter.config.focus_decay_half_life_intent
        _log(f"SIGHUP: config reloaded from {CONFIG_PATH}")
        # Log changed values
        for f in fields(Config):
            old_val = getattr(old, f.name)
            new_val = getattr(server.arbiter.config, f.name)
            if old_val != new_val:
                _log(f"  config: {f.name} {old_val} → {new_val}")

    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, handle_shutdown, sig)
    loop.add_signal_handler(signal.SIGHUP, handle_reload, signal.SIGHUP)


# ---------------------------------------------------------------------------
# CLI commands
# ---------------------------------------------------------------------------

def _check() -> None:
    """Query running daemon for status. Exits 0 if running, 1 if not."""
    if not ARBITER_SOCKET.exists():
        print("tmux-arbiter: not running (no socket)")
        sys.exit(1)
    try:
        import socket as _socket
        with _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM) as s:
            s.settimeout(2)
            s.connect(str(ARBITER_SOCKET))
            s.sendall(b'{"type":"status"}\n')
            buf = b""
            while chunk := s.recv(4096):
                buf += chunk
                if b"\n" in buf:
                    break
        resp = json.loads(buf.split(b"\n")[0])
        pid_str = ARBITER_PID.read_text().strip() if ARBITER_PID.exists() else "?"
        print(f"tmux-arbiter: running (pid={pid_str})")
        print(f"  panes tracked : {resp.get('pane_count', 0)}")
        print(f"  stream pipes  : {resp.get('stream_pipes', 0)}")
        print(f"  state file    : {resp.get('state_path', '?')}")
        print(f"  socket        : {resp.get('socket_path', '?')}")
        panes = resp.get("panes", {})
        if panes:
            print()
            for pid, info in panes.items():
                guard = " [startup-guard]" if info.get("startup_guard_active") else ""
                persona = f" {info['persona']}" if info.get("persona") else ""
                age = info.get("age_seconds", "?")
                state = info.get("state", "?")
                depth = info.get("subagent_depth", 0)
                source = info.get("last_source", "?")
                depth_str = f" depth={depth}" if depth else ""
                print(f"  {pid}{persona}: {state} ({age}s ago) src={source}{depth_str}{guard}")
                events = info.get("event_history", [])
                if events:
                    for ev in events:
                        ts = time.strftime("%H:%M:%S", time.localtime(ev.get("t", 0)))
                        print(f"    {ts} {ev.get('event','?')}: {ev.get('from','?')}→{ev.get('to','?')} [{ev.get('src','?')}]")
        else:
            print("  (no panes tracked yet)")
    except Exception as e:
        print(f"tmux-arbiter: error ({e})")
        sys.exit(1)


def _stop() -> None:
    """Stop the running daemon via SIGTERM."""
    if not ARBITER_PID.exists():
        print("tmux-arbiter: not running (no PID file)")
        sys.exit(1)
    try:
        pid = int(ARBITER_PID.read_text().strip())
        os.kill(pid, signal.SIGTERM)
        print(f"tmux-arbiter: stopped (pid={pid})")
    except (ValueError, ProcessLookupError):
        print("tmux-arbiter: stale PID file")
        ARBITER_PID.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    if "--check" in sys.argv:
        _check()
    elif "--stop" in sys.argv:
        _stop()
    else:
        # Daemon mode — flock guard prevents duplicate instances
        import fcntl
        LOCK_PATH = ARBITER_PID.with_suffix(".lock")
        LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
        # Hold lock for process lifetime — NOT in a with block.
        # The lock must outlive asyncio.run(); a with block would release it too early.
        lockfd = open(str(LOCK_PATH), "w")
        try:
            fcntl.flock(lockfd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (BlockingIOError, OSError):
            _log("already running (lock held)")
            print("tmux-arbiter: already running")
            lockfd.close()
            sys.exit(1)
        try:
            server = ArbiterServer()
            asyncio.run(server.start())
        finally:
            lockfd.close()


if __name__ == "__main__":
    main()
