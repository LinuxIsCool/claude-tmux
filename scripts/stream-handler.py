#!/usr/bin/env python3
"""stream-handler.py — Per-pane output segmenter for tmux-stream.

Reads stdin from tmux pipe-pane, segments output into command+output blocks
using Fish shell OSC markers, and appends structured JSONL.

Usage (called by tmux pipe-pane, not directly):
    tmux pipe-pane -O -t %42 "python3 -u /path/to/stream-handler.py %42"

The -u flag is CRITICAL — without it, Python buffers stdout/stdin in 64KB
blocks, and segments arrive minutes late.

Segmentation:
    1. Primary: Fish OSC markers (cmd-start / cmd-end) give exact boundaries
    2. Fallback: Silence gap (2s of no output) for non-Fish panes
"""
from __future__ import annotations

import fcntl
import json
import os
import re
import select
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

STREAM_DIR = Path("~/.claude/local/tmux-stream").expanduser()
PANE_STATES_FILE = Path("~/.claude/local/tmux/pane-states.json").expanduser()

# OSC marker pattern: \033]tmux-stream;{type};{epoch_ms};{payload}\007 or \033\\
# Accepts both BEL (\x07) and ST (\x1b\\) as OSC terminators.
OSC_PATTERN = re.compile(
    r"\x1b\]tmux-stream;(cmd|end);(\d+);?(.*?)(?:\x07|\x1b\\)"
)

# ANSI escape sequence stripper (colors, cursor movement, bracketed paste, etc.)
ANSI_STRIP = re.compile(
    r"\x1b\[[0-9;?]*[a-zA-Z]"      # CSI sequences (colors, cursor, mode set/reset)
    r"|\x1b\][^\x07]*(?:\x07|\x1b\\)"  # OSC sequences (both terminators)
    r"|\x1b[()][AB012]"             # Character set selection
    r"|\x1b[>=<]"                   # Application/normal keypad mode
    r"|\x1b\[>[0-9;]*[a-zA-Z]"     # Private mode sequences (like >4;2m)
    r"|\r"                           # Carriage returns (common in terminal output)
)

SILENCE_GAP_SECS = 2.0       # Silence gap for fallback segmentation
MAX_SEGMENT_BYTES = 102_400   # 100KB max output per segment
MAX_SEGMENT_LINES = 5_000     # 5000 lines max per segment

# TUI detection: cursor-addressed output patterns that indicate a full-screen app.
# These CSI sequences are used by vim, htop, less, man, etc.
#   ?1049h/l = alternate screen buffer enter/exit
#   ?25h/l   = cursor show/hide
#   H        = cursor home / absolute position (e.g., \x1b[5;10H)
TUI_PATTERN = re.compile(
    r"\x1b\[\?1049[hl]"            # Alternate screen buffer
    r"|\x1b\[\?25[hl]"             # Cursor visibility toggle
    r"|\x1b\[\d+;\d+H"             # Absolute cursor positioning
)

# Continuous output timer: force segment boundary after this many seconds
# of uninterrupted output without an OSC marker.
CONTINUOUS_OUTPUT_TIMEOUT = 30.0

# Backpressure: max bytes per second sustained before pausing capture
BACKPRESSURE_BYTES_PER_SEC = 1_048_576  # 1 MB/s
BACKPRESSURE_WINDOW_SECS = 10.0

# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------

@dataclass
class Segment:
    """One command+output block."""
    pane: str
    session: str = ""
    window: str = ""
    seq: int = 0
    ts: str = ""
    command: str | None = None
    exit_code: int | None = None
    duration_ms: int | None = None
    output_lines: list[str] = field(default_factory=list)
    output_bytes: int = 0
    start_epoch_ms: int = 0
    claude_session: str | None = None
    persona: str | None = None
    tui: bool = False                  # True if TUI output detected in this segment
    continuation: bool = False         # True if this is a split/overflow segment
    logical_id: str = ""               # Human-readable: "session:window.pane_index"


# ---------------------------------------------------------------------------
# State management — per-pane state files to avoid cross-process races
# ---------------------------------------------------------------------------

class StreamState:
    """Per-pane state: sequence counter, persisted in a per-pane file.

    Each handler writes ONLY its own state file, eliminating the race
    condition where multiple handlers clobber a shared state.json.
    """

    def __init__(self, pane_id: str):
        self.pane_id = pane_id
        # Per-pane state file: state-%42.json
        safe_id = pane_id.replace("%", "pane-")
        self._state_file = STREAM_DIR / f"state-{safe_id}.json"
        self._state: dict = {}
        self._load()

    def _load(self):
        try:
            if self._state_file.exists():
                self._state = json.loads(self._state_file.read_text())
        except Exception:
            self._state = {}

    def _save(self):
        try:
            STREAM_DIR.mkdir(parents=True, exist_ok=True)
            tmp = self._state_file.with_suffix(".tmp")
            tmp.write_text(json.dumps(self._state))
            tmp.rename(self._state_file)
        except Exception:
            pass

    def next_seq(self) -> int:
        """Get and increment the sequence counter for this pane."""
        seq = self._state.get("seq", 0)
        self._state["seq"] = seq + 1
        self._save()
        return seq

    def record_pipe_attach(self):
        """Record that a pipe was attached to this pane."""
        self._state["attached"] = time.time()
        self._save()


# ---------------------------------------------------------------------------
# Claude session correlation
# ---------------------------------------------------------------------------

def get_claude_context(pane_id: str) -> tuple[str | None, str | None]:
    """Read Claude session ID and persona from the arbiter's pane-states.json.

    Returns (claude_session_id, persona_emoji) or (None, None).
    This is a file read, not IPC — pane-states.json is atomically written.
    """
    try:
        if not PANE_STATES_FILE.exists():
            return None, None
        data = json.loads(PANE_STATES_FILE.read_text())
        pane_state = data.get(pane_id, {})
        if not pane_state:
            return None, None
        persona = pane_state.get("persona") or None
        return None, persona
    except Exception:
        return None, None


def get_pane_info(pane_id: str) -> tuple[str, str, str]:
    """Get session name, window identifier, and pane index for a pane.

    Returns (session_name, window_identifier, pane_index)
    e.g. ("legion-matt", "3:agents", "0").
    """
    try:
        r = subprocess.run(
            ["tmux", "display-message", "-p", "-t", pane_id,
             "#{session_name}\t#{window_index}:#{window_name}\t#{pane_index}"],
            capture_output=True, timeout=0.5,
        )
        if r.returncode == 0:
            parts = r.stdout.decode().strip().split("\t")
            if len(parts) == 3:
                return parts[0], parts[1], parts[2]
    except Exception:
        pass
    return "", "", ""


# ---------------------------------------------------------------------------
# ANSI stripping
# ---------------------------------------------------------------------------

def strip_ansi(text: str) -> str:
    """Remove ANSI escape sequences from text."""
    return ANSI_STRIP.sub("", text)


# ---------------------------------------------------------------------------
# JSONL writer — atomic writes with file locking
# ---------------------------------------------------------------------------

def jsonl_path() -> Path:
    """Get today's JSONL file path."""
    return STREAM_DIR / f"{datetime.now(timezone.utc).strftime('%Y-%m-%d')}.jsonl"


def flush_segment(seg: Segment) -> None:
    """Write a segment as one JSONL line. Skip empty segments.

    Uses fcntl.flock() to prevent interleaved writes from concurrent handlers.
    Writes the entire JSON line in a single os.write() call for atomicity.
    """
    output = "\n".join(seg.output_lines)
    if not output.strip() and seg.command is None:
        return  # Nothing to write

    record: dict = {
        "pane": seg.pane,
        "session": seg.session,
        "window": seg.window,
        "seq": seg.seq,
        "ts": seg.ts,
    }
    if seg.logical_id:
        record["logical_id"] = seg.logical_id
    if seg.command is not None:
        record["command"] = seg.command
    if seg.exit_code is not None:
        record["exit_code"] = seg.exit_code
    if seg.duration_ms is not None:
        record["duration_ms"] = seg.duration_ms
    record["output"] = output
    if seg.claude_session:
        record["claude_session"] = seg.claude_session
    if seg.persona:
        record["persona"] = seg.persona
    if seg.tui:
        record["tui"] = True
    if seg.continuation:
        record["continuation"] = True

    path = jsonl_path()
    line_bytes = (json.dumps(record, ensure_ascii=False) + "\n").encode("utf-8")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            os.write(fd, line_bytes)
        finally:
            # flock released automatically when fd is closed
            os.close(fd)
    except Exception as e:
        log(f"flush error: {e}")


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

LOG_FILE = STREAM_DIR / "handler.log"
MAX_LOG_BYTES = 500_000  # 500KB

def log(msg: str) -> None:
    """Append a log line. Lightweight — no external dependencies."""
    try:
        STREAM_DIR.mkdir(parents=True, exist_ok=True)
        if LOG_FILE.exists() and LOG_FILE.stat().st_size > MAX_LOG_BYTES:
            LOG_FILE.rename(LOG_FILE.with_suffix(".log.1"))
        with open(LOG_FILE, "a") as f:
            ts = time.strftime("%Y-%m-%dT%H:%M:%S")
            f.write(f"{ts} [stream:{os.getpid()}] {msg}\n")
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def main():
    if len(sys.argv) < 2:
        print("Usage: stream-handler.py <pane_id>", file=sys.stderr)
        sys.exit(1)

    pane_id = sys.argv[1]
    state = StreamState(pane_id)
    state.record_pipe_attach()

    # Get pane info once at startup (session/window don't change for a pane)
    session_name, window_id, pane_index = get_pane_info(pane_id)
    logical_id = f"{session_name}:{window_id}.{pane_index}" if session_name else ""

    log(f"started for pane {pane_id} (session={session_name}, window={window_id}, logical_id={logical_id})")

    # Current segment being accumulated
    current: Segment | None = None
    segment_start_time: float = 0.0  # When current segment started accumulating

    # Backpressure tracking: sliding window of bytes received
    bp_window_start: float = time.time()
    bp_window_bytes: int = 0
    bp_throttled: bool = False

    def new_segment(command: str | None = None, epoch_ms: int = 0) -> Segment:
        claude_session, persona = get_claude_context(pane_id)
        ts = datetime.now(timezone.utc).isoformat()
        if epoch_ms:
            ts = datetime.fromtimestamp(epoch_ms / 1000, tz=timezone.utc).isoformat()
        return Segment(
            pane=pane_id,
            session=session_name,
            window=window_id,
            logical_id=logical_id,
            seq=state.next_seq(),
            ts=ts,
            command=command,
            start_epoch_ms=epoch_ms,
            claude_session=claude_session,
            persona=persona,
        )

    def finish_current():
        nonlocal current
        if current is not None:
            flush_segment(current)
            current = None

    # Graceful shutdown: SIGTERM flushes and exits.
    # SIGPIPE uses default disposition (process dies cleanly when pipe breaks).
    def handle_sigterm(sig, _frame):
        del _frame  # signal handler signature; frame unused
        log(f"signal {sig} — flushing and exiting")
        finish_current()
        sys.exit(0)

    signal.signal(signal.SIGTERM, handle_sigterm)
    signal.signal(signal.SIGPIPE, signal.SIG_DFL)

    try:
        while True:
            # Wait for input with timeout for silence-gap detection
            ready, _, _ = select.select([sys.stdin], [], [], SILENCE_GAP_SECS)

            if not ready:
                # Timeout — silence gap detected
                if current is not None and current.output_lines:
                    finish_current()
                continue

            line = sys.stdin.readline()
            if not line:
                # EOF — pipe closed (pane died or arbiter detached)
                log("EOF — pipe closed")
                finish_current()
                break

            now = time.time()
            line_len = len(line)

            # Backpressure tracking
            bp_window_bytes += line_len
            window_elapsed = now - bp_window_start

            if window_elapsed >= BACKPRESSURE_WINDOW_SECS:
                rate = bp_window_bytes / window_elapsed
                if rate > BACKPRESSURE_BYTES_PER_SEC:
                    if not bp_throttled:
                        bp_throttled = True
                        log(f"backpressure: {rate/1024:.0f} KB/s exceeds limit, pausing capture")
                        finish_current()
                        # Write a gap marker
                        gap_record = {
                            "pane": pane_id, "session": session_name, "window": window_id,
                            "seq": state.next_seq(),
                            "ts": datetime.now(timezone.utc).isoformat(),
                            "type": "gap", "reason": "backpressure",
                            "rate_kbps": round(rate / 1024),
                        }
                        gap_bytes = (json.dumps(gap_record) + "\n").encode("utf-8")
                        path = jsonl_path()
                        try:
                            fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
                            try:
                                fcntl.flock(fd, fcntl.LOCK_EX)
                                os.write(fd, gap_bytes)
                            finally:
                                os.close(fd)
                        except Exception:
                            pass
                else:
                    if bp_throttled:
                        bp_throttled = False
                        log("backpressure: rate dropped, resuming capture")
                # Reset window
                bp_window_start = now
                bp_window_bytes = 0

            if bp_throttled:
                continue  # Skip this line entirely — don't accumulate

            # Check for OSC markers
            marker_match = OSC_PATTERN.search(line)
            if marker_match:
                marker_type = marker_match.group(1)
                epoch_ms = int(marker_match.group(2)) if marker_match.group(2) else 0
                payload = marker_match.group(3)

                if marker_type == "cmd":
                    # New command starting — flush previous segment, start new one
                    finish_current()
                    current = new_segment(command=payload, epoch_ms=epoch_ms)
                    segment_start_time = time.time()

                elif marker_type == "end":
                    # Command finished — set exit code and duration, flush
                    if current is not None:
                        try:
                            current.exit_code = int(payload)
                        except (ValueError, TypeError):
                            current.exit_code = None
                        if current.start_epoch_ms and epoch_ms:
                            current.duration_ms = epoch_ms - current.start_epoch_ms
                        finish_current()
                        segment_start_time = 0.0

                # Strip the marker from the line before processing further
                line = OSC_PATTERN.sub("", line)
                if not line.strip():
                    continue

            # Detect TUI output before stripping ANSI (cache the result)
            line_is_tui = bool(TUI_PATTERN.search(line))

            # Accumulate output
            clean = strip_ansi(line.rstrip("\n\r"))

            if current is None:
                # No active segment — start a silence-gap segment
                current = new_segment()
                segment_start_time = time.time()

            if line_is_tui and not current.tui:
                current.tui = True

            # Check segment size limits
            if (current.output_bytes + len(clean) > MAX_SEGMENT_BYTES
                    or len(current.output_lines) >= MAX_SEGMENT_LINES):
                # Split: flush current, start continuation
                finish_current()
                current = new_segment()
                current.command = None  # continuation has no command
                current.continuation = True
                segment_start_time = time.time()

            current.output_lines.append(clean)
            current.output_bytes += len(clean)

            # Force segment boundary after CONTINUOUS_OUTPUT_TIMEOUT seconds
            # of uninterrupted output without an OSC marker.
            if (current is not None
                    and current.command is None  # Only for non-command segments
                    and segment_start_time > 0
                    and (time.time() - segment_start_time) > CONTINUOUS_OUTPUT_TIMEOUT):
                finish_current()
                current = new_segment()
                current.continuation = True
                segment_start_time = time.time()

    except KeyboardInterrupt:
        finish_current()
    except Exception as e:
        log(f"fatal error: {e}")
        finish_current()
        sys.exit(1)

    log("handler exiting")


if __name__ == "__main__":
    main()
