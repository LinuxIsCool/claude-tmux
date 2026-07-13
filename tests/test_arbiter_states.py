"""Hypothesis stateful tests + focused unit tests for tmux-arbiter state machine.

Tests the state machine logic in TmuxArbiter.apply_event() without any tmux/IO.
"""
from __future__ import annotations

import importlib.util
import json
import sys
import time
from dataclasses import replace
from pathlib import Path
import pytest  # noqa: F401 — used by test classes
from hypothesis import HealthCheck, settings
from hypothesis.stateful import (
    RuleBasedStateMachine,
    initialize,
    invariant,
    precondition,
    rule,
)

# ---------------------------------------------------------------------------
# Import tmux-arbiter.py (hyphenated filename requires importlib)
# ---------------------------------------------------------------------------

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
THEME_PATH = Path(__file__).resolve().parent.parent / "theme.json"

spec = importlib.util.spec_from_file_location("tmux_arbiter", SCRIPTS_DIR / "tmux-arbiter.py")
assert spec is not None and spec.loader is not None, "Failed to load tmux-arbiter.py"
_mod = importlib.util.module_from_spec(spec)
sys.modules["tmux_arbiter"] = _mod
spec.loader.exec_module(_mod)

HOME = str(Path.home())


@pytest.fixture(autouse=True)
def _focus_fixture_data(tmp_path, monkeypatch):
    """Self-contained venture/alias/canonical data for the FocusEngine.

    The engine reads three machine-local files; without this fixture every
    focus test silently depends on host state (and breaks on any other
    machine, including CI).
    """
    ventures = {
        "legion": [f"{HOME}/.claude"],
        "acme-corp": [f"{HOME}/Workspace/acme"],
        "beta-labs": [f"{HOME}/Workspace/beta"],
    }
    vpath = tmp_path / "ventures.json"
    vpath.write_text(json.dumps(ventures))
    aliases = {
        "ventures": {"acme": "acme-corp", "AC": "acme-corp", "beta": "beta-labs"},
        "plugins": {"voice": "claude-voice", "hippo": "claude-hippo"},
    }
    apath = tmp_path / "aliases.json"
    apath.write_text(json.dumps(aliases))
    vdir = tmp_path / "ventures-active"
    vdir.mkdir()
    for slug in ("acme-corp", "beta-labs", "gamma-fund"):
        (vdir / f"{slug}.md").write_text(f"# {slug}\n")
    monkeypatch.setattr(_mod, "VENTURES_PATH", vpath)
    monkeypatch.setattr(_mod, "ALIASES_PATH", apath)
    monkeypatch.setattr(_mod, "VENTURES_DIR", vdir)


PaneState = _mod.PaneState
TmuxArbiter = _mod.TmuxArbiter
TmuxWriter = _mod.TmuxWriter
ThemeLoader = _mod.ThemeLoader
Config = _mod.Config

# ---------------------------------------------------------------------------
# Load theme for color assertions
# ---------------------------------------------------------------------------

_THEME = json.loads(THEME_PATH.read_text())

VALID_STATES = {"idle", "working", "agents", "asking", "error", "unread", "nudge"}
VALID_SOURCES = {"hook", "reconcile", ""}

# Map state → expected hex color
STATE_COLORS: dict[str, str] = {}
for sname, sdef in _THEME["states"].items():
    semantic = sdef["semantic"]
    STATE_COLORS[sname] = _THEME["semantic_colors"][semantic]

# The 14 hook events (excluding Notification, PaneRead, SessionStart/End which are special)
HOOK_EVENTS = [
    "UserPromptSubmit",
    "PreToolUse",
    "PostToolUseFailure",
    "Stop",
    "StopFailure",
    "PermissionRequest",
    "SubagentStart",
    "SubagentStop",
    "TeammateIdle",
    "TaskCompleted",
    "PreCompact",
    "PostCompact",
]

PANE = "%99"


# ---------------------------------------------------------------------------
# Test helper: create arbiter with all IO mocked out
# ---------------------------------------------------------------------------

def _make_arbiter() -> TmuxArbiter:
    """Create an arbiter with all IO mocked out."""
    # Monkey-patch IO methods
    TmuxWriter.write_state = classmethod(lambda cls, pane: None)
    TmuxWriter.clear_state = classmethod(lambda cls, pane_id: None)
    TmuxWriter.pane_exists = staticmethod(lambda pane_id: True)

    arb = object.__new__(TmuxArbiter)
    arb.config = Config()
    arb.theme = ThemeLoader(THEME_PATH)
    arb.emoji = _mod.EmojiLookup()
    arb.focus = _mod.FocusEngine(arb.emoji)
    arb.panes = {}
    arb.stream = _mod.StreamManager.__new__(_mod.StreamManager)
    arb.stream._attached = set()
    arb.stream._enabled = False
    return arb


def _start_session(arb: TmuxArbiter, pane_id: str = PANE) -> None:
    """Fire SessionStart and bypass startup guard."""
    arb.apply_event("SessionStart", pane_id, {})
    # Move session_start_at into the past to bypass the startup guard
    pane = arb.panes[pane_id]
    arb.panes[pane_id] = replace(pane, session_start_at=time.time() - 10)


# ---------------------------------------------------------------------------
# Hypothesis RuleBasedStateMachine
# ---------------------------------------------------------------------------

class ArbiterStateMachine(RuleBasedStateMachine):
    """Exercise all 14 events + PaneRead + SessionStart/End in random order."""

    def __init__(self):
        super().__init__()
        self.arb = _make_arbiter()
        self._session_active = False
        self._persona_was_set = False

    @initialize()
    def start_session(self):
        # Mock focus so the pane is focused (default: no unread transitions)
        self.arb._get_focused_pane = lambda: PANE
        self.arb.apply_event("SessionStart", PANE, {})
        # Bypass startup guard
        pane = self.arb.panes[PANE]
        self.arb.panes[PANE] = replace(pane, session_start_at=time.time() - 10)
        self._session_active = True
        self._persona_was_set = False

    # --- Rules for each hook event ---

    @precondition(lambda self: self._session_active)
    @rule()
    def user_prompt_submit(self):
        self.arb.apply_event("UserPromptSubmit", PANE, {})

    @precondition(lambda self: self._session_active)
    @rule()
    def pre_tool_use(self):
        self.arb.apply_event("PreToolUse", PANE, {})

    @precondition(lambda self: self._session_active)
    @rule()
    def post_tool_use_failure(self):
        self.arb.apply_event("PostToolUseFailure", PANE, {})

    @precondition(lambda self: self._session_active)
    @rule()
    def stop(self):
        self.arb.apply_event("Stop", PANE, {})

    @precondition(lambda self: self._session_active)
    @rule()
    def stop_failure(self):
        self.arb.apply_event("StopFailure", PANE, {})

    @precondition(lambda self: self._session_active)
    @rule()
    def permission_request(self):
        self.arb.apply_event("PermissionRequest", PANE, {})

    @precondition(lambda self: self._session_active)
    @rule()
    def subagent_start(self):
        self.arb.apply_event("SubagentStart", PANE, {})

    @precondition(lambda self: self._session_active)
    @rule()
    def subagent_stop(self):
        self.arb.apply_event("SubagentStop", PANE, {})

    @precondition(lambda self: self._session_active)
    @rule()
    def teammate_idle(self):
        self.arb.apply_event("TeammateIdle", PANE, {})

    @precondition(lambda self: self._session_active)
    @rule()
    def task_completed(self):
        self.arb.apply_event("TaskCompleted", PANE, {})

    @precondition(lambda self: self._session_active)
    @rule()
    def pre_compact(self):
        self.arb.apply_event("PreCompact", PANE, {})

    @precondition(lambda self: self._session_active)
    @rule()
    def post_compact(self):
        self.arb.apply_event("PostCompact", PANE, {})

    @precondition(lambda self: self._session_active)
    @rule()
    def pane_read(self):
        self.arb.apply_event("PaneRead", PANE, {})

    @precondition(lambda self: self._session_active)
    @rule()
    def stop_while_unfocused(self):
        """Temporarily mock focus to a different pane, then fire Stop."""
        old_focus = self.arb._get_focused_pane
        self.arb._get_focused_pane = lambda: "%other"
        self.arb.apply_event("Stop", PANE, {})
        self.arb._get_focused_pane = old_focus

    @precondition(lambda self: self._session_active)
    @rule()
    def compact_session_start(self):
        """SessionStart with source=compact should be a no-op."""
        self.arb.apply_event("SessionStart", PANE, {"source": "compact"})

    @precondition(lambda self: self._session_active)
    @rule()
    def session_end(self):
        self.arb.apply_event("SessionEnd", PANE, {})
        self._session_active = False
        self._persona_was_set = False

    @precondition(lambda self: not self._session_active)
    @rule()
    def restart_session(self):
        self.arb._get_focused_pane = lambda: PANE
        self.arb.apply_event("SessionStart", PANE, {})
        pane = self.arb.panes[PANE]
        self.arb.panes[PANE] = replace(pane, session_start_at=time.time() - 10)
        self._session_active = True
        self._persona_was_set = False

    # --- Invariants ---

    @invariant()
    def subagent_depth_non_negative(self):
        """Invariant 1: subagent_depth >= 0"""
        pane = self.arb.panes.get(PANE)
        if pane:
            assert pane.subagent_depth >= 0, (
                f"subagent_depth went negative: {pane.subagent_depth}"
            )

    @invariant()
    def color_matches_state(self):
        """Invariant 2: color matches the expected hex for the state."""
        pane = self.arb.panes.get(PANE)
        if pane and pane.state_name in STATE_COLORS:
            expected = STATE_COLORS[pane.state_name]
            assert pane.color == expected, (
                f"state={pane.state_name} has color={pane.color}, expected={expected}"
            )

    @invariant()
    def persona_never_blanked(self):
        """Invariant 3: once persona is set within a session, it stays set."""
        pane = self.arb.panes.get(PANE)
        if pane:
            if pane.persona:
                self._persona_was_set = True
            if self._persona_was_set and self._session_active:
                assert pane.persona, "persona was blanked after being set"

    @invariant()
    def session_end_removes_pane(self):
        """Invariant 4: after SessionEnd, pane is not in the store."""
        if not self._session_active:
            assert PANE not in self.arb.panes, "pane still in store after SessionEnd"

    @invariant()
    def state_name_is_valid(self):
        """Invariant 5: state_name is one of the known states."""
        pane = self.arb.panes.get(PANE)
        if pane:
            assert pane.state_name in VALID_STATES, (
                f"invalid state_name: {pane.state_name}"
            )

    @invariant()
    def event_history_bounded(self):
        """Invariant 6: event_history never exceeds max_event_history."""
        pane = self.arb.panes.get(PANE)
        if pane:
            assert len(pane.event_history) <= self.arb.config.max_event_history, (
                f"event_history too long: {len(pane.event_history)}"
            )

    @invariant()
    def last_source_is_valid(self):
        """Invariant 7: last_source is one of the valid values."""
        pane = self.arb.panes.get(PANE)
        if pane:
            assert pane.last_source in VALID_SOURCES, (
                f"invalid last_source: {pane.last_source!r}"
            )

    @invariant()
    def working_always_green(self):
        """Invariant 8: working state always has the active (green) color."""
        pane = self.arb.panes.get(PANE)
        if pane and pane.state_name == "working":
            expected = STATE_COLORS["working"]
            assert pane.color == expected, (
                f"working state has wrong color: {pane.color}, expected {expected}"
            )


TestArbiterStateMachine = ArbiterStateMachine.TestCase
TestArbiterStateMachine.settings = settings(
    max_examples=500,
    stateful_step_count=50,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)


# ===========================================================================
# Focused unit tests
# ===========================================================================


class TestStartupGuard:
    """Startup guard suppresses working transitions within N seconds of SessionStart."""

    def test_pre_tool_use_suppressed_during_guard(self):
        arb = _make_arbiter()
        arb._get_focused_pane = lambda: PANE
        arb.apply_event("SessionStart", PANE, {})
        # Don't bypass the guard — session_start_at is fresh
        arb.apply_event("PreToolUse", PANE, {})
        pane = arb.panes[PANE]
        assert pane.state_name == "idle", (
            f"expected idle during startup guard, got {pane.state_name}"
        )

    def test_user_prompt_allowed_after_guard(self):
        arb = _make_arbiter()
        arb._get_focused_pane = lambda: PANE
        arb.apply_event("SessionStart", PANE, {})
        # Move session_start_at into the past
        pane = arb.panes[PANE]
        arb.panes[PANE] = replace(pane, session_start_at=time.time() - 10)
        arb.apply_event("UserPromptSubmit", PANE, {})
        pane = arb.panes[PANE]
        assert pane.state_name == "working", (
            f"expected working after guard elapsed, got {pane.state_name}"
        )


class TestUnreadState:
    """Unread state triggers when turn ends on unfocused pane."""

    def test_stop_unfocused_produces_unread(self):
        arb = _make_arbiter()
        arb._get_focused_pane = lambda: "%other"
        _start_session(arb)
        arb.apply_event("UserPromptSubmit", PANE, {})
        arb.apply_event("Stop", PANE, {})
        pane = arb.panes[PANE]
        assert pane.state_name == "unread", (
            f"expected unread when unfocused, got {pane.state_name}"
        )

    def test_pane_read_transitions_unread_to_idle(self):
        arb = _make_arbiter()
        arb._get_focused_pane = lambda: "%other"
        _start_session(arb)
        arb.apply_event("UserPromptSubmit", PANE, {})
        arb.apply_event("Stop", PANE, {})
        assert arb.panes[PANE].state_name == "unread"

        arb.apply_event("PaneRead", PANE, {})
        pane = arb.panes[PANE]
        assert pane.state_name == "idle", (
            f"expected idle after PaneRead, got {pane.state_name}"
        )
        assert pane.color == STATE_COLORS["idle"], (
            f"expected idle color {STATE_COLORS['idle']}, got {pane.color}"
        )


class TestSubagentDepth:
    """Subagent depth tracking and its effect on state transitions."""

    def test_subagent_depth_tracks_correctly(self):
        arb = _make_arbiter()
        arb._get_focused_pane = lambda: PANE
        _start_session(arb)

        arb.apply_event("SubagentStart", PANE, {})
        assert arb.panes[PANE].subagent_depth == 1

        arb.apply_event("SubagentStart", PANE, {})
        assert arb.panes[PANE].subagent_depth == 2

        arb.apply_event("SubagentStop", PANE, {})
        assert arb.panes[PANE].subagent_depth == 1

        arb.apply_event("SubagentStop", PANE, {})
        assert arb.panes[PANE].subagent_depth == 0

    def test_stop_returns_to_agents_when_depth_positive(self):
        arb = _make_arbiter()
        arb._get_focused_pane = lambda: PANE
        _start_session(arb)

        arb.apply_event("SubagentStart", PANE, {})
        assert arb.panes[PANE].state_name == "agents"

        arb.apply_event("PermissionRequest", PANE, {})
        assert arb.panes[PANE].state_name == "asking"

        arb.apply_event("Stop", PANE, {})
        pane = arb.panes[PANE]
        assert pane.state_name == "agents", (
            f"expected agents (depth still 1), got {pane.state_name}"
        )


class TestBypassPermissions:
    """PermissionRequest with bypassPermissions should NOT show asking."""

    def test_bypass_permission_stays_agents_at_depth(self):
        """When subagent is active and permissions are bypassed, stay agents."""
        arb = _make_arbiter()
        arb._get_focused_pane = lambda: PANE
        _start_session(arb)

        arb.apply_event("SubagentStart", PANE, {})
        assert arb.panes[PANE].state_name == "agents"

        arb.apply_event("PermissionRequest", PANE, {"permission_mode": "bypassPermissions"})
        pane = arb.panes[PANE]
        assert pane.state_name == "agents", (
            f"expected agents with bypassPermissions, got {pane.state_name}"
        )
        assert pane.color == STATE_COLORS["agents"], (
            f"expected agents color, got {pane.color}"
        )

    def test_bypass_permission_stays_working_at_depth_zero(self):
        """At depth 0 with bypassPermissions, stay on current state."""
        arb = _make_arbiter()
        arb._get_focused_pane = lambda: PANE
        _start_session(arb)

        arb.apply_event("UserPromptSubmit", PANE, {})
        assert arb.panes[PANE].state_name == "working"

        arb.apply_event("PermissionRequest", PANE, {"permission_mode": "bypassPermissions"})
        pane = arb.panes[PANE]
        assert pane.state_name == "working", (
            f"expected working with bypassPermissions at depth 0, got {pane.state_name}"
        )

    def test_real_permission_still_shows_asking(self):
        """Without bypassPermissions, PermissionRequest should show asking normally."""
        arb = _make_arbiter()
        arb._get_focused_pane = lambda: PANE
        _start_session(arb)

        arb.apply_event("SubagentStart", PANE, {})
        arb.apply_event("PermissionRequest", PANE, {})
        pane = arb.panes[PANE]
        assert pane.state_name == "asking", (
            f"expected asking for real permission request, got {pane.state_name}"
        )

    def test_real_permission_with_default_mode(self):
        """permission_mode=default should still show asking."""
        arb = _make_arbiter()
        arb._get_focused_pane = lambda: PANE
        _start_session(arb)

        arb.apply_event("PermissionRequest", PANE, {"permission_mode": "default"})
        pane = arb.panes[PANE]
        assert pane.state_name == "asking", (
            f"expected asking for default permission mode, got {pane.state_name}"
        )


class TestCompactionGuard:
    """SessionStart with source=compact is a no-op."""

    def test_compact_session_start_is_noop(self):
        arb = _make_arbiter()
        arb._get_focused_pane = lambda: PANE
        _start_session(arb)
        arb.apply_event("UserPromptSubmit", PANE, {})
        assert arb.panes[PANE].state_name == "working"

        arb.apply_event("SessionStart", PANE, {"source": "compact"})
        pane = arb.panes[PANE]
        assert pane.state_name == "working", (
            f"compact SessionStart should be no-op, state changed to {pane.state_name}"
        )


# ---------------------------------------------------------------------------
# EmojiLookup tests
# ---------------------------------------------------------------------------

class TestEmojiLookup:
    """Tests for EmojiLookup — mtime-cached emoji_flat.json reader."""

    def test_get_existing_key(self, tmp_path):
        flat = {"event:session-start": "🌅", "tool:read": "📖"}
        p = tmp_path / "emoji_flat.json"
        p.write_text(json.dumps(flat))
        lookup = _mod.EmojiLookup(str(p))
        assert lookup.get("event:session-start") == "🌅"
        assert lookup.get("tool:read") == "📖"

    def test_get_missing_key_returns_fallback(self, tmp_path):
        flat = {"event:session-start": "🌅"}
        p = tmp_path / "emoji_flat.json"
        p.write_text(json.dumps(flat))
        lookup = _mod.EmojiLookup(str(p))
        assert lookup.get("event:nonexistent") == ""
        assert lookup.get("event:nonexistent", "❓") == "❓"

    def test_missing_file_returns_fallback(self, tmp_path):
        lookup = _mod.EmojiLookup(str(tmp_path / "nope.json"))
        assert lookup.get("event:session-start") == ""

    def test_corrupt_json_returns_fallback(self, tmp_path):
        p = tmp_path / "emoji_flat.json"
        p.write_text("{bad json")
        lookup = _mod.EmojiLookup(str(p))
        assert lookup.get("event:session-start") == ""

    def test_mtime_reload(self, tmp_path):
        p = tmp_path / "emoji_flat.json"
        p.write_text(json.dumps({"event:stop": "🏁"}))
        lookup = _mod.EmojiLookup(str(p))
        assert lookup.get("event:stop") == "🏁"
        import os, time as _t
        _t.sleep(0.05)
        p.write_text(json.dumps({"event:stop": "🔴"}))
        os.utime(p, None)
        assert lookup.get("event:stop") == "🔴"


# ---------------------------------------------------------------------------
# Hook-to-emoji resolution tests
# ---------------------------------------------------------------------------

class TestHookToEventKey:
    def test_simple_event(self):
        assert _mod.hook_to_event_key("Stop") == "event:stop"

    def test_two_word_event(self):
        assert _mod.hook_to_event_key("SessionStart") == "event:session-start"

    def test_three_word_event(self):
        assert _mod.hook_to_event_key("PreToolUse") == "event:pre-tool-use"

    def test_four_word_event(self):
        assert _mod.hook_to_event_key("PostToolUseFailure") == "event:post-tool-use-failure"

    def test_user_prompt_submit(self):
        assert _mod.hook_to_event_key("UserPromptSubmit") == "event:user-prompt-submit"

    def test_all_25_hooks(self):
        hooks = [
            "SessionStart", "SessionEnd", "UserPromptSubmit", "Notification",
            "Elicitation", "ElicitationResult", "PreToolUse", "PostToolUse",
            "PostToolUseFailure", "PermissionRequest", "SubagentStart",
            "SubagentStop", "TeammateIdle", "TaskCreated", "TaskCompleted",
            "Stop", "StopFailure", "InstructionsLoaded", "ConfigChange",
            "CwdChanged", "FileChanged", "WorktreeCreate", "WorktreeRemove",
            "PreCompact", "PostCompact",
        ]
        for h in hooks:
            key = _mod.hook_to_event_key(h)
            assert key.startswith("event:"), f"{h} → {key} doesn't start with event:"
            assert key == key.lower(), f"{h} → {key} is not lowercase"


class TestRefinementKey:
    def test_pre_tool_use_with_tool_name(self):
        assert _mod.refinement_key("PreToolUse", {"tool_name": "Read"}) == "tool:read"

    def test_pre_tool_use_bash(self):
        assert _mod.refinement_key("PreToolUse", {"tool_name": "Bash"}) == "tool:bash"

    def test_notification_with_type(self):
        assert _mod.refinement_key("Notification", {"notification_type": "idle_prompt"}) == "notify:idle_prompt"

    def test_subagent_start_with_persona(self):
        assert _mod.refinement_key("SubagentStart", {"agent_type": "claude-personas:matt"}) == "persona:matt"

    def test_session_start_with_source(self):
        assert _mod.refinement_key("SessionStart", {"source": "resume"}) == "source:resume"

    def test_session_end_with_reason(self):
        assert _mod.refinement_key("SessionEnd", {"reason": "exit"}) == "reason:exit"

    def test_stop_with_source(self):
        assert _mod.refinement_key("Stop", {"source": "clear"}) == "source:clear"

    def test_no_refinement_for_user_prompt(self):
        assert _mod.refinement_key("UserPromptSubmit", {"prompt": "hello"}) is None

    def test_no_refinement_for_unknown_hook(self):
        assert _mod.refinement_key("FileChanged", {"path": "/foo"}) is None

    def test_empty_tool_name_returns_none(self):
        assert _mod.refinement_key("PreToolUse", {"tool_name": ""}) is None

    def test_missing_field_returns_none(self):
        assert _mod.refinement_key("PreToolUse", {}) is None

    def test_permission_request_with_tool(self):
        assert _mod.refinement_key("PermissionRequest", {"tool_name": "Bash"}) == "tool:bash"

    def test_post_tool_use_failure_with_tool(self):
        assert _mod.refinement_key("PostToolUseFailure", {"tool_name": "Edit"}) == "tool:edit"


# ---------------------------------------------------------------------------
# Slot toggle tests
# ---------------------------------------------------------------------------

class TestHookEmojiSlots:
    """Tests for hook_emoji_slots config toggle."""

    def test_mode2_shows_both_slots(self):
        """Default mode 2: event + refinement (mock lookup — host-independent)."""
        arb = _make_arbiter()
        arb.config = replace(arb.config, hook_emoji_slots=2)
        import types
        _lookup = {"event:pre-tool-use": "🔧", "tool:read": "📖"}
        _get = lambda key, fallback="": _lookup.get(key, fallback)
        arb.emoji = types.SimpleNamespace(get=_get, get_tmux=_get)
        arb.apply_event("PreToolUse", "%99", {"tool_name": "Read"})
        pane = arb.panes["%99"]
        assert "🔧" in pane.hook_emoji and "📖" in pane.hook_emoji

    def test_mode1_prefers_refinement(self):
        """Mode 1: refinement when available, else event."""
        arb = _make_arbiter()
        arb.config = replace(arb.config, hook_emoji_slots=1)
        # Create a mock emoji lookup that returns known values
        import types
        _lookup = {"event:pre-tool-use": "🔧", "tool:read": "📖"}
        _get = lambda key, fallback="": _lookup.get(key, fallback)
        arb.emoji = types.SimpleNamespace(get=_get, get_tmux=_get)
        arb.apply_event("PreToolUse", "%99", {"tool_name": "Read"})
        pane = arb.panes["%99"]
        assert pane.hook_emoji == "📖"  # refinement wins

    def test_mode1_falls_back_to_event(self):
        """Mode 1: no refinement available, shows event."""
        arb = _make_arbiter()
        arb.config = replace(arb.config, hook_emoji_slots=1)
        import types
        _lookup = {"event:user-prompt-submit": "📩"}
        _get = lambda key, fallback="": _lookup.get(key, fallback)
        arb.emoji = types.SimpleNamespace(get=_get, get_tmux=_get)
        arb.apply_event("UserPromptSubmit", "%99", {})
        pane = arb.panes["%99"]
        assert pane.hook_emoji == "📩"  # event fallback


# ---------------------------------------------------------------------------
# Comprehensive hook-to-emoji integration tests (against real emoji_flat.json)
# ---------------------------------------------------------------------------

EMOJI_FLAT_PATH = Path("~/.claude/local/emoji/emoji_flat.json").expanduser()

# Load real emoji_flat.json if available — skip these tests otherwise
_REAL_EMOJI: dict[str, str] = {}
if EMOJI_FLAT_PATH.exists():
    _REAL_EMOJI = json.loads(EMOJI_FLAT_PATH.read_text())


def _strip_vs16(s: str) -> str:
    """Strip VS16 like EmojiLookup.get() does."""
    return s.replace("\uFE0F", "") if s else s


@pytest.mark.skipif(not _REAL_EMOJI, reason="emoji_flat.json not found")
class TestEveryHookProducesEmoji:
    """Validate that every Claude Code hook type resolves to an emoji via emoji_flat.json."""

    # All 25 hook types from Claude Code
    ALL_HOOKS = [
        "SessionStart", "SessionEnd", "UserPromptSubmit", "Notification",
        "Elicitation", "ElicitationResult", "PreToolUse", "PostToolUse",
        "PostToolUseFailure", "PermissionRequest", "SubagentStart",
        "SubagentStop", "TeammateIdle", "TaskCreated", "TaskCompleted",
        "Stop", "StopFailure", "InstructionsLoaded", "ConfigChange",
        "CwdChanged", "FileChanged", "WorktreeCreate", "WorktreeRemove",
        "PreCompact", "PostCompact",
    ]

    def test_every_hook_has_event_key_in_db(self):
        """Every hook type must map to a key that exists in emoji_flat.json."""
        missing = []
        for hook in self.ALL_HOOKS:
            key = _mod.hook_to_event_key(hook)
            if key not in _REAL_EMOJI:
                missing.append(f"{hook} → {key}")
        assert not missing, f"Missing emoji for hooks: {missing}"

    def test_every_event_emoji_is_nonempty(self):
        """Every event emoji must be a non-empty string."""
        empty = []
        for hook in self.ALL_HOOKS:
            key = _mod.hook_to_event_key(hook)
            emoji = _REAL_EMOJI.get(key, "")
            if not emoji.strip():
                empty.append(f"{hook} → {key} = empty")
        assert not empty, f"Empty emoji for hooks: {empty}"

    def test_no_duplicate_event_emojis(self):
        """No two hook types should resolve to the same emoji."""
        seen: dict[str, str] = {}
        dupes = []
        for hook in self.ALL_HOOKS:
            key = _mod.hook_to_event_key(hook)
            emoji = _strip_vs16(_REAL_EMOJI.get(key, ""))
            if emoji in seen:
                dupes.append(f"{emoji}: {seen[emoji]} and {key}")
            else:
                seen[emoji] = key
        assert not dupes, f"Duplicate event emojis: {dupes}"

    def test_global_uniqueness(self):
        """No emoji should appear in more than one key across entire DB."""
        from collections import Counter
        counts = Counter(_strip_vs16(v) for v in _REAL_EMOJI.values())
        dupes = []
        for emoji, count in counts.items():
            if count > 1:
                keys = [k for k, v in _REAL_EMOJI.items() if _strip_vs16(v) == emoji]
                dupes.append(f"{emoji} ({count}x): {keys}")
        assert not dupes, f"Cross-category duplicates: {dupes}"

    # --- Per-hook tests with expected emoji ---

    def test_session_start(self):
        assert _strip_vs16(_REAL_EMOJI["event:session-start"]) == "🌅"

    def test_session_end(self):
        assert _strip_vs16(_REAL_EMOJI["event:session-end"]) == "🌆"

    def test_user_prompt_submit(self):
        assert _strip_vs16(_REAL_EMOJI["event:user-prompt-submit"]) == "📩"

    def test_stop(self):
        assert _strip_vs16(_REAL_EMOJI["event:stop"]) == "🏁"

    def test_stop_failure(self):
        assert _strip_vs16(_REAL_EMOJI["event:stop-failure"]) == "💥"

    def test_pre_tool_use(self):
        assert _strip_vs16(_REAL_EMOJI["event:pre-tool-use"]) == "🔧"

    def test_post_tool_use(self):
        assert _strip_vs16(_REAL_EMOJI["event:post-tool-use"]) == _strip_vs16("☑️")

    def test_post_tool_use_failure(self):
        assert _strip_vs16(_REAL_EMOJI["event:post-tool-use-failure"]) == _strip_vs16("❌")

    def test_permission_request(self):
        assert _strip_vs16(_REAL_EMOJI["event:permission-request"]) == "🔐"

    def test_notification(self):
        assert _strip_vs16(_REAL_EMOJI["event:notification"]) == "🔔"

    def test_subagent_start(self):
        assert _strip_vs16(_REAL_EMOJI["event:subagent-start"]) == "🚀"

    def test_subagent_stop(self):
        assert _strip_vs16(_REAL_EMOJI["event:subagent-stop"]) == "🛬"

    def test_pre_compact(self):
        assert _strip_vs16(_REAL_EMOJI["event:pre-compact"]) == _strip_vs16("🗜️")

    def test_post_compact(self):
        assert _strip_vs16(_REAL_EMOJI["event:post-compact"]) == "📦"

    def test_elicitation(self):
        assert _strip_vs16(_REAL_EMOJI["event:elicitation"]) == _strip_vs16("❓")

    def test_elicitation_result(self):
        assert _strip_vs16(_REAL_EMOJI["event:elicitation-result"]) == "💡"

    def test_teammate_idle(self):
        assert _strip_vs16(_REAL_EMOJI["event:teammate-idle"]) == "😶"

    def test_task_created(self):
        assert _strip_vs16(_REAL_EMOJI["event:task-created"]) == "📌"

    def test_task_completed(self):
        assert _strip_vs16(_REAL_EMOJI["event:task-completed"]) == _strip_vs16("✔️")

    def test_instructions_loaded(self):
        assert _strip_vs16(_REAL_EMOJI["event:instructions-loaded"]) == "📜"

    def test_config_change(self):
        assert _strip_vs16(_REAL_EMOJI["event:config-change"]) == _strip_vs16("⚙️")

    def test_cwd_changed(self):
        assert _strip_vs16(_REAL_EMOJI["event:cwd-changed"]) == "📂"

    def test_file_changed(self):
        assert _strip_vs16(_REAL_EMOJI["event:file-changed"]) == "📄"

    def test_worktree_create(self):
        assert _strip_vs16(_REAL_EMOJI["event:worktree-create"]) == "🍃"

    def test_worktree_remove(self):
        assert _strip_vs16(_REAL_EMOJI["event:worktree-remove"]) == "🍂"


@pytest.mark.skipif(not _REAL_EMOJI, reason="emoji_flat.json not found")
class TestToolRefinementEmojis:
    """Validate tool refinement emojis exist and are unique."""

    CORE_TOOLS = [
        "read", "write", "edit", "bash", "glob", "grep", "agent",
        "websearch", "webfetch", "skill", "sendmessage", "todowrite",
        "notebookedit", "askuserquestion", "enterplanmode", "exitplanmode",
        "task", "taskcreate", "taskget", "taskupdate", "tasklist",
        "taskoutput", "taskstop", "teamcreate", "teamdelete",
    ]

    def test_every_core_tool_has_emoji(self):
        """Every core tool name must have a tool:{name} entry."""
        missing = [t for t in self.CORE_TOOLS if f"tool:{t}" not in _REAL_EMOJI]
        assert not missing, f"Missing tool emojis: {missing}"

    def test_no_duplicate_tool_emojis(self):
        """No two tools should share an emoji."""
        seen: dict[str, str] = {}
        dupes = []
        for t in self.CORE_TOOLS:
            key = f"tool:{t}"
            emoji = _strip_vs16(_REAL_EMOJI.get(key, ""))
            if not emoji:
                continue
            if emoji in seen:
                dupes.append(f"{emoji}: {seen[emoji]} and {key}")
            else:
                seen[emoji] = key
        assert not dupes, f"Duplicate tool emojis: {dupes}"


@pytest.mark.skipif(not _REAL_EMOJI, reason="emoji_flat.json not found")
class TestNotifyRefinementEmojis:
    """Validate notification refinement emojis."""

    NOTIFY_TYPES = ["idle_prompt", "permission_prompt"]

    def test_notify_types_have_emoji(self):
        missing = [n for n in self.NOTIFY_TYPES if f"notify:{n}" not in _REAL_EMOJI]
        assert not missing, f"Missing notify emojis: {missing}"


@pytest.mark.skipif(not _REAL_EMOJI, reason="emoji_flat.json not found")
class TestVoiceStateEmojis:
    """Validate voice state emojis match the defined progression."""

    EXPECTED = {
        "state:enabled": "🔈",
        "state:listening": "🎤",
        "state:hearing": "👂",
        "state:speaking": "🌟",
        "state:queued": "💬",
        "state:muted": "🔇",
    }

    def test_all_voice_states_exist(self):
        missing = [k for k in self.EXPECTED if k not in _REAL_EMOJI]
        assert not missing, f"Missing voice state emojis: {missing}"

    def test_voice_state_emojis_match(self):
        for key, expected in self.EXPECTED.items():
            actual = _strip_vs16(_REAL_EMOJI.get(key, ""))
            assert actual == expected, f"{key}: expected {expected}, got {actual}"

    def test_no_duplicate_voice_emojis(self):
        seen: dict[str, str] = {}
        dupes = []
        for key, expected in self.EXPECTED.items():
            if expected in seen:
                dupes.append(f"{expected}: {seen[expected]} and {key}")
            else:
                seen[expected] = key
        assert not dupes, f"Duplicate voice state emojis: {dupes}"


@pytest.mark.skipif(not _REAL_EMOJI, reason="emoji_flat.json not found")
class TestEmojiLookupVS16Stripping:
    """Validate that EmojiLookup strips VS16 for tmux safety."""

    def test_vs16_stripped_from_config_change(self):
        lookup = _mod.EmojiLookup()
        result = lookup.get("event:config-change")
        assert "\uFE0F" not in result, f"VS16 not stripped: {repr(result)}"

    def test_vs16_stripped_from_tool_edit(self):
        lookup = _mod.EmojiLookup()
        result = lookup.get("tool:edit")
        assert "\uFE0F" not in result, f"VS16 not stripped: {repr(result)}"

    def test_fallback_returns_empty(self):
        lookup = _mod.EmojiLookup()
        assert lookup.get("nonexistent:key") == ""

    def test_fallback_custom(self):
        lookup = _mod.EmojiLookup()
        assert lookup.get("nonexistent:key", "?") == "?"


@pytest.mark.skipif(not _REAL_EMOJI, reason="emoji_flat.json not found")
class TestFocusEngine:
    """Validate FocusEngine — extraction, composition, and integration."""

    def _make_engine(self):
        emoji = _mod.EmojiLookup()
        engine = _mod.FocusEngine(emoji)
        engine._maybe_reload()
        return engine

    # ---- File path extraction (returns tuples) ----

    def test_file_path_detects_plugin(self):
        e = self._make_engine()
        v, p = e.resolve_file_path(
            f"{HOME}/.claude/plugins/claude-voice/scripts/voice_arbiter.py"
        )
        assert p == "plugin:claude-voice"

    def test_file_path_detects_venture(self):
        e = self._make_engine()
        v, p = e.resolve_file_path(
            f"{HOME}/.claude/plugins/claude-voice/scripts/voice_arbiter.py"
        )
        assert v == "venture:legion"

    def test_file_path_data_dir(self):
        e = self._make_engine()
        _, p = e.resolve_file_path(
            str(Path("~/.claude/local/emoji/emoji_flat.json").expanduser())
        )
        assert p == "plugin:claude-emoji"

    def test_file_path_no_match(self):
        e = self._make_engine()
        v, p = e.resolve_file_path("/tmp/random/file.txt")
        assert v == "" and p == ""

    def test_file_path_empty(self):
        e = self._make_engine()
        v, p = e.resolve_file_path("")
        assert v == "" and p == ""

    # ---- Window name extraction ----

    def test_window_name_claude_plugin(self):
        e = self._make_engine()
        v, p = e.resolve_window_name("claude-voice")
        assert p == "plugin:claude-voice"
        assert v == "venture:legion"  # inferred — claude-* → legion

    def test_window_name_unknown(self):
        e = self._make_engine()
        v, p = e.resolve_window_name("bash")
        assert v == "" and p == ""

    def test_window_name_empty(self):
        e = self._make_engine()
        v, p = e.resolve_window_name("")
        assert v == "" and p == ""

    # ---- Alias table loading ----

    def test_load_aliases_ventures(self):
        e = self._make_engine()
        e._load_aliases()
        assert isinstance(e._aliases_ventures, dict)
        assert e._aliases_ventures.get("acme") == "acme-corp"
        assert e._aliases_ventures.get("beta") == "beta-labs"

    def test_load_aliases_plugins(self):
        e = self._make_engine()
        e._load_aliases()
        assert isinstance(e._aliases_plugins, dict)
        assert e._aliases_plugins.get("voice") == "claude-voice"
        assert e._aliases_plugins.get("hippo") == "claude-hippo"

    def test_load_aliases_missing_file(self):
        e = self._make_engine()
        e._aliases_path = Path("/tmp/nonexistent-aliases.json")
        e._load_aliases()
        assert e._aliases_ventures == {}
        assert e._aliases_plugins == {}

    # ---- Alias-aware window name resolution ----

    def test_window_name_venture_alias(self):
        """Short alias 'acme' resolves to full venture ID."""
        e = self._make_engine()
        v, p = e.resolve_window_name("acme")
        assert v == "venture:acme-corp"

    def test_window_name_venture_alias_case(self):
        """Alias 'AC' resolves to acme-corp."""
        e = self._make_engine()
        v, p = e.resolve_window_name("AC")
        assert v == "venture:acme-corp"

    def test_window_name_venture_alias_regen(self):
        e = self._make_engine()
        v, p = e.resolve_window_name("beta")
        assert v == "venture:beta-labs"

    def test_window_name_plugin_alias(self):
        """Short alias 'voice' resolves to plugin:claude-voice."""
        e = self._make_engine()
        v, p = e.resolve_window_name("voice")
        assert p == "plugin:claude-voice"
        assert v == "venture:legion"

    def test_window_name_plugin_alias_hippo(self):
        e = self._make_engine()
        v, p = e.resolve_window_name("hippo")
        assert p == "plugin:claude-hippo"
        assert v == "venture:legion"

    def test_window_name_exact_still_works(self):
        """Exact venture slug still works (no regression)."""
        e = self._make_engine()
        v, p = e.resolve_window_name("beta-labs")
        assert v == "venture:beta-labs"

    def test_window_name_exact_plugin_still_works(self):
        """Exact plugin name still works (no regression)."""
        e = self._make_engine()
        v, p = e.resolve_window_name("claude-voice")
        assert p == "plugin:claude-voice"
        assert v == "venture:legion"

    # ---- Task 3: Canonical venture IDs from markdown files ----

    def test_canonical_ventures_loaded(self):
        """Canonical venture list loaded from markdown filenames."""
        e = self._make_engine()
        e._load_canonical_ventures()
        assert "acme-corp" in e._canonical_ventures
        assert "beta-labs" in e._canonical_ventures
        assert "gamma-fund" in e._canonical_ventures

    def test_canonical_ventures_fallback(self):
        """Graceful when venture directory doesn't exist."""
        e = self._make_engine()
        e._ventures_dir = Path("/tmp/nonexistent-ventures/")
        e._load_canonical_ventures()
        assert e._canonical_ventures == set()

    def test_window_name_canonical_venture(self):
        """Window name matching canonical venture ID."""
        e = self._make_engine()
        v, p = e.resolve_window_name("gamma-fund")
        assert v == "venture:gamma-fund"

    # ---- Task 4: Startup validation warns on slug drift ----

    def test_validate_ventures_clean(self):
        """Slugs in ventures.json that ARE in canonical ventures produce no warnings.
        Known drift (e.g. koi-protocol in ventures.json but no .md yet) is permitted.
        This test verifies canonical ventures (bcrg, regen-ai, etc.) are not flagged."""
        e = self._make_engine()
        e._load_canonical_ventures()
        e._maybe_reload()
        e._load_aliases()
        warnings = e.validate_ventures()
        # None of the canonical venture slugs should appear as "not found in active"
        for slug in e._canonical_ventures:
            assert not any(f"'{slug}' not found in active" in w for w in warnings), (
                f"Canonical venture '{slug}' wrongly flagged as missing: {warnings}"
            )

    def test_validate_ventures_missing(self):
        """Warns when ventures.json has slug not in canonical ventures."""
        e = self._make_engine()
        e._canonical_ventures = {"regen-ai", "bcrg"}
        e._ventures = {"regen-ai": [], "bcrg": [], "fake-venture": []}
        e._aliases_ventures = {}
        warnings = e.validate_ventures()
        assert any("fake-venture" in w for w in warnings)

    # ---- Task 5: Case-insensitive alias and venture resolution ----

    def test_window_name_case_insensitive_alias(self):
        """Case-insensitive alias lookup."""
        e = self._make_engine()
        v, _ = e.resolve_window_name("Acme")
        assert v == "venture:acme-corp"

    def test_window_name_case_insensitive_venture(self):
        """Case-insensitive exact venture match."""
        e = self._make_engine()
        v, _ = e.resolve_window_name("GAMMA-FUND")
        assert v == "venture:gamma-fund"

    def test_window_name_case_insensitive_plugin(self):
        """Case-insensitive plugin alias."""
        e = self._make_engine()
        _, p = e.resolve_window_name("Voice")
        assert p == "plugin:claude-voice"

    # ---- Task 2: Domain vocabulary fallback ----

    def test_file_path_documents_domain(self):
        """~/Documents paths resolve to domain:documents."""
        e = self._make_engine()
        v, p = e.resolve_file_path(f"{HOME}/Documents/Notes/Shared/messages/test.md")
        assert p == "domain:documents"

    def test_file_path_config_domain(self):
        """~/.config paths resolve to domain:config."""
        e = self._make_engine()
        v, p = e.resolve_file_path(f"{HOME}/.config/tmux/tmux.conf")
        assert p == "domain:config"

    def test_file_path_docs_superpowers(self):
        """~/docs paths resolve to domain:documents."""
        e = self._make_engine()
        v, p = e.resolve_file_path(f"{HOME}/docs/superpowers/plans/test.md")
        assert p == "domain:documents"

    def test_file_path_workspace_generic(self):
        """~/Workspace with no venture match resolves to domain:workspace."""
        e = self._make_engine()
        v, p = e.resolve_file_path(f"{HOME}/Workspace/unknown-project/file.py")
        # If no venture matches, should get domain:workspace
        assert p == "domain:workspace" or v != ""

    def test_file_path_tmp_no_domain(self):
        """/tmp paths don't get domain fallback."""
        e = self._make_engine()
        v, p = e.resolve_file_path("/tmp/claude-1000/test.py")
        assert v == "" and p == ""

    def test_file_path_plugin_beats_domain(self):
        """Plugin detection takes priority over domain fallback."""
        e = self._make_engine()
        v, p = e.resolve_file_path(
            f"{HOME}/.claude/plugins/claude-voice/test.py"
        )
        assert p == "plugin:claude-voice"

    # ---- cwd extraction ----

    def test_cwd_home_shawn_no_match(self):
        e = self._make_engine()
        v, p = e.resolve_cwd(f"{HOME}")
        assert v == "" and p == ""

    def test_cwd_venture_match(self):
        e = self._make_engine()
        v, p = e.resolve_cwd(f"{HOME}/.claude/plugins")
        assert v == "venture:legion"
        assert p == ""

    # ---- Composition rules ----

    def test_compose_venture_only(self):
        e = self._make_engine()
        result = e.compose("venture:regen-ai", "")
        assert result.strip() != ""

    def test_compose_plugin_only(self):
        e = self._make_engine()
        result = e.compose("", "plugin:claude-voice")
        assert result.strip() != ""

    def test_compose_collapse_legion(self):
        """Legion venture + claude-* plugin → collapse to plugin only."""
        e = self._make_engine()
        result = e.compose("venture:legion", "plugin:claude-voice")
        plugin_emoji = e.emoji.get_tmux("plugin:claude-voice")
        assert result == plugin_emoji, f"Expected collapse to plugin, got {result!r}"

    def test_compose_prefers_slot2(self):
        """Different venture + plugin → single emoji (slot 2 wins, more specific)."""
        e = self._make_engine()
        result = e.compose("venture:acme-corp", "plugin:claude-hippo")
        plugin_emoji = e.emoji.get_tmux("plugin:claude-hippo")
        assert result == plugin_emoji

    # ---- Task 6: clear_pane ----

    def test_clear_pane_removes_signals(self):
        e = self._make_engine()
        e.observe("file_path", "plugin:claude-voice", pane_id="%0", now=1000.0)
        e.observe("window_name", "venture:legion", pane_id="%0", now=1000.0)
        assert "%0" in e._signals
        e.clear_pane("%0")
        assert "%0" not in e._signals

    def test_clear_pane_noop_unknown(self):
        e = self._make_engine()
        e.clear_pane("%99")  # should not raise

    # ---- Task 7: config half-life override ----

    def test_config_overrides_half_life(self):
        """FocusEngine respects config half-life values."""
        e = self._make_engine()
        assert e.DECAY_HALF_LIFE_FILE == 120.0
        e.DECAY_HALF_LIFE_FILE = 60.0
        sig = _mod.FocusSignal(key="plugin:claude-voice", weight=8.0, timestamp=1000.0, source="file_path")
        result = e._decayed_weight(sig, now=1060.0)
        assert abs(result - 4.0) < 0.01

    # ---- Task 8: signal serialization ----

    def test_signal_survives_serialization(self):
        """FocusSignal round-trips through dataclasses.asdict."""
        import dataclasses as dc
        sig = _mod.FocusSignal(key="plugin:claude-voice", weight=5.0, timestamp=1000.0, source="file_path")
        d = dc.asdict(sig)
        assert d == {"key": "plugin:claude-voice", "weight": 5.0, "timestamp": 1000.0, "source": "file_path"}
        restored = _mod.FocusSignal(**d)
        assert restored == sig

    # ---- Task 9: prune_stale ----

    def test_prune_removes_dead_signals(self):
        e = self._make_engine()
        e.observe("file_path", "plugin:claude-voice", pane_id="%0", now=1000.0)
        e.observe("window_name", "venture:legion", pane_id="%0", now=1000.0)
        far_future = 1000.0 + 20 * e.DECAY_HALF_LIFE_FILE
        pruned = e.prune_stale(now=far_future)
        assert pruned == 1  # file_path pruned, window_name survives
        assert "venture:legion" in e._signals["%0"]
        assert "plugin:claude-voice" not in e._signals["%0"]

    def test_prune_removes_empty_panes(self):
        """If all signals pruned, pane entry removed entirely."""
        e = self._make_engine()
        e.observe("file_path", "plugin:claude-voice", pane_id="%0", now=1000.0)
        far_future = 1000.0 + 20 * e.DECAY_HALF_LIFE_FILE
        e.prune_stale(now=far_future)
        assert "%0" not in e._signals

    def test_compose_empty(self):
        e = self._make_engine()
        assert e.compose("", "") == ""

    # ---- Integration: apply_event sets venture + plugin ----

    def test_apply_event_sets_focus_signals(self):
        arb = _make_arbiter()
        _start_session(arb)
        arb.apply_event("PreToolUse", PANE, {
            "tool_name": "Read",
            "tool_input": {
                "file_path": f"{HOME}/.claude/plugins/claude-voice/scripts/voice_arbiter.py"
            }
        })
        pane = arb.panes[PANE]
        assert pane.focus_venture == "venture:legion"
        assert pane.focus_plugin == "plugin:claude-voice"
        assert pane.focus_emoji != ""

    def test_apply_event_uses_decay_signals(self):
        """apply_event with two file_path events — newer plugin wins."""
        arb = _make_arbiter()
        _start_session(arb)
        # Touch voice first
        arb.apply_event("PreToolUse", PANE, {
            "tool_name": "Read",
            "tool_input": {
                "file_path": f"{HOME}/.claude/plugins/claude-voice/scripts/voice_arbiter.py"
            }
        })
        assert arb.panes[PANE].focus_plugin == "plugin:claude-voice"

        # Touch hippo later
        arb.apply_event("PreToolUse", PANE, {
            "tool_name": "Read",
            "tool_input": {
                "file_path": f"{HOME}/.claude/plugins/claude-hippo/scripts/hippo.py"
            }
        })
        # Hippo is newest signal — should win
        assert arb.panes[PANE].focus_plugin == "plugin:claude-hippo"

    # ---- FocusSignal dataclass ----

    def test_focus_signal_dataclass(self):
        sig = _mod.FocusSignal(
            key="plugin:claude-voice",
            weight=5.0,
            timestamp=1000.0,
            source="file_path",
        )
        assert sig.key == "plugin:claude-voice"
        assert sig.weight == 5.0
        assert sig.timestamp == 1000.0
        assert sig.source == "file_path"

    # ---- _decayed_weight() ----

    def test_decay_zero_age(self):
        """No decay when signal is fresh."""
        e = self._make_engine()
        sig = _mod.FocusSignal(key="plugin:claude-voice", weight=5.0, timestamp=1000.0, source="file_path")
        result = e._decayed_weight(sig, now=1000.0)
        assert result == 5.0

    def test_decay_one_half_life(self):
        """Weight halves after one half-life."""
        e = self._make_engine()
        sig = _mod.FocusSignal(key="plugin:claude-voice", weight=8.0, timestamp=1000.0, source="file_path")
        result = e._decayed_weight(sig, now=1000.0 + e.DECAY_HALF_LIFE_FILE)
        assert abs(result - 4.0) < 0.01

    def test_decay_two_half_lives(self):
        """Weight quarters after two half-lives."""
        e = self._make_engine()
        sig = _mod.FocusSignal(key="plugin:claude-voice", weight=8.0, timestamp=1000.0, source="file_path")
        result = e._decayed_weight(sig, now=1000.0 + 2 * e.DECAY_HALF_LIFE_FILE)
        assert abs(result - 2.0) < 0.01

    def test_decay_window_name_no_decay(self):
        """Window name signals never decay (sticky)."""
        e = self._make_engine()
        sig = _mod.FocusSignal(key="venture:legion", weight=10.0, timestamp=0.0, source="window_name")
        result = e._decayed_weight(sig, now=999999.0)
        assert result == 10.0

    def test_decay_explicit_no_decay(self):
        """Explicit /focus signals never decay."""
        e = self._make_engine()
        sig = _mod.FocusSignal(key="venture:regen-ai", weight=100.0, timestamp=0.0, source="explicit")
        result = e._decayed_weight(sig, now=999999.0)
        assert result == 100.0

    def test_decay_cwd_slower_than_file(self):
        """cwd decays slower than file_path (longer half-life)."""
        e = self._make_engine()
        sig_file = _mod.FocusSignal(key="plugin:claude-voice", weight=5.0, timestamp=1000.0, source="file_path")
        sig_cwd = _mod.FocusSignal(key="venture:legion", weight=5.0, timestamp=1000.0, source="cwd")
        at = 1000.0 + e.DECAY_HALF_LIFE_FILE  # one file half-life elapsed
        w_file = e._decayed_weight(sig_file, now=at)
        w_cwd = e._decayed_weight(sig_cwd, now=at)
        assert w_cwd > w_file  # cwd decays slower

    # ---- observe() signal store ----

    def test_observe_file_path_stores_signal(self):
        e = self._make_engine()
        e.observe("file_path", "plugin:claude-voice", pane_id="%0", now=1000.0)
        assert "%0" in e._signals
        assert "plugin:claude-voice" in e._signals["%0"]
        sig = e._signals["%0"]["plugin:claude-voice"]
        assert sig.source == "file_path"
        assert sig.timestamp == 1000.0

    def test_observe_updates_timestamp(self):
        """Repeat observation updates timestamp, keeps max weight."""
        e = self._make_engine()
        e.observe("file_path", "plugin:claude-voice", pane_id="%0", now=1000.0)
        e.observe("file_path", "plugin:claude-voice", pane_id="%0", now=2000.0)
        sig = e._signals["%0"]["plugin:claude-voice"]
        assert sig.timestamp == 2000.0

    def test_observe_window_name_weight_higher(self):
        e = self._make_engine()
        e.observe("window_name", "venture:legion", pane_id="%0", now=1000.0)
        e.observe("file_path", "venture:legion", pane_id="%0", now=1000.0)
        sig = e._signals["%0"]["venture:legion"]
        # window_name weight (10.0) should win over file_path (5.0)
        assert sig.weight == 10.0
        assert sig.source == "window_name"

    def test_observe_separate_panes(self):
        e = self._make_engine()
        e.observe("file_path", "plugin:claude-voice", pane_id="%0", now=1000.0)
        e.observe("file_path", "plugin:claude-hippo", pane_id="%1", now=1000.0)
        assert "plugin:claude-voice" in e._signals["%0"]
        assert "plugin:claude-hippo" in e._signals["%1"]
        assert "plugin:claude-hippo" not in e._signals["%0"]

    # ---- resolve_focus() ----

    def test_resolve_focus_single_plugin(self):
        e = self._make_engine()
        e.observe("file_path", "plugin:claude-voice", pane_id="%0", now=1000.0)
        result = e.resolve_focus("%0", now=1000.0)
        assert result.slot1 == "plugin:claude-voice"  # no venture = goes to slot1
        assert result.slot2 == ""
        assert result.emoji.strip() != ""

    def test_resolve_focus_venture_and_plugin(self):
        e = self._make_engine()
        e.observe("window_name", "venture:acme-corp", pane_id="%0", now=1000.0)
        e.observe("file_path", "plugin:claude-hippo", pane_id="%0", now=1000.0)
        result = e.resolve_focus("%0", now=1000.0)
        assert result.slot1 == "venture:acme-corp"
        assert result.slot2 == "plugin:claude-hippo"
        # Single emoji — slot 2 (more specific) wins
        plugin_emoji = e.emoji.get_tmux("plugin:claude-hippo")
        plugin_emoji = e.emoji.get_tmux("plugin:claude-hippo")
        assert result.emoji == plugin_emoji  # single emoji, slot 2 wins

    def test_resolve_focus_decay_changes_winner(self):
        """Old plugin signal decays, newer one wins."""
        e = self._make_engine()
        e.observe("file_path", "plugin:claude-voice", pane_id="%0", now=1000.0)
        e.observe("file_path", "plugin:claude-hippo", pane_id="%0", now=1500.0)
        # At t=1500, hippo is fresh (weight=5.0), voice is 500s old (decayed)
        result = e.resolve_focus("%0", now=1500.0)
        assert result.slot1 == "plugin:claude-hippo"  # no venture = plugin goes to slot1

    def test_resolve_focus_window_name_survives_decay(self):
        """Window name venture never decays — still wins after long time."""
        e = self._make_engine()
        e.observe("window_name", "venture:legion", pane_id="%0", now=1000.0)
        e.observe("file_path", "venture:regen-ai", pane_id="%0", now=5000.0)
        # file_path venture is fresh but window_name has higher base weight AND no decay
        result = e.resolve_focus("%0", now=5000.0)
        assert result.slot1 == "venture:legion"

    def test_resolve_focus_empty_pane(self):
        e = self._make_engine()
        result = e.resolve_focus("%99", now=1000.0)
        assert result.slot1 == "" and result.slot2 == "" and result.emoji == ""

    def test_resolve_focus_fully_decayed(self):
        """After many half-lives, file_path signal effectively zero — no focus."""
        e = self._make_engine()
        e.observe("file_path", "plugin:claude-voice", pane_id="%0", now=1000.0)
        # 20 half-lives later = weight * 2^-20 ~ 0.000005 — below threshold
        result = e.resolve_focus("%0", now=1000.0 + 20 * e.DECAY_HALF_LIFE_FILE)
        assert result.slot1 == ""
        assert result.emoji == ""

    # ---- Phase 5 Task 1: explicit focus set/clear/status ----

    def test_set_explicit_focus_venture(self):
        """Explicit focus sets venture signal with weight 100."""
        e = self._make_engine()
        e.observe("explicit", "venture:regen-ai", pane_id="%0")
        result = e.resolve_focus("%0")
        assert result.slot1 == "venture:regen-ai"
        assert result.emoji.strip() != ""

    def test_set_explicit_focus_plugin(self):
        """Explicit focus sets plugin signal with weight 100."""
        e = self._make_engine()
        e.observe("explicit", "plugin:claude-voice", pane_id="%0")
        result = e.resolve_focus("%0")
        assert result.slot1 == "plugin:claude-voice"  # no venture = plugin goes to slot1

    def test_explicit_overrides_file_path(self):
        """Explicit focus always beats file_path signals."""
        e = self._make_engine()
        e.observe("file_path", "venture:legion", pane_id="%0", now=1000.0)
        e.observe("explicit", "venture:regen-ai", pane_id="%0", now=1000.0)
        result = e.resolve_focus("%0", now=1000.0)
        assert result.slot1 == "venture:regen-ai"

    def test_explicit_overrides_window_name(self):
        """Explicit focus beats even window_name signals."""
        e = self._make_engine()
        e.observe("window_name", "venture:legion", pane_id="%0", now=1000.0)
        e.observe("explicit", "venture:regen-ai", pane_id="%0", now=1000.0)
        result = e.resolve_focus("%0", now=1000.0)
        assert result.slot1 == "venture:regen-ai"

    def test_clear_explicit_focus(self):
        """clear_explicit removes only explicit signals, keeps others."""
        e = self._make_engine()
        e.observe("window_name", "venture:legion", pane_id="%0", now=1000.0)
        e.observe("explicit", "venture:regen-ai", pane_id="%0", now=1000.0)
        e.clear_explicit("%0")
        result = e.resolve_focus("%0", now=1000.0)
        assert result.slot1 == "venture:legion"

    def test_get_focus_status(self):
        """get_focus_status returns structured signal info."""
        e = self._make_engine()
        e.observe("window_name", "venture:legion", pane_id="%0", now=1000.0)
        e.observe("file_path", "plugin:claude-voice", pane_id="%0", now=1000.0)
        status = e.get_focus_status("%0", now=1000.0)
        assert status["slot1"] == "venture:legion"
        assert status["slot2"] == "plugin:claude-voice"
        assert len(status["signals"]) == 2

    def test_get_focus_status_empty(self):
        e = self._make_engine()
        status = e.get_focus_status("%99", now=1000.0)
        assert status["slot1"] == ""
        assert status["slot2"] == ""
        assert status["signals"] == []


    # ---- N-category ranking tests ----

    def test_resolve_picks_person_over_plugin(self):
        """Person has higher info priority than plugin for slot 2."""
        e = self._make_engine()
        e.observe("window_name", "venture:legion", pane_id="%0", now=1000.0)
        e.observe("file_path", "plugin:claude-voice", pane_id="%0", now=1000.0)
        e.observe("file_path", "person:darren", pane_id="%0", now=1000.0)
        result = e.resolve_focus("%0", now=1000.0)
        assert result.slot1 == "venture:legion"
        assert result.slot2 == "person:darren"

    def test_resolve_picks_plugin_over_action(self):
        """Plugin has higher info priority than action for slot 2 (plugin = identity)."""
        e = self._make_engine()
        e.observe("window_name", "venture:legion", pane_id="%0", now=1000.0)
        e.observe("file_path", "plugin:claude-voice", pane_id="%0", now=1000.0)
        e.observe("agent_type", "action:review", pane_id="%0", now=1000.0)
        result = e.resolve_focus("%0", now=1000.0)
        assert result.slot1 == "venture:legion"
        assert result.slot2 == "plugin:claude-voice"

    def test_resolve_picks_person_over_action(self):
        """Person beats action for slot 2."""
        e = self._make_engine()
        e.observe("window_name", "venture:legion", pane_id="%0", now=1000.0)
        e.observe("agent_type", "action:review", pane_id="%0", now=1000.0)
        e.observe("file_path", "person:darren", pane_id="%0", now=1000.0)
        result = e.resolve_focus("%0", now=1000.0)
        assert result.slot1 == "venture:legion"
        assert result.slot2 == "person:darren"

    def test_resolve_no_venture_uses_highest_priority(self):
        """Without venture, slot 1 is highest priority non-venture."""
        e = self._make_engine()
        e.observe("agent_type", "action:ops", pane_id="%0", now=1000.0)
        result = e.resolve_focus("%0", now=1000.0)
        assert result.slot1 == "action:ops"
        assert result.slot2 == ""

    def test_resolve_plugin_always_wins_over_action(self):
        """Plugin (identity) always beats action (transient) for slot 2."""
        e = self._make_engine()
        e.observe("window_name", "venture:legion", pane_id="%0", now=1000.0)
        e.observe("window_name", "plugin:claude-tmux", pane_id="%0", now=1000.0)
        e.observe("agent_type", "action:review", pane_id="%0", now=1000.0)
        result = e.resolve_focus("%0", now=1000.0)
        assert result.slot2 == "plugin:claude-tmux"  # plugin wins
        # After action decays, still plugin
        result2 = e.resolve_focus("%0", now=7000.0)
        assert result2.slot2 == "plugin:claude-tmux"

    def test_resolve_backward_compat_venture_plugin(self):
        """Normal venture+plugin still works."""
        e = self._make_engine()
        e.observe("window_name", "venture:legion", pane_id="%0", now=1000.0)
        e.observe("window_name", "plugin:claude-voice", pane_id="%0", now=1000.0)
        result = e.resolve_focus("%0", now=1000.0)
        assert result.slot1 == "venture:legion"
        assert result.slot2 == "plugin:claude-voice"
        assert result.emoji != ""

    def test_resolve_empty_returns_focus_result(self):
        e = self._make_engine()
        result = e.resolve_focus("%99", now=1000.0)
        assert isinstance(result, _mod.FocusResult)
        assert result.slot1 == ""
        assert result.slot2 == ""
        assert result.emoji == ""

    def test_compose_venture_person_picks_person(self):
        """venture+person → single emoji (person wins, more specific)."""
        e = self._make_engine()
        result = e.compose("venture:legion", "person:darren")
        darren_e = e.emoji.get_tmux("person:darren")
        assert result == darren_e

    def test_compose_venture_action_picks_action(self):
        """venture+action → single emoji (action wins, more specific)."""
        e = self._make_engine()
        result = e.compose("venture:legion", "action:review")
        review_e = e.emoji.get_tmux("action:review")
        assert result == review_e

    def test_compose_venture_plugin_still_collapses(self):
        """venture+plugin collapse rule still works."""
        e = self._make_engine()
        result = e.compose("venture:legion", "plugin:claude-voice")
        plugin_e = e.emoji.get_tmux("plugin:claude-voice")
        assert result == plugin_e  # collapsed


class TestIntentClassifier:
    """Validate IntentClassifier — agent_type and skill_name to action mapping."""

    def _make_classifier(self):
        return _mod.IntentClassifier()

    def test_agent_code_reviewer(self):
        c = self._make_classifier()
        assert c.from_agent_type("superpowers:code-reviewer") == "action:review"

    def test_agent_code_explorer(self):
        c = self._make_classifier()
        assert c.from_agent_type("feature-dev:code-explorer") == "action:research"

    def test_agent_researcher(self):
        c = self._make_classifier()
        assert c.from_agent_type("claude-research:researcher") == "action:research"

    def test_agent_code_architect(self):
        c = self._make_classifier()
        assert c.from_agent_type("feature-dev:code-architect") == "action:plan"

    def test_agent_taskmaster(self):
        c = self._make_classifier()
        assert c.from_agent_type("claude-backlog:taskmaster") == "action:plan"

    def test_agent_scribe(self):
        c = self._make_classifier()
        assert c.from_agent_type("claude-journal:scribe") == "action:write"

    def test_agent_general_purpose_no_intent(self):
        c = self._make_classifier()
        assert c.from_agent_type("general-purpose") == ""

    def test_agent_empty(self):
        c = self._make_classifier()
        assert c.from_agent_type("") == ""

    def test_skill_brainstorming(self):
        c = self._make_classifier()
        assert c.from_skill_name("brainstorming") == "action:plan"

    def test_skill_writing_plans(self):
        c = self._make_classifier()
        assert c.from_skill_name("writing-plans") == "action:plan"

    def test_skill_subagent_driven(self):
        c = self._make_classifier()
        assert c.from_skill_name("subagent-driven-development") == "action:build"

    def test_skill_tdd(self):
        c = self._make_classifier()
        assert c.from_skill_name("test-driven-development") == "action:build"

    def test_skill_debugging(self):
        c = self._make_classifier()
        assert c.from_skill_name("systematic-debugging") == "action:fix"

    def test_skill_code_review(self):
        c = self._make_classifier()
        assert c.from_skill_name("requesting-code-review") == "action:review"

    def test_skill_checkpoint(self):
        c = self._make_classifier()
        assert c.from_skill_name("checkpoint") == "action:ops"

    def test_skill_unknown(self):
        c = self._make_classifier()
        assert c.from_skill_name("some-random-skill") == ""


# ---------------------------------------------------------------------------
# Task 4: apply_event() signal wiring tests
# ---------------------------------------------------------------------------

class TestApplyEventSignalWiring:
    """Validate that apply_event() wires SubagentStart→action, Skill→action, vault→person."""

    def test_apply_event_subagent_sets_action(self):
        """SubagentStart with code-reviewer sets action:review."""
        arb = _make_arbiter()
        _start_session(arb)
        arb.apply_event("SubagentStart", PANE, {"agent_type": "superpowers:code-reviewer"})
        result = arb.focus.resolve_focus(PANE)
        assert "action:review" in (result.slot1, result.slot2)

    def test_apply_event_generic_agent_no_action(self):
        """general-purpose agent doesn't set action."""
        arb = _make_arbiter()
        _start_session(arb)
        arb.apply_event("SubagentStart", PANE, {"agent_type": "general-purpose"})
        signals = arb.focus._signals.get(PANE, {})
        action_signals = [k for k in signals if k.startswith("action:")]
        assert action_signals == []

    def test_apply_event_shared_vault_sets_person(self):
        """File path in a configured shared vault sets person:<configured>."""
        arb = _make_arbiter()
        arb.config.shared_vault_path = f"{HOME}/Documents/Notes/Shared"
        arb.config.shared_vault_person = "dana"
        _start_session(arb)
        arb.apply_event("PreToolUse", PANE, {
            "tool_name": "Read",
            "tool_input": {
                "file_path": f"{HOME}/Documents/Notes/Shared/messages/test.md"
            }
        })
        signals = arb.focus._signals.get(PANE, {})
        assert "person:dana" in signals

    def test_apply_event_skill_sets_sticky_action(self):
        """Skill invocation sets action with 'skill' source (no decay)."""
        arb = _make_arbiter()
        _start_session(arb)
        arb.apply_event("PreToolUse", PANE, {
            "tool_name": "Skill",
            "tool_input": {"skill": "brainstorming"}
        })
        signals = arb.focus._signals.get(PANE, {})
        assert "action:plan" in signals
        sig = signals["action:plan"]
        assert sig.source == "skill"
