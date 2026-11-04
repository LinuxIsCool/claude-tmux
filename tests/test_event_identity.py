"""Lifecycle routing is bound to native executions, not reusable pane numbers."""
import importlib.util
from pathlib import Path
import time

import pytest


spec = importlib.util.spec_from_file_location(
    "event_identity_test", Path(__file__).resolve().parents[1] / "scripts/event_identity.py")
policy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(policy)


@pytest.fixture
def native_event():
    now = time.time()
    binding = {"provider": "claude", "session_id": "current-session",
               "process_id": 120, "process_start": "800"}
    message = {"type": "event", "event": "PreToolUse", "pane": "%0",
               "data": {"_provider": "claude", "session_id": "current-session"},
               "source": {"provider": "claude", "session_id": "current-session",
                          "socket_path": "/private/main.sock", "sent_at": now}}
    return message, binding, now


def reject(fixture):
    message, binding, now = fixture
    return policy.rejection_reason(message, binding, "/private/main.sock", now, 60)


def test_two_servers_with_same_pane_and_session_cannot_cross_route(native_event):
    assert reject(native_event) is None
    native_event[0]["source"]["socket_path"] = "/private/test.sock"
    assert reject(native_event) == "server-mismatch"


def test_hook_cannot_choose_provider_or_attach_to_another_session(native_event):
    message, _, _ = native_event
    message["data"]["_provider"] = "codex"
    assert reject(native_event) == "provider-mismatch"
    message["data"]["_provider"] = "claude"
    message["data"]["session_id"] = "previous-session"
    assert reject(native_event) == "session-mismatch"


def test_legacy_completion_requires_current_native_owner(native_event):
    message, binding, now = native_event
    message.pop("source")
    message["event"] = "Stop"
    assert reject(native_event) is None
    binding["session_id"] = "replacement-session"
    assert reject(native_event) == "session-mismatch"
    assert policy.rejection_reason(message, None, "/private/main.sock", now, 60) == "unbound-pane"


def test_old_completion_cannot_end_new_turn_in_same_session(native_event):
    message, binding, _ = native_event
    message.pop("source")
    message["event"] = "Stop"
    message["data"]["turn_id"] = "completed-turn"
    binding.update(state="working", turn_id="new-turn")
    assert reject(native_event) == "turn-mismatch"


@pytest.mark.parametrize("age", [61, -6])
def test_delayed_and_future_dated_events_are_rejected(native_event, age):
    message, _, now = native_event
    message["source"]["sent_at"] = now - age
    assert reject(native_event) == "stale-event"


def test_event_older_than_native_boundary_cannot_reopen_completed_turn(native_event):
    _, binding, now = native_event
    binding.update(state="idle", observed_at=now + 1)
    assert reject(native_event) == "superseded-event"


@pytest.mark.parametrize("bad", [None, "now", float("nan"), float("inf"), True])
def test_invalid_event_clock_is_rejected(native_event, bad):
    native_event[0]["source"]["sent_at"] = bad
    assert reject(native_event) == "malformed-timestamp"


@pytest.mark.parametrize("message", [None, [], {}, {"type": "event", "event": "PreToolUse",
                                                   "pane": "%0", "data": []}])
def test_malformed_events_cannot_enter_state_machine(native_event, message):
    _, binding, now = native_event
    assert policy.rejection_reason(message, binding, "", now, 60) == "malformed-event"


def write_process(root, pid, parent, start):
    directory = root / str(pid)
    directory.mkdir(exist_ok=True)
    # Linux stat fields 3..22. Command names may contain spaces and parentheses.
    fields = ["S", str(parent)] + ["0"] * 17 + [str(start)]
    (directory / "stat").write_text(f"{pid} (test (agent)) " + " ".join(fields))


def test_pid_reuse_and_different_pane_ancestry_invalidate_binding(tmp_path):
    binding = {"process_id": 120, "process_start": "800"}
    write_process(tmp_path, 120, 100, 800)
    write_process(tmp_path, 100, 1, 500)
    assert policy.process_is_current(binding, 100, tmp_path)
    assert not policy.process_is_current(binding, 101, tmp_path)
    write_process(tmp_path, 120, 100, 801)
    assert not policy.process_is_current(binding, 100, tmp_path)
    (tmp_path / "120/stat").unlink()
    assert not policy.process_is_current(binding, 100, tmp_path)
