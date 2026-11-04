"""Native turn state must survive quiet inference and fail visibly on evidence loss."""
import importlib.util
from pathlib import Path
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest


@pytest.fixture
def arbiter(monkeypatch, tmp_path):
    path = Path(__file__).resolve().parents[1] / "scripts/tmux-arbiter.py"
    spec = importlib.util.spec_from_file_location("lifecycle_reconcile_test", path)
    mod = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, mod)
    spec.loader.exec_module(mod)
    arb = object.__new__(mod.TmuxArbiter)
    arb.config = mod.Config()
    arb.theme = mod.ThemeLoader(path.parent.parent / "theme.json")
    arb.emoji = mod.EmojiLookup(tmp_path / "emoji.json")
    arb.focus = mod.FocusEngine(arb.emoji)
    arb.panes = {}
    arb._persist = lambda: None
    arb._get_focused_pane = lambda: "%9"
    evidence = {"%8": {"provider": "codex", "state": "working", "source": "native-turn",
                       "process_id": 123, "process_start": "456", "session_id": "thread",
                       "updated_at": time.time() - 3600}}
    arb.lifecycle_collectors = [SimpleNamespace(poll=lambda inventory: evidence.copy())]
    monkeypatch.setattr(mod.subprocess, "run", lambda *a, **kw:
                        SimpleNamespace(returncode=0, stdout="%8\t100\tcodex\n"))
    writes = []
    clears = []
    monkeypatch.setattr(mod.TmuxWriter, "write_state", lambda pane: writes.append(pane))
    monkeypatch.setattr(mod.TmuxWriter, "clear_state", lambda pane: clears.append(pane))
    return mod, arb, evidence, writes, clears


def test_quiet_native_turn_stays_working_without_new_events(arbiter):
    _, arb, _, writes, _ = arbiter
    assert arb.reconcile_lifecycle() == 1
    assert arb.panes["%8"].state_name == "working"
    assert arb.reconcile_lifecycle() == 0
    assert len(writes) == 1, "unchanged native state must not churn terminal selection"


def test_native_children_update_without_root_state_change(arbiter):
    _, arb, evidence, writes, _ = arbiter
    evidence["%8"].update(child_count_known=True, active_agent_count=2)
    arb.reconcile_lifecycle()
    assert arb.panes["%8"].active_agent_count == 2
    assert arb.reconcile_lifecycle() == 0
    evidence["%8"]["active_agent_count"] = 1
    assert arb.reconcile_lifecycle() == 1
    assert writes[-1].active_agent_count == 1
    evidence["%8"]["active_agent_count"] = 0
    arb.reconcile_lifecycle()
    assert arb.panes["%8"].active_agent_count == 0


def test_incomplete_child_evidence_removes_stale_marker(arbiter):
    _, arb, evidence, _, _ = arbiter
    evidence["%8"].update(child_count_known=True, active_agent_count=2)
    arb.reconcile_lifecycle()
    evidence["%8"].pop("active_agent_count")
    evidence["%8"]["child_count_known"] = False
    arb.reconcile_lifecycle()
    assert arb.panes["%8"].state_name == "working"
    assert arb.panes["%8"].active_agent_count == 0


def test_idle_parent_can_still_have_native_active_children(arbiter):
    _, arb, evidence, _, _ = arbiter
    evidence["%8"].update(state="idle", child_count_known=True, active_agent_count=1)
    arb.reconcile_lifecycle()
    assert arb.panes["%8"].state_name == "idle"
    assert arb.panes["%8"].active_agent_count == 1


def test_actual_completion_is_unread_until_viewed(arbiter):
    _, arb, evidence, _, _ = arbiter
    arb.reconcile_lifecycle()
    evidence["%8"]["state"] = "idle"
    arb.reconcile_lifecycle()
    assert arb.panes["%8"].state_name == "unread"
    arb.reconcile_lifecycle()
    assert arb.panes["%8"].state_name == "unread"
    arb._get_focused_pane = lambda: "%8"
    arb.reconcile_lifecycle()
    assert arb.panes["%8"].state_name == "idle"


def test_new_process_does_not_inherit_old_completion(arbiter):
    _, arb, evidence, _, _ = arbiter
    arb.reconcile_lifecycle()
    evidence["%8"].update(state="idle", process_start="new")
    arb.reconcile_lifecycle()
    assert arb.panes["%8"].state_name == "idle"


def test_native_reader_failure_is_unknown_never_idle(arbiter):
    _, arb, evidence, _, clears = arbiter
    evidence["%8"].update(child_count_known=True, active_agent_count=2)
    arb.reconcile_lifecycle()
    def fail(_):
        raise OSError("fixture read failed")
    arb.lifecycle_collectors = [SimpleNamespace(poll=fail)]
    arb.reconcile_lifecycle()
    assert arb.panes["%8"].state_name == "unknown"
    assert arb.panes["%8"].active_agent_count == 0
    assert clears == []


def test_unread_survives_reader_failure_and_recovery(arbiter):
    _, arb, evidence, _, _ = arbiter
    arb.reconcile_lifecycle()
    evidence["%8"].update(state="idle", updated_at=time.time())
    arb.reconcile_lifecycle()
    assert arb.panes["%8"].lifecycle_unread
    collector = arb.lifecycle_collectors[0]
    def fail(_):
        raise OSError("temporary")
    arb.lifecycle_collectors = [SimpleNamespace(poll=fail)]
    arb.reconcile_lifecycle()
    assert arb.panes["%8"].state_name == "unknown"
    arb.lifecycle_collectors = [collector]
    arb.reconcile_lifecycle()
    assert arb.panes["%8"].state_name == "unread"


def test_shared_process_reader_exception_cannot_stop_daemon(arbiter):
    _, arb, _, _, _ = arbiter
    arb.reconcile_lifecycle()
    def fail():
        raise ValueError("bad process row")
    arb.lifecycle_process_reader = fail
    arb.reconcile_lifecycle()
    assert arb.panes["%8"].state_name == "unknown"


def test_old_native_idle_cannot_override_new_prompt_hook(arbiter):
    mod, arb, evidence, _, _ = arbiter
    evidence["%8"]["state"] = "idle"
    arb.reconcile_lifecycle()
    arb.apply_event("UserPromptSubmit", "%8", {"_provider": "codex"})
    for _ in range(3):
        arb.reconcile_lifecycle()
        assert arb.panes["%8"].state_name == "working"


def test_new_execution_cannot_inherit_old_prompt_hook(arbiter):
    _, arb, evidence, _, _ = arbiter
    arb.reconcile_lifecycle()
    arb.apply_event("PermissionRequest", "%8", {"_provider": "codex"})
    evidence["%8"].update(state="unknown", session_id="other-thread")
    arb.reconcile_lifecycle()
    assert arb.panes["%8"].state_name == "unknown"


def test_agent_exit_clears_badge_when_shell_pane_remains(arbiter, monkeypatch):
    mod, arb, evidence, _, clears = arbiter
    arb.reconcile_lifecycle()
    evidence.clear()
    monkeypatch.setattr(mod.subprocess, "run", lambda *a, **kw:
                        SimpleNamespace(returncode=0, stdout="%8\t100\tfish\n"))
    arb.reconcile_lifecycle()
    assert "%8" not in arb.panes
    assert clears == ["%8"]


def test_recognized_cli_with_missing_process_metadata_is_unknown(arbiter):
    _, arb, evidence, _, clears = arbiter
    arb.reconcile_lifecycle()
    evidence.clear()
    arb.reconcile_lifecycle()
    assert arb.panes["%8"].state_name == "unknown"
    assert clears == []


def test_tmux_inventory_failure_preserves_state(arbiter, monkeypatch):
    mod, arb, _, _, clears = arbiter
    arb.reconcile_lifecycle()
    def fail(*a, **kw):
        raise subprocess.TimeoutExpired("tmux", 1)
    monkeypatch.setattr(mod.subprocess, "run", fail)
    assert arb.reconcile_lifecycle() == 0
    assert arb.panes["%8"].state_name == "working"
    assert clears == []


def test_newer_permission_hook_wins_until_native_boundary_moves(arbiter):
    _, arb, evidence, _, _ = arbiter
    arb.reconcile_lifecycle()
    p = arb.panes["%8"]
    p.state_name, p.last_source = "asking", "hook"
    p.updated_at = time.time()
    arb.reconcile_lifecycle()
    assert arb.panes["%8"].state_name == "asking"
    arb.reconcile_lifecycle()
    assert arb.panes["%8"].state_name == "asking"
    evidence["%8"].update(state="idle", updated_at=time.time() + 1)
    arb.reconcile_lifecycle()
    assert arb.panes["%8"].state_name == "unread"


def test_observed_at_native_status_releases_permission_hook(arbiter):
    _, arb, evidence, _, _ = arbiter
    evidence["%8"]["provider"] = "claude"
    arb.reconcile_lifecycle()
    arb.apply_event("PermissionRequest", "%8", {})
    evidence["%8"].pop("updated_at")
    evidence["%8"].update(state="idle", observed_at=time.time() + 1)
    arb.reconcile_lifecycle()
    assert arb.panes["%8"].state_name == "unread"


def test_older_claude_busy_cannot_overwrite_completion_or_read(arbiter):
    _, arb, evidence, _, _ = arbiter
    evidence["%8"].update(provider="claude", source="claude-session")
    arb.reconcile_lifecycle()
    arb.apply_event("Stop", "%8", {"_provider": "claude"})
    for _ in range(3):
        arb.reconcile_lifecycle()
        assert arb.panes["%8"].state_name == "unread"
    arb.apply_event("PaneRead", "%8", {})
    arb.reconcile_lifecycle()
    assert arb.panes["%8"].state_name == "idle"
    evidence["%8"]["updated_at"] = time.time() + 1
    arb.reconcile_lifecycle()
    assert arb.panes["%8"].state_name == "working"
