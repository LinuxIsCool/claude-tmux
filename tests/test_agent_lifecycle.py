"""Lifecycle regressions use native records, never terminal activity guesses."""
import importlib.util
import json
from pathlib import Path
import sys

import pytest


spec = importlib.util.spec_from_file_location("agent_lifecycle", Path(__file__).parents[1] / "scripts/agent_lifecycle.py")
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value))


@pytest.fixture
def environment(tmp_path):
    home, proc = tmp_path / "home", tmp_path / "proc"
    home.mkdir(); proc.mkdir()
    (proc / "stat").write_text("btime 1000\n")
    collector = module.Collector(home, proc)
    process = module.Process(12, 11, "100", "claude", cwd="/project")
    (proc / "12").mkdir()
    fields = ["S", "11"] + ["0"] * 17 + ["100"]
    (proc / "12/stat").write_text("12 (claude) " + " ".join(fields))
    processes = {11: module.Process(11, 1, "99", "fish"), 12: process}
    return collector, process, processes


def claude_record(collector, process, **changes):
    record = {"pid": process.pid, "procStart": process.start, "tmux": "0:@0.%4",
              "sessionId": "session-one", "status": "busy", "statusUpdatedAt": 1000}
    record.update(changes)
    write(collector.home / ".claude/sessions/12.json", record)


@pytest.mark.parametrize("native,expected", [("busy", "working"), ("shell", "idle"),
    ("idle", "idle"), ("waiting", "asking"), ("unrecognized", "unknown")])
def test_native_claude_status_does_not_expire_during_quiet_wait(environment, native, expected):
    collector, process, processes = environment
    claude_record(collector, process, status=native)
    row = collector.poll({"%4": {"pid": 11, "command": "fish"}}, processes)["%4"]
    assert row["state"] == expected
    assert row["source"] == "claude-session"
    assert row["session_id"] == "session-one"
    assert "content" not in json.dumps(row)


@pytest.mark.parametrize("changes", [{"procStart": "older-process"}, {"pid": 99}, {"tmux": "0:@0.%5"}])
def test_stale_native_record_cannot_bind_to_another_process_or_pane(environment, changes):
    collector, process, processes = environment
    claude_record(collector, process, **changes)
    assert collector.poll({"%4": {"pid": 11}}, processes)["%4"]["state"] == "unknown"


def test_pid_domain_rejects_other_machine_or_pid_namespace(environment):
    collector, process, processes = environment
    machine = collector.proc_root / "12/root/etc/machine-id"
    machine.parent.mkdir(parents=True)
    machine.write_text("abcd\n")
    namespace = collector.proc_root / "12/ns/pid"
    namespace.parent.mkdir(parents=True)
    namespace.symlink_to("pid:[123]")
    claude_record(collector, process, pidDomain="linux:abcd:pid:[123]")
    assert collector.poll({"%4": {"pid": 11}}, processes)["%4"]["state"] == "working"
    claude_record(collector, process, pidDomain="linux:old:pid:[123]")
    assert collector.poll({"%4": {"pid": 11}}, processes)["%4"]["state"] == "unknown"


def test_exited_harness_is_absent_even_when_shell_pane_survives(environment):
    collector, process, processes = environment
    claude_record(collector, process)
    del processes[12]
    assert collector.poll({"%4": {"pid": 11}}, processes) == {}


def test_descendant_discovery_crosses_nested_bubblewrap(environment):
    collector, process, processes = environment
    process.ppid = 14
    processes[13] = module.Process(13, 11, "1", "bwrap")
    processes[14] = module.Process(14, 13, "2", "bwrap")
    claude_record(collector, process)
    assert collector.poll({"%4": {"pid": 11}}, processes)["%4"]["process_id"] == 12


def test_outer_harness_owns_pane_instead_of_its_agent_child(environment):
    collector, process, processes = environment
    processes[13] = module.Process(13, 12, "2", "pi")
    claude_record(collector, process)
    assert collector.poll({"%4": {"pid": 11}}, processes)["%4"]["provider"] == "claude"


def session(path, messages, cwd="/project"):
    path.parent.mkdir(parents=True, exist_ok=True)
    records = [{"type": "session", "id": "pi-session", "cwd": cwd, "timestamp": "2026-11-03T10:00:00Z"}]
    records += [{"type": "message", "timestamp": "2026-11-03T10:01:00Z", "message": m} for m in messages]
    path.write_text("".join(json.dumps(record) + "\n" for record in records))


@pytest.mark.parametrize("message,expected", [
    ({"role": "user", "content": "private"}, "unknown"),
    ({"role": "toolResult"}, "unknown"),
    ({"role": "assistant", "stopReason": "toolUse"}, "unknown"),
    ({"role": "assistant", "stopReason": "stop"}, "unknown"),
    ({"role": "assistant", "stopReason": "aborted"}, "unknown"),
    ({"role": "assistant", "stopReason": "error"}, "unknown"),
])
def test_exact_pi_session_state_and_no_message_body_retention(environment, message, expected):
    collector, process, processes = environment
    path = collector.home / "explicit.jsonl"
    session(path, [message])
    process.comm = "pi"
    process.args = ("pi", "--session", str(path))
    row = collector.poll({"%4": {"pid": 11}}, processes)["%4"]
    assert row["state"] == expected
    assert row["session_id"] == "pi-session"
    assert "private" not in repr(row)
    assert "private" not in repr(vars(next(iter(collector.transcripts.values()))))


def test_transcript_partial_final_record_never_claims_previous_completion(environment):
    collector, process, processes = environment
    process.comm = "omp"
    path = collector.home / "exact.jsonl"
    process.args = ("omp", "--session", str(path))
    session(path, [{"role": "assistant", "stopReason": "stop"}])
    assert collector.poll({"%4": {"pid": 11}}, processes)["%4"]["state"] == "unknown"
    with path.open("a") as stream:
        stream.write('{"type":"message","message":{"role":"user"')
    assert collector.poll({"%4": {"pid": 11}}, processes)["%4"]["state"] == "unknown"
    with path.open("a") as stream:
        stream.write('}}\n')
    assert collector.poll({"%4": {"pid": 11}}, processes)["%4"]["last_message_state"] == "working"


def test_oversized_records_are_bounded_and_next_record_recovers(tmp_path):
    path = tmp_path / "log.jsonl"
    session(path, [{"role": "assistant", "stopReason": "stop"}])
    transcript = module.Transcript()
    assert transcript.poll(path)["state"] == "idle"
    with path.open("a") as stream:
        stream.write("x" * (module.MAX_LINE + 1))
    assert transcript.poll(path)["state"] == "unknown"
    assert len(transcript.partial) <= module.MAX_LINE
    with path.open("a") as stream:
        stream.write('\n{"type":"message","message":{"role":"user"}}\n')
    assert transcript.poll(path)["state"] == "working"


def test_rotated_or_truncated_transcript_discards_old_state(tmp_path):
    path = tmp_path / "log.jsonl"
    session(path, [{"role": "assistant", "stopReason": "stop"}])
    transcript = module.Transcript()
    assert transcript.poll(path)["state"] == "idle"
    path.unlink()
    session(path, [{"role": "user"}])
    assert transcript.poll(path)["state"] == "working"
    path.write_text("")
    assert transcript.poll(path)["state"] == "unknown"


def test_first_unpersisted_pi_request_is_unknown(environment):
    collector, process, processes = environment
    process.comm = "pi"
    assert collector.poll({"%4": {"pid": 11}}, processes)["%4"]["state"] == "unknown"


def test_unique_new_transcript_in_shared_cwd_cannot_bind_to_another_process(environment):
    collector, process, processes = environment
    process.comm = "pi"
    base = collector.home / ".pi/agent/sessions/project"
    session(base / "one.jsonl", [{"role": "user"}])
    assert collector.poll({"%4": {"pid": 11}}, processes)["%4"]["state"] == "unknown"
    session(base / "other-cwd.jsonl", [{"role": "user"}], cwd="/other")
    assert collector.poll({"%4": {"pid": 11}}, processes)["%4"]["state"] == "unknown"
    session(base / "ambiguous.jsonl", [{"role": "user"}])
    assert collector.poll({"%4": {"pid": 11}}, processes)["%4"]["state"] == "unknown"


def test_session_from_previous_process_is_not_selected_by_mtime(environment):
    collector, process, processes = environment
    process.comm = "pi"
    path = collector.home / ".pi/agent/sessions/project/old.jsonl"
    session(path, [{"role": "assistant", "stopReason": "stop"}])
    (collector.proc_root / "stat").write_text("btime 9999999999\n")
    assert collector.poll({"%4": {"pid": 11}}, processes)["%4"]["state"] == "unknown"


def test_explicit_session_cwd_mismatch_is_unknown(environment):
    collector, process, processes = environment
    path = collector.home / "exact.jsonl"
    session(path, [{"role": "user"}], cwd="/other")
    process.comm = "pi"
    process.args = ("pi", "--session", str(path))
    assert collector.poll({"%4": {"pid": 11}}, processes)["%4"]["state"] == "unknown"


def test_ambiguous_live_harnesses_are_unknown_not_absent(environment):
    collector, process, processes = environment
    processes[13] = module.Process(13, 11, "200", "omp")
    assert collector.poll({"%4": {"pid": 11}}, processes)["%4"]["source"] == "ambiguous-process"


def test_process_reuse_during_native_record_read_does_not_preserve_busy(environment):
    collector, process, processes = environment
    claude_record(collector, process)
    (collector.proc_root / "12/stat").write_text("12 (claude) S 11 " + "0 " * 17 + "999")
    assert collector.poll({"%4": {"pid": 11}}, processes)["%4"]["state"] == "unknown"


def test_namespace_bound_native_pi_boundary_overrides_advisory_transcript(environment):
    collector, process, processes = environment
    process.comm = "pi"
    (collector.proc_root / "12/status").write_text("Name:\tpi\nNSpid:\t12\t2\n")
    namespace = collector.proc_root / "12/ns/pid"
    namespace.parent.mkdir(parents=True)
    namespace.symlink_to("pid:[456]")
    path = collector.home / ".pi/agent/tmux-lifecycle/2-100.json"
    record = {"schema": 1, "provider": "pi", "pid": 2, "process_start": "100",
              "pid_namespace": "pid:[456]", "cwd": "/project", "state": "working",
              "session_id": "native-session", "event": "agent_start", "updated_at": 1000}
    write(path, record)
    assert collector.poll({"%4": {"pid": 11}}, processes)["%4"]["state"] == "working"
    record.update(state="idle", event="agent_end")
    write(path, record)
    row = collector.poll({"%4": {"pid": 11}}, processes)["%4"]
    assert row["state"] == "idle"
    assert row["source"] == "native-extension"
    record["pid_namespace"] = "pid:[previous]"
    write(path, record)
    assert collector.poll({"%4": {"pid": 11}}, processes)["%4"]["state"] == "unknown"


def test_codex_outer_owner_prevents_claude_child_from_owning_pane(environment):
    collector, process, processes = environment
    processes[11].comm = "codex"
    processes[11].exe = "/native/codex"
    claude_record(collector, process)
    assert collector.poll({"%4": {"pid": 11}}, processes) == {}
    assert [p.pid for p in module.outer_harness_processes(11, processes)] == [11]


def test_claude_outer_owner_prevents_codex_child_from_owning_pane(environment):
    collector, process, processes = environment
    processes[13] = module.Process(13, 12, "200", "codex", exe="/native/codex")
    claude_record(collector, process)
    assert [p.pid for p in module.outer_harness_processes(11, processes)] == [12]
    assert collector.poll({"%4": {"pid": 11}}, processes)["%4"]["provider"] == "claude"


def test_malformed_pi_header_does_not_degrade_another_live_pane(environment):
    collector, process, processes = environment
    process.comm = "pi"
    path = collector.home / "malformed.jsonl"
    path.write_text("[]\n")
    process.args = ("pi", "--session", str(path))
    processes[21] = module.Process(21, 1, "200", "fish")
    processes[22] = module.Process(22, 21, "201", "pi")
    rows = collector.poll({"%4": {"pid": 11}, "%5": {"pid": 21}}, processes)
    assert rows["%4"]["state"] == "unknown"
    assert rows["%5"]["state"] == "unknown"
