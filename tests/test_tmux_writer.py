"""Exercise indicator writes and terminal redraws against an isolated tmux."""
from __future__ import annotations

import fcntl
import importlib.util
import os
from pathlib import Path
import pty
import select
import shutil
import struct
import subprocess
import sys
import termios
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
            pane_id=pane_id, state_name="working", color="#a6e3a1", glyph="⬤",
            persona="⌘", updated_at=time.time(), session_start_at=0,
            hook_emoji="🔧  ", focus_emoji="🌱", active_agent_count=2,
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
