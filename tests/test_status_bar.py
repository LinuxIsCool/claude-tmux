"""Readable status rows, actual tmux projection and mouse window targets."""
from __future__ import annotations

import fcntl
import importlib.util
import os
from pathlib import Path
import pty
import re
import select
import shlex
import shutil
import struct
import subprocess
import sys
import termios
import time

import pytest


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("readable_status_bar", ROOT / "scripts/tmux_status_bar.py")
bar = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = bar
spec.loader.exec_module(bar)

NAMES = ["kids-studio", "Avalanche", "Indigenomics", "Longtail Financial", "Yami", "Legion",
         "Regen AI", "Oasis", "Symbiocene", "fish", "news-agent", "Pi-input-fixture"]
STATES = ["unread", "working", "agents", "idle", "unknown", "working", "unread", "unknown", "", "", "", "asking"]
CLOCK = "Tue Nov 03 2026 19:04 PST"


def windows():
    return [bar.Window(index, name, STATES[index], index == 5, bool(STATES[index]))
            for index, name in enumerate(NAMES)]


def visible_text(format_string):
    return re.sub(r"#\[[^\]]*\]", "", format_string).replace("##", "#")


def test_real_window_indices_and_words_fit_full_width_without_scrolling():
    rendered = bar.render_status_bar(232, windows(), session_name="0", clock=CLOCK, host="shawn@legion")
    assert len(rendered.rows) == 3
    assert rendered.visible_indices == tuple(range(12))
    assert rendered.hidden_indices == ()
    for row in rendered.rows:
        assert bar.columns(visible_text(row)) == 232
        assert "list=on" not in row
        assert "#{W:" not in row
    text = "\n".join(map(visible_text, rendered.rows))
    for index, name in enumerate(NAMES):
        assert f"{index}: {name}" in text
        assert f"#[range=window|{index}," in "".join(rendered.rows)
    assert ">5: Legion" in text
    assert "[WORK]" in text and "[IDLE]" in text and "[INPUT]" in text
    assert bar.LEGEND in visible_text(rendered.rows[0])
    assert visible_text(rendered.rows[0]).endswith(CLOCK)
    assert "⌘" not in text and "⬤" not in text and "🔧" not in text


@pytest.mark.parametrize("width", [80, 100, 120, 160, 232, 400])
def test_layout_adapts_without_losing_indices_at_supported_widths(width):
    rendered = bar.render_status_bar(width, windows(), clock=CLOCK)
    assert 2 <= len(rendered.rows) <= 5
    assert rendered.visible_indices == tuple(range(12))
    assert not rendered.hidden_indices
    assert all(bar.columns(visible_text(row)) <= width for row in rendered.rows)


def test_unsupported_overflow_is_explicit_and_selected_window_stays_visible():
    many = [bar.Window(index, "long window name", "unknown", index == 88) for index in range(100)]
    rendered = bar.render_status_bar(80, many, clock=CLOCK)
    assert len(rendered.rows) <= 5
    assert 0 in rendered.visible_indices and 88 in rendered.visible_indices
    assert len(rendered.visible_indices) + len(rendered.hidden_indices) == 100
    assert "OVERFLOW:" in visible_text(rendered.rows[-1])
    assert f"{len(rendered.hidden_indices)} windows hidden" in visible_text(rendered.rows[-1])
    assert all(bar.columns(visible_text(row)) <= 80 for row in rendered.rows)


def test_tiny_terminal_reports_overflow_instead_of_clipping_away_windows():
    rendered = bar.render_status_bar(16, windows(), clock=CLOCK)
    assert rendered.hidden_indices == tuple(range(12))
    assert not rendered.visible_indices
    assert visible_text(rendered.rows[-1]).startswith("OVERFLOW:")
    assert all(bar.columns(visible_text(row)) <= 16 for row in rendered.rows)


@pytest.mark.parametrize("state,evidence,expected", [
    ("working", True, "WORK"), ("agents", True, "WORK"), ("asking", True, "INPUT"),
    ("idle", True, "IDLE"), ("unread", True, "IDLE"), ("unknown", True, "UNKNOWN"),
    ("error", True, "ERROR"), ("", False, "SHELL"), ("", True, "UNKNOWN"),
    ("unsupported-provider", False, "UNKNOWN"),
])
def test_labels_keep_unknown_distinct_from_plain_shell(state, evidence, expected):
    assert bar.Window(0, "test", state, has_lifecycle=evidence).label == expected


def test_names_are_literal_and_wide_names_fit():
    named = [bar.Window(0, "#(touch /tmp/bad) #{session_name}\n界界界\x1b[31m", "working")]
    rendered = bar.render_status_bar(60, named, session_name="#{evil}\nname", clock=CLOCK)
    assert "##(touch" in rendered.rows[1]
    assert "#{session_name}" not in rendered.rows[1].replace("##{session_name}", "")
    assert "\n" not in "".join(rendered.rows)
    assert "\x1b" not in "".join(rendered.rows)
    assert all(bar.columns(visible_text(row)) <= 60 for row in rendered.rows)


def test_continuum_save_job_survives_header_replacement():
    original = "#(/tmp/continuum_save.sh)#{?client_prefix,yes,no}%H:%M"
    jobs = bar.continuum_jobs(original)
    assert jobs == "#(/tmp/continuum_save.sh)"
    rendered = bar.render_status_bar(232, windows(), clock=CLOCK, background_jobs=jobs)
    assert rendered.rows[0].startswith(jobs)
    assert "%H:%M" not in rendered.rows[0]


def test_clock_uses_fresh_process_and_system_zone(monkeypatch):
    calls = []

    def date(args, **kwargs):
        calls.append((args, kwargs))
        return subprocess.CompletedProcess(args, 0, CLOCK + "\n", "")

    monkeypatch.setattr(bar.subprocess, "run", date)
    monkeypatch.setenv("TZ", "UTC")
    assert bar.live_clock() == CLOCK
    assert bar.live_clock() == CLOCK
    assert len(calls) == 2
    assert calls[0][0] == ["date", bar.TIME_FORMAT]
    assert calls[0][1]["env"]["TZ"] == ":/etc/localtime"


@pytest.fixture
def isolated_bar(tmp_path, monkeypatch):
    if not shutil.which("tmux"):
        pytest.skip("tmux required")
    command = [shutil.which("tmux"), "-S", str(tmp_path / "tmux.sock")]
    home = tmp_path / "home"
    (home / ".config/tmux").mkdir(parents=True)
    bin_directory = tmp_path / "bin"
    bin_directory.mkdir()
    wrapper = bin_directory / "tmux"
    wrapper.write_text("#!/bin/sh\nexec " + shlex.join(command) + ' "$@"\n')
    wrapper.chmod(0o755)
    systemctl = bin_directory / "systemctl"
    systemctl.write_text("#!/bin/sh\nexit 0\n")
    systemctl.chmod(0o755)
    environment = dict(os.environ, HOME=str(home), TERM="xterm-256color",
                       PATH=str(bin_directory) + os.pathsep + os.environ["PATH"])
    environment.pop("TMUX", None)
    environment.pop("CLAUDE_TMUX_THEME", None)
    subprocess.run([*command, "-f", "/dev/null", "new-session", "-d", "-s", "bar",
                    "-n", NAMES[0], "sleep 120"], check=True, env=environment)
    subprocess.run([*command, "set", "-g", "status-interval", "0"], check=True)
    subprocess.run([*command, "set", "-g", "automatic-rename", "off"], check=True)
    for index, name in enumerate(NAMES):
        if index:
            subprocess.run([*command, "new-window", "-d", "-n", name, "sleep 120"], check=True)
        if STATES[index]:
            subprocess.run([*command, "set", "-w", "-t", f"bar:{index}",
                            "@claude_window_state_name", STATES[index]], check=True)
    subprocess.run([*command, "select-window", "-t", "bar:5"], check=True)
    master, slave = pty.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 59, 232, 0, 0))
    client = subprocess.Popen([*command, "attach-session", "-t", "bar"],
                              stdin=slave, stdout=slave, stderr=slave, env=environment)

    def output():
        result = bytearray()
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            if not select.select([master], [], [], 0.05)[0]:
                break
            result.extend(os.read(master, 65536))
        return bytes(result)

    time.sleep(0.05)
    output()
    monkeypatch.setattr(bar, "live_clock", lambda: CLOCK)
    try:
        yield command, environment, output, master
    finally:
        output()
        subprocess.run([*command, "kill-server"], capture_output=True)
        try:
            client.wait(timeout=2)
        except subprocess.TimeoutExpired:
            client.kill()
            client.wait(timeout=2)
        os.close(master)
        os.close(slave)


def test_real_tmux_rows_show_window_zero_and_apply_only_changed_options(isolated_bar, monkeypatch):
    command, environment, output, master = isolated_bar
    assert bar.refresh_status_bar(tmux_cmd=command)
    rendered = output()
    assert b"0: kids-studio" in rendered
    assert b"5: Legion" in rendered
    assert b"11: Pi-input-fixture" in rendered
    assert b"[WORK]" in rendered and b"[INPUT]" in rendered and b"[IDLE]" in rendered
    assert subprocess.check_output([*command, "show", "-v", "status"], text=True).strip() == "3"
    original_run, mutations = bar.subprocess.run, []

    def record(args, **kwargs):
        if "set-option" in args:
            mutations.append(args)
        return original_run(args, **kwargs)

    monkeypatch.setattr(bar.subprocess, "run", record)
    assert bar.refresh_status_bar(tmux_cmd=command)
    assert not mutations
    assert output() == b""
    subprocess.run([*command, "set", "status-format[1]", "broken theme"], check=True)
    output()
    assert bar.refresh_status_bar(tmux_cmd=command)
    assert len(mutations) == 1
    assert "status-format[1]" in mutations[0]
    assert "status-format[0]" not in mutations[0]
    output()


def test_real_mouse_ranges_select_window_zero_and_last_window(isolated_bar):
    command, environment, output, master = isolated_bar
    subprocess.run([*command, "set", "-g", "mouse", "on"], check=True)
    assert bar.refresh_status_bar(tmux_cmd=command)
    output()
    # 59 rows, status height 3: header y57, first window row y58, second y59.
    for x, y, index in ((5, 58, 0), (210, 59, 11)):
        time.sleep(0.35)  # Separate clicks rather than tmux's double-click gesture.
        os.write(master, f"\x1b[<0;{x};{y}M".encode())
        os.write(master, f"\x1b[<0;{x};{y}m".encode())
        deadline = time.monotonic() + 2
        while subprocess.check_output([*command, "display", "-p", "#{window_index}"], text=True).strip() != str(index):
            assert time.monotonic() < deadline, f"click did not select window {index}"
            time.sleep(0.01)
        output()


def test_real_tmux_renders_literal_format_like_window_name(isolated_bar):
    command, environment, output, master = isolated_bar
    subprocess.run([*command, "rename-window", "-t", "bar:0", "##{z}##[bg=red]"], check=True)
    assert bar.refresh_status_bar(tmux_cmd=command)
    rendered = output()
    assert b"#{z}#[bg=red]" in rendered


def test_setup_and_dynamic_config_restore_readable_rows_after_theme_reload(isolated_bar):
    command, environment, output, master = isolated_bar
    subprocess.run(["bash", str(ROOT / "scripts/claude-tmux-setup.sh")], env=environment,
                   check=True, capture_output=True, text=True, timeout=10)
    assert subprocess.check_output([*command, "show", "-v", "status"], text=True).strip() == "3"
    dynamic = Path(environment["HOME"]) / ".config/tmux/plugins-dynamic.conf"
    assert "tmux_status_bar.py" in dynamic.read_text()
    subprocess.run([*command, "set", "status", "on", ";", "set", "status-format[0]", "wrong theme"], check=True)
    subprocess.run([*command, "source-file", str(dynamic)], check=True)
    assert subprocess.check_output([*command, "show", "-v", "status"], text=True).strip() == "3"
    row = subprocess.check_output([*command, "show", "-v", "status-format[1]"], text=True)
    assert "kids-studio" in row and "[WORK]" in row
    output()
