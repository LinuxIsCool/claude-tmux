"""Exercise indicator writes and terminal redraws against an isolated tmux."""
from __future__ import annotations

import fcntl
import asyncio
import importlib.util
import json
import os
from pathlib import Path
import pty
import select
import shlex
import shutil
import signal
import socket
import struct
import subprocess
import sys
import termios
import tempfile
import threading
import time
from dataclasses import replace

import pytest


@pytest.fixture
def tmux_writer(tmp_path, monkeypatch):
    if not shutil.which("tmux"):
        pytest.skip("tmux is required")
    cmd = ["tmux", "-S", str(tmp_path / "tmux.sock")]
    env = {k: v for k, v in os.environ.items() if k != "TMUX"}
    env["TERM"] = "xterm-256color"
    subprocess.run(
        [*cmd, "-f", "/dev/null", "new-session", "-d", "-s", "writer", "sleep 120"],
        check=True, env=env,
    )
    client = None
    master, slave = pty.openpty()
    try:
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 100, 0, 0))
        subprocess.run([*cmd, "set", "-g", "status-interval", "0"], check=True)
        subprocess.run([*cmd, "set", "-w", "automatic-rename", "off"], check=True)
        client = subprocess.Popen(
            [*cmd, "attach-session", "-t", "writer"],
            stdin=slave, stdout=slave, stderr=slave, env=env,
        )
        path = Path(__file__).resolve().parents[1] / "scripts/tmux-arbiter.py"
        spec = importlib.util.spec_from_file_location("tmux_writer_test", path)
        mod = importlib.util.module_from_spec(spec)
        monkeypatch.setitem(sys.modules, spec.name, mod)
        spec.loader.exec_module(mod)
        monkeypatch.setattr(mod, "TMUX_CMD", cmd)
        monkeypatch.setattr(mod, "ARBITER_LOG", tmp_path / "arbiter.log")
        pane_id = subprocess.check_output(
            [*cmd, "display-message", "-p", "#{pane_id}"], text=True,
        ).strip()
        pane = mod.PaneState(
            pane_id=pane_id, state_name="working", color="#a6e3a1", glyph="🟢",
            persona="⌘", updated_at=time.time(), session_start_at=0,
            hook_emoji="🔧  ", focus_emoji="🌱", active_agent_count=0,
        )

        def terminal_output():
            output = bytearray()
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                if not select.select([master], [], [], 0.2)[0]:
                    break
                output.extend(os.read(master, 65536))
            return bytes(output)

        terminal_output()
        yield mod, cmd, pane, terminal_output
    finally:
        subprocess.run([*cmd, "kill-server"], capture_output=True)
        if client is not None:
            client.wait(timeout=5)
        os.close(master)
        os.close(slave)


def test_repeated_state_does_not_repaint_terminal(tmux_writer):
    mod, cmd, pane, output = tmux_writer
    mod.TmuxWriter.write_state(pane)
    output()
    for _ in range(5):
        mod.TmuxWriter.write_state(pane)
    assert output() == b"", "unchanged indicators must produce no terminal redraw"


def test_two_real_servers_with_same_pane_cannot_cross_route(tmux_writer, tmp_path):
    mod, cmd, pane, output = tmux_writer
    foreign = ["tmux", "-S", str(tmp_path / "foreign.sock")]
    subprocess.run([*foreign, "-f", "/dev/null", "new-session", "-d", "sleep 120"], check=True)
    try:
        foreign_pane = subprocess.check_output(
            [*foreign, "display-message", "-p", "#{pane_id}"], text=True).strip()
        assert foreign_pane == pane.pane_id, "exercise the colliding pane number"
        shell = int(subprocess.check_output(
            [*cmd, "display-message", "-p", "#{pane_pid}"], text=True))
        raw = Path(f"/proc/{shell}/stat").read_text()
        start = raw[raw.rindex(")") + 2:].split()[19]
        arb = object.__new__(mod.TmuxArbiter)
        arb.config = mod.Config()
        arb.lifecycle_bindings = {pane.pane_id: {
            "provider": "claude", "session_id": "owner", "process_id": shell,
            "process_start": start}}
        arb.rejected_events = {}
        accepted = []
        arb.apply_event = lambda **event: accepted.append(event)
        server = object.__new__(mod.ArbiterServer)
        server.arbiter = arb
        mod.TmuxWriter.write_state(pane)
        output()
        event = {"type": "event", "event": "PreToolUse", "pane": foreign_pane,
                 "data": {"session_id": "owner", "_provider": "claude"},
                 "source": {"session_id": "owner", "provider": "claude",
                            "socket_path": foreign[2], "sent_at": time.time()}}
        assert asyncio.run(server._dispatch(event)) == {
            "type": "event_rejected", "reason": "server-mismatch"}
        assert asyncio.run(server._dispatch({"type": "window_refresh", "pane": foreign_pane,
            "mark_read": True, "socket_path": foreign[2]}))["type"] == "error"
        assert accepted == []
        assert output() == b"", "foreign events cannot redraw the monitored server"
        event["source"]["socket_path"] = cmd[2]
        assert asyncio.run(server._dispatch(event)) is None
        assert len(accepted) == 1, "the same event with the actual owner is accepted"
    finally:
        subprocess.run([*foreign, "kill-server"], check=True)


def test_changes_are_batched_and_external_drift_is_repaired(tmux_writer, monkeypatch):
    mod, cmd, pane, output = tmux_writer
    calls = []
    original = mod.TmuxWriter._run

    def record(args):
        calls.append(args)
        return original(args)

    monkeypatch.setattr(mod.TmuxWriter, "_run", staticmethod(record))
    mod.TmuxWriter.write_state(pane)
    assert len(calls) == 1, "one state update must use one tmux mutation batch"
    calls.clear()
    mod.TmuxWriter.write_state(pane)
    assert calls == []
    # An external writer changes the projection after our successful write.
    subprocess.run(
        [*cmd, "set", "-p", "-t", pane.pane_id, "@claude_color", "#badbad"], check=True,
    )
    mod.TmuxWriter.write_state(pane)
    assert len(calls) == 1
    assert subprocess.check_output(
        [*cmd, "show-options", "-pv", "-t", pane.pane_id, "@claude_color"], text=True,
    ).strip() == pane.color
    assert "@claude_window_color" not in calls[0], "unchanged fallback must be skipped"


def test_removed_decorations_preserve_voice_and_last_hook(tmux_writer):
    mod, cmd, pane, output = tmux_writer
    subprocess.run([*cmd, "set", "-p", "@claude_voice", "🎤"], check=True)
    subprocess.run([*cmd, "set", "-w", "@claude_window_voice", "🌟"], check=True)
    mod.TmuxWriter.write_state(pane)
    cleared = replace(pane, persona="", focus_emoji="", hook_emoji="", active_agent_count=0)
    mod.TmuxWriter.write_state(cleared)
    pane_options = subprocess.check_output([*cmd, "show-options", "-p"], text=True)
    window_options = subprocess.check_output([*cmd, "show-options", "-w"], text=True)
    for name in ("@claude_persona", "@claude_focus", "@claude_agents"):
        assert name not in pane_options
    for name in ("@claude_window_focus", "@claude_window_agents"):
        assert name not in window_options
    assert "@claude_voice 🎤" in pane_options
    assert "@claude_window_voice 🌟" in window_options
    assert subprocess.check_output(
        [*cmd, "show-options", "-pv", "@claude_hook"], text=True,
    ).rstrip("\n") == pane.hook_emoji


def test_failed_write_is_retried_from_tmux_truth(tmux_writer, monkeypatch):
    mod, cmd, pane, output = tmux_writer
    with monkeypatch.context() as failing:
        failing.setattr(mod.TmuxWriter, "_run", staticmethod(lambda args: False))
        mod.TmuxWriter.write_state(pane)
    mod.TmuxWriter.write_state(pane)
    assert subprocess.check_output(
        [*cmd, "show-options", "-pv", "@claude_state"], text=True,
    ).strip() == pane.glyph


def test_failed_read_does_not_mutate(tmux_writer, monkeypatch):
    mod, cmd, pane, output = tmux_writer
    original = mod.subprocess.run

    def fail_read(args, **kwargs):
        if "show-options" in args:
            return subprocess.CompletedProcess(args, 1, stdout="", stderr="unreachable")
        return original(args, **kwargs)

    monkeypatch.setattr(mod.subprocess, "run", fail_read)
    mod.TmuxWriter.write_state(pane)
    assert "@claude_state" not in subprocess.check_output(
        [*cmd, "show-options", "-p"], text=True,
    )


def split_pane(cmd):
    return subprocess.check_output(
        [*cmd, "split-window", "-d", "-P", "-F", "#{pane_id}", "sleep 120"], text=True,
    ).strip()


def option(cmd, name, pane_id, scope="w"):
    return subprocess.check_output(
        [*cmd, "show-options", f"-{scope}qv", "-t", pane_id, name], text=True,
    ).rstrip("\n")


def test_idle_sibling_cannot_replace_working_badge(tmux_writer):
    mod, cmd, working, output = tmux_writer
    idle = replace(working, pane_id=split_pane(cmd), state_name="idle", color="#89b4fa",
                   persona="idle", focus_emoji="", hook_emoji="idle-event", active_agent_count=0)
    mod.TmuxWriter.write_state(working)
    mod.TmuxWriter.write_state(idle)
    assert option(cmd, "@claude_window_state_name", idle.pane_id) == "working"
    assert option(cmd, "@claude_window_color", idle.pane_id) == working.color
    assert option(cmd, "@claude_window_persona", idle.pane_id) == working.persona
    assert option(cmd, "@claude_window_hook", idle.pane_id) == working.hook_emoji
    assert option(cmd, "@claude_window_agents", idle.pane_id) == ""
    # The aggregate follows the next sibling after the working pane finishes.
    mod.TmuxWriter.write_state(replace(working, state_name="idle", color="#89b4fa",
                                      persona="", hook_emoji="finished", active_agent_count=0,
                                      focus_emoji=""))
    assert option(cmd, "@claude_window_state_name", idle.pane_id) == "idle"
    assert option(cmd, "@claude_window_persona", idle.pane_id) == ""
    assert option(cmd, "@claude_window_hook", idle.pane_id) == "finished"
    assert option(cmd, "@claude_window_agents", idle.pane_id) == ""


def test_other_working_pane_cannot_hide_running_children(tmux_writer):
    mod, cmd, ceo, output = tmux_writer
    cto = replace(ceo, pane_id=split_pane(cmd), active_agent_count=2, persona="CTO")
    mod.TmuxWriter.write_state(cto)
    mod.TmuxWriter.write_state(ceo)
    assert option(cmd, "@claude_window_state_name", ceo.pane_id) == "agents"
    assert option(cmd, "@claude_window_state", ceo.pane_id) == "💠"
    assert option(cmd, "@claude_state_name", cto.pane_id, "p") == "working"
    assert option(cmd, "@claude_window_agents", ceo.pane_id) == "💠2"
    mod.TmuxWriter.write_state(replace(cto, active_agent_count=0))
    assert option(cmd, "@claude_window_state_name", ceo.pane_id) == "working"
    assert option(cmd, "@claude_window_state", ceo.pane_id) == "🟢"
    assert option(cmd, "@claude_window_agents", ceo.pane_id) == ""


@pytest.mark.parametrize("names", [
    ("agents", "working"), ("agents", "asking"), ("asking", "error"),
    ("error", "unread"), ("unread", "unknown"), ("idle", "unknown"),
])
def test_window_priority_uses_semantic_state(tmux_writer, names):
    mod, cmd, pane, output = tmux_writer
    first, second = names
    higher = replace(pane, state_name=first, color="#123456", persona="higher")
    lower = replace(pane, pane_id=split_pane(cmd), state_name=second,
                    color="#654321", persona="lower")
    mod.TmuxWriter.write_state(higher)
    mod.TmuxWriter.write_state(lower)
    assert option(cmd, "@claude_window_state_name", pane.pane_id) == first
    assert option(cmd, "@claude_window_persona", pane.pane_id) == "higher"


def test_clear_recomputes_complete_badge_and_preserves_voice(tmux_writer):
    mod, cmd, working, output = tmux_writer
    idle = replace(working, pane_id=split_pane(cmd), state_name="idle", color="#89b4fa",
                   persona="", focus_emoji="", hook_emoji="idle-event", active_agent_count=0)
    mod.TmuxWriter.write_state(idle)
    mod.TmuxWriter.write_state(working)
    subprocess.run([*cmd, "set", "-p", "-t", working.pane_id, "@claude_voice", "🎤"], check=True)
    subprocess.run([*cmd, "set", "-w", "@claude_window_voice", "🌟"], check=True)
    mod.TmuxWriter.clear_state(working.pane_id)
    assert option(cmd, "@claude_window_state_name", idle.pane_id) == "idle"
    assert option(cmd, "@claude_window_color", idle.pane_id) == idle.color
    assert option(cmd, "@claude_window_hook", idle.pane_id) == idle.hook_emoji
    for field in ("persona", "focus", "agents"):
        assert option(cmd, "@claude_window_" + field, idle.pane_id) == ""
    assert option(cmd, "@claude_voice", working.pane_id, "p") == "🎤"
    mod.TmuxWriter.clear_state(idle.pane_id)
    for field in mod.TmuxWriter._FIELDS:
        assert option(cmd, "@claude_window_" + field, idle.pane_id) == ""
    assert option(cmd, "@claude_window_voice", idle.pane_id) == "🌟"
    output()
    mod.TmuxWriter.clear_state(idle.pane_id)
    assert output() == b""


def test_equal_priority_uses_stable_pane_id_after_pane_swap(tmux_writer):
    mod, cmd, first, output = tmux_writer
    second = replace(first, pane_id=split_pane(cmd), persona="second")
    mod.TmuxWriter.write_state(second)
    mod.TmuxWriter.write_state(first)
    subprocess.run([*cmd, "swap-pane", "-d", "-s", first.pane_id, "-t", second.pane_id], check=True)
    mod.TmuxWriter.refresh_window(second.pane_id)
    assert option(cmd, "@claude_window_persona", second.pane_id) == first.persona


def test_legacy_hook_color_is_included_in_window_aggregation(tmux_writer):
    mod, cmd, pane, output = tmux_writer
    sibling = split_pane(cmd)
    subprocess.run([*cmd, "set", "-p", "-t", sibling, "@claude_state", "⬤"], check=True)
    subprocess.run([*cmd, "set", "-p", "-t", sibling, "@claude_color", "#a6e3a1"], check=True)
    mod.TmuxWriter.write_state(replace(pane, state_name="idle", color="#89b4fa"))
    assert option(cmd, "@claude_window_color", pane.pane_id) == "#a6e3a1"
    assert option(cmd, "@claude_window_persona", pane.pane_id) == ""


@pytest.fixture
def script_env(tmux_writer, tmp_path):
    mod, cmd, pane, output = tmux_writer
    # Full setup executes against the isolated server and a disposable HOME.
    home = tmp_path / "home"
    (home / ".config/tmux").mkdir(parents=True)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    wrapper = bin_dir / "tmux"
    wrapper.write_text("#!/bin/sh\nexec " + shlex.join([shutil.which("tmux"), *cmd[1:]]) + ' "$@"\n')
    wrapper.chmod(0o755)
    systemctl = bin_dir / "systemctl"
    systemctl.write_text("#!/bin/sh\nexit 0\n")
    systemctl.chmod(0o755)
    env = dict(os.environ, HOME=str(home), PATH=str(bin_dir) + os.pathsep + os.environ["PATH"])
    env.pop("TMUX", None)
    env.pop("CLAUDE_TMUX_THEME", None)
    for key in ("HOME", "PATH"):
        subprocess.run([*cmd, "set-environment", "-g", key, env[key]], check=True)
        subprocess.run([*cmd, "set-environment", "-t", "writer", key, env[key]], check=True)
    return env


def run_script(name, env, *args):
    path = Path(__file__).resolve().parents[1] / "scripts" / name
    subprocess.run(["bash", str(path), *args], env=env, check=True, capture_output=True, text=True)


def test_focus_script_preserves_winning_badge_and_never_repaints_unchanged(tmux_writer, script_env):
    mod, cmd, working, output = tmux_writer
    idle = replace(working, pane_id=split_pane(cmd), state_name="idle", color="#89b4fa")
    mod.TmuxWriter.write_state(working)
    mod.TmuxWriter.write_state(idle)
    subprocess.run([*cmd, "select-pane", "-t", idle.pane_id], check=True)
    subprocess.run([*cmd, "set", "-w", "@claude_window_voice", "🌟"], check=True)
    output()
    for _ in range(3):
        run_script("tmux-window-state.sh", script_env)
    assert output() == b"", "focus aggregation must not rewrite unchanged options"
    assert option(cmd, "@claude_window_color", idle.pane_id) == working.color
    assert option(cmd, "@claude_window_voice", idle.pane_id) == "🌟"
    subprocess.run([*cmd, "kill-pane", "-t", working.pane_id], check=True)
    run_script("tmux-window-state.sh", script_env)
    assert option(cmd, "@claude_window_state_name", idle.pane_id) == "idle"


def test_focus_script_clears_legacy_unread_without_daemon(tmux_writer, script_env):
    mod, cmd, pane, output = tmux_writer
    subprocess.run([*cmd, "set", "-p", "@claude_state", "⬥"], check=True)
    subprocess.run([*cmd, "set", "-p", "@claude_color", "#b4befe"], check=True)
    run_script("tmux-window-state.sh", script_env)
    assert option(cmd, "@claude_state_name", pane.pane_id, "p") == "idle"
    assert option(cmd, "@claude_state", pane.pane_id, "p") == "🔵"
    assert option(cmd, "@claude_window_color", pane.pane_id) == "#89b4fa"


@pytest.mark.parametrize("acknowledged", [True, False])
def test_focus_script_delegates_to_daemon_and_falls_back_for_old_protocol(
    tmux_writer, script_env, acknowledged,
):
    mod, cmd, pane, output = tmux_writer
    mod.TmuxWriter.write_state(pane)
    subprocess.run([*cmd, "set", "-w", "@claude_window_color", "#123456"], check=True)
    # Unix socket paths are limited to 108 bytes, shorter than pytest paths.
    with tempfile.TemporaryDirectory(prefix="tmux-focus-", dir="/tmp") as home:
        sock_dir = Path(home) / ".claude/local/tmux"
        sock_dir.mkdir(parents=True)
        listener = socket.socket(socket.AF_UNIX)
        listener.bind(str(sock_dir / "arbiter.sock"))
        listener.listen(1)
        listener.settimeout(5)
        messages = []

        def respond():
            with listener:
                connection, _ = listener.accept()
                with connection:
                    message = bytearray()
                    while b"\n" not in message:
                        message.extend(connection.recv(4096))
                    messages.append(json.loads(message))
                    kind = "window_refresh_ack" if acknowledged else "error"
                    connection.sendall((f'{{"type":"{kind}"}}\n').encode())

        worker = threading.Thread(target=respond)
        worker.start()
        run_script("tmux-window-state.sh", dict(script_env, HOME=home))
        worker.join(timeout=5)
        assert not worker.is_alive()
        assert messages == [{"type": "window_refresh", "pane": pane.pane_id,
                             "mark_read": True, "socket_path": cmd[2]}]
    expected = "#123456" if acknowledged else pane.color
    assert option(cmd, "@claude_window_color", pane.pane_id) == expected


def test_setup_renders_only_lifecycle_grammar_and_preserves_voice_options(tmux_writer, script_env):
    mod, cmd, working, output = tmux_writer
    idle = replace(working, pane_id=split_pane(cmd), state_name="idle", color="#89b4fa",
                   persona="idle-persona", hook_emoji="idle-event")
    mod.TmuxWriter.write_state(working)
    mod.TmuxWriter.write_state(idle)
    subprocess.run([*cmd, "select-pane", "-t", idle.pane_id], check=True)
    subprocess.run([*cmd, "set", "-p", "-t", idle.pane_id, "@claude_voice", "🎤"], check=True)
    subprocess.run([*cmd, "set", "-w", "@claude_window_voice", "🌟"], check=True)
    subprocess.run([*cmd, "set", "-g", "window-status-format",
                    "#{?#{@claude_state},legacy, }#I:#W"], check=True)
    subprocess.run([*cmd, "set", "-g", "window-status-current-format", "#[fg=#fffafa]#I:#W"], check=True)
    run_script("claude-tmux-setup.sh", script_env)
    formats = [option(cmd, name, idle.pane_id, "g") for name in
               ("window-status-format", "window-status-current-format")]
    current, other = [subprocess.check_output(
        [*cmd, "display-message", "-p", "-t", idle.pane_id, "#{E:" + name + "}"], text=True,
    ) for name in ("window-status-current-format", "window-status-format")]
    for rendered in (current, other):
        assert "🟢" in rendered
        assert working.persona not in rendered
        assert working.hook_emoji not in rendered
        assert "idle-persona" not in rendered
        assert "🎤" not in rendered and "🌟" not in rendered
        assert working.focus_emoji not in rendered
    assert option(cmd, "@claude_voice", idle.pane_id, "p") == "🎤"
    assert option(cmd, "@claude_window_voice", idle.pane_id) == "🌟"
    assert "legacy" not in other
    assert "#[fg=#fffafa]" in current
    run_script("claude-tmux-setup.sh", script_env)
    assert [option(cmd, name, idle.pane_id, "g") for name in
            ("window-status-format", "window-status-current-format")] == formats
    subprocess.run([*cmd, "kill-pane", "-t", working.pane_id], check=True)
    deadline = time.monotonic() + 3
    while option(cmd, "@claude_window_state_name", idle.pane_id) != "idle":
        assert time.monotonic() < deadline, "pane removal hook must recompute the window badge"
        time.sleep(0.02)
    assert option(cmd, "@claude_window_persona", idle.pane_id) == idle.persona
    subprocess.run([*cmd, "set", "-pu", "-t", idle.pane_id, "@claude_voice"], check=True)
    current = subprocess.check_output(
        [*cmd, "display-message", "-p", "-t", idle.pane_id,
         "#{E:window-status-current-format}"], text=True,
    )
    assert "🌟" not in current
    assert "🔵" in current
    assert option(cmd, "@claude_window_voice", idle.pane_id) == "🌟"
    output()


def test_pane_died_recomputes_lifecycle_without_writing_voice(tmux_writer, script_env):
    mod, cmd, working, output = tmux_writer
    idle = replace(working, pane_id=split_pane(cmd), state_name="idle", color="#89b4fa",
                   persona="idle", hook_emoji="idle", active_agent_count=0)
    mod.TmuxWriter.write_state(working)
    mod.TmuxWriter.write_state(idle)
    subprocess.run([*cmd, "set", "-p", "-t", working.pane_id, "@claude_voice", "🎤"], check=True)
    subprocess.run([*cmd, "set", "-w", "remain-on-exit", "on"], check=True)
    run_script("claude-tmux-setup.sh", script_env)
    pid = int(subprocess.check_output(
        [*cmd, "display-message", "-p", "-t", working.pane_id, "#{pane_pid}"], text=True,
    ))
    os.kill(pid, signal.SIGTERM)
    deadline = time.monotonic() + 3
    while option(cmd, "@claude_window_state_name", idle.pane_id) != "idle":
        assert time.monotonic() < deadline, "pane-died must reveal the live idle sibling"
        time.sleep(0.02)
    assert option(cmd, "@claude_state_name", working.pane_id, "p") == ""
    assert option(cmd, "@claude_agents", working.pane_id, "p") == ""
    assert option(cmd, "@claude_voice", working.pane_id, "p") == "🎤"
    output()
