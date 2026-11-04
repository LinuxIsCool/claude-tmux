"""Native lifecycle evidence must remain attached to the correct live CLI."""

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from codex_lifecycle import Collector


ROOT = "00000000-0000-4000-8000-000000000001"
CHILD = "00000000-0000-4000-8000-000000000002"


def row(kind, timestamp="2026-11-03T12:00:00Z", turn="t1"):
    return json.dumps({"type": "event_msg", "timestamp": timestamp,
                       "payload": {"type": kind, "turn_id": turn}}) + "\n"


@pytest.fixture
def native(tmp_path):
    proc = tmp_path / "proc"
    home = tmp_path / "home"
    sessions = home / ".codex/sessions/2026/11/03"
    locks = home / ".codex/thread-writer-locks"
    sessions.mkdir(parents=True)
    locks.mkdir(parents=True)
    processes = {}
    for pid, ppid, name in [(10, 1, "tmux"), (20, 10, "fish"), (30, 20, "codex")]:
        directory = proc / str(pid)
        (directory / "fd").mkdir(parents=True)
        # /proc stat starttime is the twentieth field after comm.
        fields = ["S", str(ppid)] + ["0"] * 17 + [str(pid * 100)]
        (directory / "stat").write_text(f"{pid} ({name}) " + " ".join(fields))
        processes[pid] = SimpleNamespace(pid=pid, ppid=ppid, start=str(pid * 100),
            comm=name, exe=f"/bin/{name}", args=(name,), cwd="/project",
            environment={"TMUX": "/tmp/tmux/default,10,0", "TMUX_PANE": "%1"})

    def rollout(thread=ROOT, parent=None, events="", hold=True):
        path = sessions / f"rollout-{thread}.jsonl"
        path.write_text(json.dumps({"type": "session_meta", "payload": {
            "id": thread, "source": "cli" if parent is None else {"subagent": {}},
            "parent_thread_id": parent}}) + "\n" + events)
        lock = locks / f"{thread}.lock"
        lock.touch()
        if hold:
            directory = proc / "30/fd"
            index = len(list(directory.iterdir()))
            (directory / str(index)).symlink_to(path)
            (directory / str(index + 1)).symlink_to(lock)
        return path

    return SimpleNamespace(collector=Collector(home, proc), processes=processes,
        panes={"%1": {"pid": 20, "command": "codex"}}, rollout=rollout, proc=proc)


def poll(native):
    return native.collector.poll(native.panes, native.processes)


def test_root_completion_ignores_child_activity_and_final_text(native):
    path = native.rollout(events=row("task_started") + row("task_complete"))
    native.rollout(CHILD, ROOT, row("task_started", "2026-11-03T12:10:00Z"))
    result = poll(native)["%1"]
    assert result["state"] == "idle"
    assert result["session_id"] == ROOT
    assert result["updated_at"] == 1793707200
    assert result["process_id"] == 30
    with path.open("a") as handle:
        handle.write(row("task_started", "2026-11-03T12:20:00Z", "t2"))
        handle.write(json.dumps({"type": "response_item", "payload": {
            "type": "message", "role": "assistant", "phase": "final_answer"}}) + "\n")
    assert poll(native)["%1"]["state"] == "working"


def test_abort_and_partial_append_are_incremental(native):
    path = native.rollout(events=row("turn_started"))
    assert poll(native)["%1"]["state"] == "working"
    abort = row("turn_aborted", "2026-11-03T12:02:00Z")
    with path.open("a") as handle:
        handle.write(abort[:30])
    assert poll(native)["%1"]["state"] == "unknown"
    with path.open("a") as handle:
        handle.write(abort[30:])
    assert poll(native)["%1"]["event"] == "turn_aborted"
    assert poll(native)["%1"]["state"] == "idle"


def test_partial_new_turn_cannot_preserve_previous_idle(native):
    path = native.rollout(events=row("task_complete"))
    assert poll(native)["%1"]["state"] == "idle"
    start = row("task_started", "2026-11-03T12:02:00Z", "t2")
    with path.open("a") as handle:
        handle.write(start[:30])
    assert poll(native)["%1"]["state"] == "unknown"
    assert poll(native)["%1"]["state"] == "unknown"
    with path.open("a") as handle:
        handle.write(start[30:])
    result = poll(native)["%1"]
    assert result["state"] == "working"
    assert result["turn_id"] == "t2"


@pytest.mark.parametrize("damaged", [
    '{"type":"event_msg","payload":{"type":"task_started"}\n',
    '{"type":"event_msg","payload":}\n',
    json.dumps({"type": "event_msg", "payload": {}}) + "\n",
    json.dumps({"type": "event_msg", "payload": {"type": "task_started"}}) + "\n",
    json.dumps({"type": "event_msg", "timestamp": "invalid", "payload": {"type": "task_started"}}) + "\n",
])
def test_malformed_lifecycle_record_cannot_preserve_previous_idle(native, damaged):
    path = native.rollout(events=row("task_complete"))
    assert poll(native)["%1"]["state"] == "idle"
    with path.open("a") as handle:
        handle.write(damaged)
    assert poll(native)["%1"]["state"] == "unknown"
    assert poll(native)["%1"]["state"] == "unknown"
    with path.open("a") as handle:
        handle.write(row("task_started", "2026-11-03T12:02:00Z", "t2"))
    assert poll(native)["%1"]["state"] == "working"


def test_shared_project_other_pane_does_not_bind(native):
    native.rollout(events=row("task_started"))
    native.processes[30].environment["TMUX_PANE"] = "%2"
    assert poll(native)["%1"]["state"] == "unknown"


def test_reused_pid_is_unknown_and_exit_removes_result(native):
    native.rollout(events=row("task_started"))
    native.processes[30].start = "old-start"
    assert poll(native)["%1"]["state"] == "unknown"
    del native.processes[30]
    assert poll(native) == {}


def test_missing_lock_and_multiple_roots_fail_closed(native):
    native.rollout(events=row("task_started"))
    native.rollout(CHILD, events=row("task_complete"))
    assert poll(native)["%1"]["state"] == "unknown"
    for fd in (native.proc / "30/fd").iterdir():
        if str(fd.readlink()).endswith(".lock"):
            fd.unlink()
    assert poll(native)["%1"]["state"] == "unknown"


def test_app_server_is_never_a_cli(native):
    native.rollout(events=row("task_started"))
    native.processes[30].args = ("codex", "app-server")
    assert poll(native) == {}


def test_tool_error_and_unpersisted_approval_are_not_completion(native):
    native.rollout(events=row("task_started") + row("error") + row("exec_approval_request"))
    assert poll(native)["%1"]["state"] == "working"


def test_cold_scan_is_bounded_and_does_not_reuse_truncated_boundary(native, monkeypatch):
    import codex_lifecycle
    path = native.rollout(events=row("task_complete"))
    assert poll(native)["%1"]["state"] == "idle"
    monkeypatch.setattr(codex_lifecycle, "MAX_READ", 512)
    with path.open("a") as handle:
        handle.write(row("task_started"))
        handle.write(json.dumps({"type": "response_item", "payload": "x" * 3000}) + "\n")
    assert poll(native)["%1"]["state"] == "unknown"
    with path.open("a") as handle:
        handle.write(row("task_complete", "2026-11-03T12:30:00Z"))
    assert poll(native)["%1"]["state"] == "idle"


def test_broken_server_ancestry_and_non_cli_header_rejected(native):
    native.rollout(ROOT, CHILD, row("task_started"))
    assert poll(native)["%1"]["state"] == "unknown"
    native.processes[20].ppid = 1
    assert poll(native)["%1"]["state"] == "unknown"


def add_process(native, pid, ppid, provider):
    template = native.processes[30]
    directory = native.proc / str(pid)
    (directory / "fd").mkdir(parents=True)
    fields = ["S", str(ppid)] + ["0"] * 17 + [str(pid * 100)]
    (directory / "stat").write_text(f"{pid} ({provider}) " + " ".join(fields))
    native.processes[pid] = SimpleNamespace(pid=pid, ppid=ppid, start=str(pid * 100),
        comm=provider, exe=f"/bin/{provider}", args=(provider,), cwd=template.cwd,
        environment=dict(template.environment))


def test_codex_tool_inside_claude_cannot_replace_outer_owner(native):
    native.rollout(events=row("task_complete"))
    add_process(native, 25, 20, "claude")
    native.processes[30].ppid = 25
    assert poll(native) == {}


def test_nested_claude_and_codex_do_not_hide_outer_codex(native):
    native.rollout(events=row("task_started"))
    add_process(native, 40, 30, "claude")
    add_process(native, 50, 40, "codex")
    result = poll(native)["%1"]
    assert result["process_id"] == 30
    assert result["state"] == "working"


@pytest.mark.parametrize("title", [
    "[ ! ] Action Required | project", "[ . ] Action Required | project",
    "● [ ! ] Action Required | project", "[ ! ] Action Required",
])
def test_native_action_required_title_is_positive_input_evidence(native, title):
    native.rollout(events=row("task_started"))
    native.panes["%1"]["title"] = title
    result = poll(native)["%1"]
    assert result["state"] == "asking"
    assert result["event"] == "action_required_title"
    native.panes["%1"]["title"] = "working title"
    assert poll(native)["%1"]["state"] == "working"


@pytest.mark.parametrize("title", [
    "Action Required", "[ ! ] Action Requiredness", "prefix [ ! ] Action Required | project",
    "[?] Action Required | project", "●● [ ! ] Action Required | project",
])
def test_other_titles_do_not_imply_input_required(native, title):
    native.rollout(events=row("task_started"))
    native.panes["%1"]["title"] = title
    assert poll(native)["%1"]["state"] == "working"


def test_title_without_bound_root_cannot_claim_asking(native):
    native.panes["%1"]["title"] = "[ ! ] Action Required | project"
    assert poll(native)["%1"]["state"] == "unknown"
