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
GRANDCHILD = "00000000-0000-4000-8000-000000000003"


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


def test_live_replaced_executable_preserves_identity_and_turn_boundaries(native):
    path = native.rollout(events=row("task_started"))
    native.processes[30].exe = "/bin/codex (deleted)"
    active = poll(native)["%1"]
    assert (active["state"], active["session_id"], active["process_id"]) == ("working", ROOT, 30)
    with path.open("a") as handle:
        handle.write(row("task_complete", "2026-11-03T12:02:00Z"))
    completed = poll(native)["%1"]
    assert (completed["state"], completed["session_id"], completed["process_id"]) == ("idle", ROOT, 30)


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


def cold_scan_limits(monkeypatch):
    import codex_lifecycle
    monkeypatch.setattr(codex_lifecycle, "MAX_READ", 512)
    monkeypatch.setattr(codex_lifecycle, "MAX_BACKSCAN", 8192)
    return (json.dumps({"type": "response_item", "payload": "x" * 97}) + "\n") * 30


@pytest.mark.parametrize("event,state", [("task_started", "working"), ("task_complete", "idle")])
def test_cold_scan_finds_latest_boundary_beyond_tail(native, monkeypatch, event, state):
    padding = cold_scan_limits(monkeypatch)
    path = native.rollout(events=row("task_complete") + row(event, turn="latest") + padding)
    result = poll(native)["%1"]
    assert result["state"] == state
    assert result["turn_id"] == "latest"
    assert native.collector._tails[path]["offset"] == path.stat().st_size
    assert poll(native)["%1"]["state"] == state
    with path.open("a") as handle:
        handle.write(row("turn_aborted", "2026-11-03T12:30:00Z", "latest"))
    assert poll(native)["%1"]["event"] == "turn_aborted"


@pytest.mark.parametrize("replace_inode", [False, True])
def test_reset_searches_old_boundary_without_reusing_previous_idle(native, monkeypatch, replace_inode):
    padding = cold_scan_limits(monkeypatch)
    path = native.rollout(events=row("task_complete") + padding + padding.splitlines(keepends=True)[0] * 5)
    assert poll(native)["%1"]["state"] == "idle"
    replacement = path.read_text().splitlines(keepends=True)[0] + row("task_started", turn="replacement") + padding
    if replace_inode:
        temporary = path.with_suffix(".new")
        temporary.write_text(replacement)
        temporary.replace(path)
    else:
        path.write_text(replacement)
    result = poll(native)["%1"]
    assert result["state"] == "working"
    assert result["turn_id"] == "replacement"


def test_cold_scan_stops_at_corruption_before_historical_idle(native, monkeypatch):
    padding = cold_scan_limits(monkeypatch)
    path = native.rollout(events=row("task_complete") + '{"type":"event_msg","payload":}\n' + padding)
    assert poll(native)["%1"]["state"] == "unknown"
    assert poll(native)["%1"]["state"] == "unknown"
    with path.open("a") as handle:
        handle.write(row("task_started", turn="recovered"))
    assert poll(native)["%1"]["turn_id"] == "recovered"


def test_cold_partial_tail_stays_unknown_until_boundary_finishes(native, monkeypatch):
    padding = cold_scan_limits(monkeypatch)
    start = row("task_started", turn="new")
    path = native.rollout(events=row("task_complete") + padding + start[:30])
    assert poll(native)["%1"]["state"] == "unknown"
    assert poll(native)["%1"]["state"] == "unknown"
    with path.open("a") as handle:
        handle.write(start[30:])
    result = poll(native)["%1"]
    assert result["state"] == "working"
    assert result["turn_id"] == "new"


@pytest.mark.parametrize("has_boundary", [False, True])
def test_cold_search_has_fixed_io_budget_and_unchanged_polls_do_not_rescan(native, monkeypatch, has_boundary):
    padding = cold_scan_limits(monkeypatch)
    path = native.rollout(events=(row("task_started") if has_boundary else "") + padding * 3)
    original = Path.open
    reads = []

    class CountReads:
        def __init__(self, handle):
            self.handle = handle

        def __enter__(self):
            self.handle.__enter__()
            return self

        def __exit__(self, *args):
            return self.handle.__exit__(*args)

        def __getattr__(self, name):
            return getattr(self.handle, name)

        def read(self, length=-1):
            assert 0 <= length <= 512
            data = self.handle.read(length)
            reads.append(len(data))
            return data

    def counted_open(candidate, *args, **kwargs):
        handle = original(candidate, *args, **kwargs)
        return CountReads(handle) if candidate == path else handle

    monkeypatch.setattr(Path, "open", counted_open)
    assert native.collector._boundary(path) == {}
    assert sum(reads) == 8192
    reads.clear()
    for _ in range(3):
        assert native.collector._boundary(path) == {}
    assert reads == [0, 0, 0]


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


def test_direct_child_count_tracks_native_followup_and_completion(native):
    native.rollout(events=row("task_started"))
    child = native.rollout(CHILD, ROOT, row("task_started"))
    first = poll(native)["%1"]
    assert first["child_count_known"] is True
    assert first["active_agent_count"] == 1
    assert poll(native)["%1"]["active_agent_count"] == 1
    with child.open("a") as handle:
        handle.write(row("task_complete", "2026-11-03T12:02:00Z"))
    assert poll(native)["%1"]["active_agent_count"] == 0
    with child.open("a") as handle:
        handle.write(row("task_started", "2026-11-03T12:03:00Z", "followup"))
    assert poll(native)["%1"]["active_agent_count"] == 1
    with child.open("a") as handle:
        handle.write(row("turn_aborted", "2026-11-03T12:04:00Z", "followup"))
    assert poll(native)["%1"]["active_agent_count"] == 0


def test_only_direct_active_children_count_even_when_parent_is_idle(native):
    native.rollout(events=row("task_complete"))
    child = native.rollout(CHILD, ROOT, row("task_complete"))
    native.rollout(GRANDCHILD, CHILD, row("task_started"))
    result = poll(native)["%1"]
    assert result["state"] == "idle"
    assert result["active_agent_count"] == 0
    with child.open("a") as handle:
        handle.write(row("task_started", "2026-11-03T12:03:00Z"))
    result = poll(native)["%1"]
    assert result["state"] == "idle"
    assert result["active_agent_count"] == 1


def test_unowned_and_unrelated_rollouts_do_not_create_children(native):
    native.rollout(events=row("task_started"))
    native.rollout(CHILD, ROOT, row("task_started"), hold=False)
    native.rollout(GRANDCHILD, CHILD, row("task_started"))
    result = poll(native)["%1"]
    assert result["child_count_known"] is True
    assert result["active_agent_count"] == 0


def test_child_count_disappears_on_incomplete_boundary_and_recovers(native):
    native.rollout(events=row("task_started"))
    child = native.rollout(CHILD, ROOT, row("task_complete"))
    assert poll(native)["%1"]["active_agent_count"] == 0
    start = row("task_started", "2026-11-03T12:02:00Z")
    with child.open("a") as handle:
        handle.write(start[:30])
    result = poll(native)["%1"]
    assert result["state"] == "working"
    assert result["child_count_known"] is False
    assert "active_agent_count" not in result
    with child.open("a") as handle:
        handle.write(start[30:])
    assert poll(native)["%1"]["active_agent_count"] == 1


@pytest.mark.parametrize("remove_rollout", [False, True])
def test_incomplete_child_writer_ownership_is_unknown(native, remove_rollout):
    native.rollout(events=row("task_started"))
    child = native.rollout(CHILD, ROOT, row("task_started"))
    assert poll(native)["%1"]["active_agent_count"] == 1
    for fd in (native.proc / "30/fd").iterdir():
        target = fd.readlink()
        remove = target == child if remove_rollout else target.name == f"{CHILD}.lock"
        if remove:
            fd.unlink()
    result = poll(native)["%1"]
    assert result["child_count_known"] is False
    assert "active_agent_count" not in result


def test_released_child_descriptors_clear_count_and_cached_evidence(native):
    native.rollout(events=row("task_started"))
    child = native.rollout(CHILD, ROOT, row("task_started"))
    assert poll(native)["%1"]["active_agent_count"] == 1
    for fd in (native.proc / "30/fd").iterdir():
        target = fd.readlink()
        if target == child or target.name == f"{CHILD}.lock":
            fd.unlink()
    result = poll(native)["%1"]
    assert result["child_count_known"] is True
    assert result["active_agent_count"] == 0
    assert child not in native.collector._tails


def test_unknown_parent_binding_never_reports_child_count(native):
    native.rollout(CHILD, ROOT, row("task_started"))
    result = poll(native)["%1"]
    assert result["state"] == "unknown"
    assert "child_count_known" not in result
    assert "active_agent_count" not in result


def test_multiple_direct_workers_count_and_corrupt_child_never_becomes_zero(native):
    native.rollout(events=row("task_started"))
    native.rollout(CHILD, ROOT, row("task_started"))
    other = native.rollout(GRANDCHILD, ROOT, row("task_started"))
    assert poll(native)["%1"]["active_agent_count"] == 2
    with other.open("a") as handle:
        handle.write('{"type":"event_msg","payload":}\n')
    result = poll(native)["%1"]
    assert result["child_count_known"] is False
    assert "active_agent_count" not in result
