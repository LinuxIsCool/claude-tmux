#!/usr/bin/env python3
"""Readable, bounded tmux window rows projected by the existing arbiter.

No daemon lives here. ``refresh_status_bar(tmux_cmd=...)`` takes one inventory,
renders each session for its narrowest attached client, and batches changes.
"""
from __future__ import annotations

from dataclasses import dataclass
import getpass
import math
import os
import re
import shlex
import socket
import subprocess
import unicodedata


MAX_WINDOW_ROWS = 4
MAX_NAME_WIDTH = 32
BACKGROUND = "#262626"
FOREGROUND = "#fffafa"
SELECTED_BACKGROUND = "#45475a"
COLORS = {"WORK": "#a6e3a1", "IDLE": "#89b4fa", "INPUT": "#f9e2af",
          "UNKNOWN": "#cdd6f4", "ERROR": "#f38ba8", "SHELL": "#a6adc8"}
LEGEND = "WORK running | INPUT needs reply | IDLE ready | UNKNOWN unverified | SHELL terminal | ERROR failed"
TIME_FORMAT = "+%a %b %d %Y %H:%M %Z"


@dataclass(frozen=True)
class Window:
    index: int
    name: str
    state: str = ""
    selected: bool = False
    has_lifecycle: bool = False

    @property
    def label(self) -> str:
        known = {"working": "WORK", "agents": "WORK", "asking": "INPUT",
                 "idle": "IDLE", "unread": "IDLE", "unknown": "UNKNOWN", "error": "ERROR"}
        return known.get(self.state, "UNKNOWN" if self.state or self.has_lifecycle else "SHELL")


@dataclass(frozen=True)
class StatusBar:
    rows: tuple[str, ...]
    visible_indices: tuple[int, ...]
    hidden_indices: tuple[int, ...] = ()


def clean_text(value: str) -> str:
    """Names are literal text, never another tmux format or terminal command."""
    return " ".join("".join(character if character.isprintable() else " " for character in str(value)).split())


def columns(value: str) -> int:
    # Emoji variation selectors and joiners are deliberately removed from
    # literal names below. East Asian wide characters occupy two columns.
    return sum(0 if unicodedata.combining(character) else
               2 if unicodedata.east_asian_width(character) in {"W", "F"} else 1
               for character in value)


def literal_text(value: str) -> str:
    value = clean_text(value)
    return "".join(character for character in value if character not in "\ufe0e\ufe0f\u200d")


def clip(value: str, width: int) -> str:
    if width <= 0:
        return ""
    if columns(value) <= width:
        return value
    suffix = "..." if width > 3 else ""
    remaining, result = width - len(suffix), ""
    for character in value:
        size = columns(character)
        if size > remaining:
            break
        result += character
        remaining -= size
    return result + suffix


def escape_format(value: str) -> str:
    return value.replace("#", "##")


def live_clock() -> str:
    """Read the operating system's zone in a new process, bypassing tmux TZ."""
    environment = dict(os.environ, TZ=":/etc/localtime", LC_ALL="C")
    try:
        result = subprocess.run(["date", TIME_FORMAT], capture_output=True, text=True,
                                timeout=0.5, env=environment)
        if result.returncode == 0 and result.stdout.strip():
            return literal_text(result.stdout.strip())
    except (OSError, subprocess.TimeoutExpired):
        pass
    return "CLOCK UNAVAILABLE"


def continuum_jobs(value: str) -> str:
    """Keep continuum's background save trigger when replacing status-right."""
    jobs, offset = [], 0
    while (start := value.find("#(", offset)) >= 0:
        depth, end = 1, start + 2
        while end < len(value) and depth:
            if value[end] == "(":
                depth += 1
            elif value[end] == ")":
                depth -= 1
            end += 1
        if depth:
            break
        job = value[start:end]
        if "continuum_save.sh" in job and job not in jobs:
            jobs.append(job)
        offset = end
    return "".join(jobs)


def _header(width: int, session_name: str, session_names: tuple[str, ...], count: int,
            clock: str, host: str, jobs: str) -> str:
    clock = clip(literal_text(clock), width)
    available = max(0, width - columns(clock) - 1)
    info = f"{literal_text(host)} | Session {literal_text(session_name)} | {count} windows"
    other = [literal_text(name) for name in session_names if name != session_name]
    if other:
        info += " | Sessions " + ", ".join(other)
    if columns(info) + len(LEGEND) + 3 <= available:
        info += " | " + LEGEND
    info = clip(info, available)
    padding = " " * max(0, width - columns(info) - columns(clock))
    return (jobs + f"#[align=left,bg={BACKGROUND},fg={FOREGROUND},nobold,norange]"
            + escape_format(info + padding) + f"#[bold]" + escape_format(clock) + "#[nobold]")


def _cell(window: Window, width: int) -> str:
    label = window.label
    marker = ">" if window.selected else " "
    prefix, suffix = f" {marker}{window.index}: ", f" [{label}] "
    budget = max(0, width - len(prefix) - len(suffix))
    name = clip(literal_text(window.name), budget)
    text = prefix + name
    padding = " " * max(0, width - columns(text) - len(suffix))
    background = SELECTED_BACKGROUND if window.selected else BACKGROUND
    emphasis = "bold" if window.selected else "nobold"
    return (f"#[range=window|{window.index},bg={background},fg={FOREGROUND},{emphasis}]"
            + escape_format(text + padding) + f"#[fg={COLORS[label]}]" + suffix + "#[norange]")


def render_status_bar(width: int, windows: list[Window], *, session_name: str = "",
                      session_names: tuple[str, ...] = (), clock: str = "",
                      host: str = "", background_jobs: str = "") -> StatusBar:
    """Pure renderer: one header and up to four window rows, without scrolling."""
    width = max(1, int(width))
    windows = sorted(windows, key=lambda window: window.index)
    header = _header(width, session_name, session_names, len(windows), clock, host, background_jobs)
    if not windows:
        return StatusBar((header, f"#[bg={BACKGROUND},fg={FOREGROUND}]" + clip("No windows", width)), ())
    minimum = max(8 + len(str(window.index)) + len(window.label) + 3 for window in windows)
    ideal = max(8 + len(str(window.index)) + len(window.label)
                + min(MAX_NAME_WIDTH, columns(literal_text(window.name))) for window in windows)
    rows = MAX_WINDOW_ROWS
    for count in range(1, MAX_WINDOW_ROWS + 1):
        if width // math.ceil(len(windows) / count) >= ideal:
            rows = count
            break

    hidden, visible = [], windows
    if width // math.ceil(len(windows) / rows) < minimum:
        per_row = width // minimum
        capacity = per_row * (MAX_WINDOW_ROWS - 1)
        if capacity:
            visible = windows[:capacity]
            selected = next((window for window in windows if window.selected), None)
            if selected is not None and selected not in visible:
                visible = sorted([*visible[:-1], selected], key=lambda window: window.index)
        else:
            visible = []
        shown = {window.index for window in visible}
        hidden = [window.index for window in windows if window.index not in shown]
        rows = MAX_WINDOW_ROWS - 1 if visible else 0

    rendered = [header]
    if visible:
        per_row = math.ceil(len(visible) / rows)
        for offset in range(0, len(visible), per_row):
            group = visible[offset:offset + per_row]
            cell_width, extra = divmod(width, len(group))
            rendered.append("".join(_cell(window, cell_width + (index < extra))
                                    for index, window in enumerate(group)))
    if hidden:
        notice = f"OVERFLOW: {len(hidden)} windows hidden. Widen terminal or use prefix+w."
        rendered.append(f"#[norange,bg={BACKGROUND},fg={COLORS['ERROR']},bold]"
                        + escape_format(clip(notice, width)))
    return StatusBar(tuple(rendered), tuple(window.index for window in visible), tuple(hidden))


def _options(text: str) -> dict[str, str]:
    result = {}
    for line in text.splitlines():
        try:
            parts = shlex.split(line)
            if len(parts) == 2:
                result[parts[0]] = parts[1]
        except ValueError:
            continue
    return result


def refresh_status_bar(tmux_cmd: list[str] | None = None) -> bool:
    """Project the inventory. Return False on failed read/write; never raise."""
    command = ["tmux"] if tmux_cmd is None else list(tmux_cmd)
    windows_marker, clients_marker, options_marker = ("__status_bar_windows__", "__status_bar_clients__", "__status_bar_options__")
    try:
        result = subprocess.run([
            *command, "list-sessions", "-F", "#{session_id}\t#{session_name}\t#{window_width}", ";",
            "display-message", "-p", windows_marker, ";", "list-windows", "-a", "-F",
            "#{session_id}\t#{window_index}\t#{window_name}\t#{window_active}\t#{@claude_window_state_name}\t#{@claude_window_state}", ";",
            "display-message", "-p", clients_marker, ";", "list-clients", "-F", "#{session_id}\t#{client_width}", ";",
            "display-message", "-p", options_marker, ";", "show-options", "-g",
        ], capture_output=True, text=True, timeout=0.8)
        if result.returncode:
            return False
        sessions_text, separator, remainder = result.stdout.partition(windows_marker + "\n")
        windows_text, separator2, remainder = remainder.partition(clients_marker + "\n")
        clients_text, separator3, global_text = remainder.partition(options_marker + "\n")
        if not all((separator, separator2, separator3)):
            return False
        sessions = [line.split("\t", 2) for line in sessions_text.splitlines()]
        if not all(len(parts) == 3 and re.fullmatch(r"\$\d+", parts[0]) for parts in sessions):
            return False
        all_windows = {parts[0]: [] for parts in sessions}
        for line in windows_text.splitlines():
            session_id, index, name, active, state, glyph = line.split("\t")
            all_windows[session_id].append(Window(int(index), name, state, active == "1", bool(glyph)))
        widths: dict[str, list[int]] = {}
        for line in clients_text.splitlines():
            session_id, width = line.split("\t")
            widths.setdefault(session_id, []).append(int(width))
        global_options = _options(global_text)
        local_options = {}
        for session_id, _, _ in sessions:
            options = subprocess.run([*command, "show-options", "-t", session_id],
                                     capture_output=True, text=True, timeout=0.5)
            if options.returncode:
                return False
            local_options[session_id] = _options(options.stdout)
        clock = live_clock()
        host = f"{getpass.getuser()}@{socket.gethostname().split('.')[0]}"
        mutations = list(command)
        for session_id, name, fallback_width in sessions:
            width = min(widths[session_id]) if session_id in widths else int(fallback_width or 80)
            current = {**global_options, **local_options[session_id]}
            jobs = continuum_jobs(current.get("status-right", ""))
            bar = render_status_bar(width, all_windows[session_id], session_name=name,
                                    session_names=tuple(parts[1] for parts in sessions), clock=clock,
                                    host=host, background_jobs=jobs)
            desired = {"status": str(len(bar.rows))}
            desired.update({f"status-format[{index}]": row for index, row in enumerate(bar.rows)})
            for option, value in desired.items():
                if current.get(option) == value:
                    continue
                if len(mutations) > len(command):
                    mutations.append(";")
                mutations.extend(["set-option", "-t", session_id, option, value])
        if len(mutations) == len(command):
            return True
        updated = subprocess.run(mutations, capture_output=True, timeout=0.8)
        return updated.returncode == 0
    except (OSError, ValueError, KeyError, IndexError, subprocess.TimeoutExpired):
        return False


if __name__ == "__main__":
    raise SystemExit(0 if refresh_status_bar() else 1)
