#!/usr/bin/env python3
"""tmux-stream-cli.py — Search, browse, and inspect tmux-stream history.

Usage:
    tmux-stream log [--pane %42] [--since 2h] [--session NAME] [--errors]
    tmux-stream search <pattern> [--pane %42] [--date YYYY-MM-DD]
    tmux-stream stats [--date YYYY-MM-DD]
    tmux-stream status
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

STREAM_DIR = Path("~/.claude/local/tmux-stream").expanduser()
ARBITER_SOCKET = Path("~/.claude/local/tmux/arbiter.sock").expanduser()


def parse_since(since_str: str) -> datetime:
    """Parse relative time like '2h', '30m', '1d' into a UTC datetime."""
    m = re.match(r"(\d+)([smhd])", since_str)
    if not m:
        raise ValueError(f"Invalid --since format: {since_str!r}. Use: 30m, 2h, 1d")
    val, unit = int(m.group(1)), m.group(2)
    delta = {"s": timedelta(seconds=val), "m": timedelta(minutes=val),
             "h": timedelta(hours=val), "d": timedelta(days=val)}[unit]
    return datetime.now(timezone.utc) - delta


def iter_segments(date: str | None = None, since: str | None = None):
    """Yield parsed segment dicts from JSONL files.

    If date is given, read only that date's file.
    If since is given, read today's file and filter by timestamp.
    If neither, read today's file.
    """
    if date:
        files = [STREAM_DIR / f"{date}.jsonl", STREAM_DIR / f"{date}.jsonl.zst"]
    else:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        files = [STREAM_DIR / f"{today}.jsonl"]

    cutoff = parse_since(since) if since else None

    for path in files:
        if not path.exists():
            continue
        if path.suffix == ".zst":
            # Decompress on the fly
            try:
                r = subprocess.run(["zstdcat", str(path)], capture_output=True, timeout=30)
                lines = r.stdout.decode("utf-8", errors="replace").splitlines()
            except Exception:
                continue
        else:
            try:
                lines = path.read_text(errors="replace").splitlines()
            except Exception:
                continue

        for line in lines:
            if not line.strip():
                continue
            try:
                seg = json.loads(line)
            except json.JSONDecodeError:
                continue
            if cutoff and "ts" in seg:
                try:
                    seg_time = datetime.fromisoformat(seg["ts"])
                    if seg_time < cutoff:
                        continue
                except (ValueError, TypeError):
                    pass
            yield seg


def cmd_log(args):
    """Show recent segments."""
    count = 0
    for seg in iter_segments(date=args.date, since=args.since):
        if args.pane and seg.get("pane") != args.pane:
            continue
        if args.session and seg.get("session") != args.session:
            continue
        if args.errors and seg.get("exit_code", 0) == 0:
            continue
        if seg.get("type") == "gap":
            print(f"\033[33m[GAP] {seg.get('ts', '?')} {seg.get('pane', '?')} — {seg.get('reason', '?')}\033[0m")
            count += 1
            continue

        ts = seg.get("ts", "?")[:19]
        pane = seg.get("pane", "?")
        cmd = seg.get("command", "")
        exit_code = seg.get("exit_code")
        persona = seg.get("persona", "")
        tui = " [TUI]" if seg.get("tui") else ""
        output = seg.get("output", "")

        # Header line
        exit_str = f" exit={exit_code}" if exit_code is not None else ""
        cmd_str = f" $ {cmd}" if cmd else ""
        print(f"\033[36m{ts}\033[0m {persona}{pane}{tui}{cmd_str}\033[2m{exit_str}\033[0m")

        # Output (truncated for display)
        lines = output.split("\n")
        for line in lines[:10]:
            print(f"  {line}")
        if len(lines) > 10:
            print(f"  \033[2m... ({len(lines) - 10} more lines)\033[0m")
        print()
        count += 1

    if count == 0:
        print("No segments found.")


def cmd_search(args):
    """Search segment output for a pattern."""
    try:
        pattern = re.compile(args.pattern, re.IGNORECASE)
    except re.error as e:
        print(f"Invalid regex: {e}", file=sys.stderr)
        sys.exit(1)

    count = 0
    for seg in iter_segments(date=args.date, since=args.since):
        if args.pane and seg.get("pane") != args.pane:
            continue
        if seg.get("type") == "gap":
            continue
        output = seg.get("output", "")
        cmd = seg.get("command", "")
        search_text = f"{cmd}\n{output}" if cmd else output

        if pattern.search(search_text):
            ts = seg.get("ts", "?")[:19]
            pane = seg.get("pane", "?")
            persona = seg.get("persona", "")
            cmd_str = f" $ {cmd}" if cmd else ""

            print(f"\033[36m{ts}\033[0m {persona}{pane}{cmd_str}")

            # Show matching lines with highlight
            for line in output.split("\n"):
                if pattern.search(line):
                    highlighted = pattern.sub(lambda m: f"\033[1;31m{m.group()}\033[0m", line)
                    print(f"  {highlighted}")

            print()
            count += 1

    print(f"\033[2m{count} matching segment(s)\033[0m")


def cmd_stats(args):
    """Show stream statistics."""
    date_str = args.date or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    segments = list(iter_segments(date=date_str))

    if not segments:
        print(f"No data for {date_str}")
        return

    # Compute stats
    total = len(segments)
    panes: dict[str, int] = {}
    commands = 0
    errors = 0
    tui_count = 0
    gaps = 0
    total_bytes = 0

    for seg in segments:
        pane = seg.get("pane", "?")
        panes[pane] = panes.get(pane, 0) + 1
        if seg.get("command"):
            commands += 1
        if seg.get("exit_code", 0) != 0 and seg.get("exit_code") is not None:
            errors += 1
        if seg.get("tui"):
            tui_count += 1
        if seg.get("type") == "gap":
            gaps += 1
        total_bytes += len(seg.get("output", ""))

    # File size
    jsonl_file = STREAM_DIR / f"{date_str}.jsonl"
    zst_file = STREAM_DIR / f"{date_str}.jsonl.zst"
    if jsonl_file.exists():
        file_size = jsonl_file.stat().st_size
        file_label = f"{file_size / 1024 / 1024:.1f} MB (uncompressed)"
    elif zst_file.exists():
        file_size = zst_file.stat().st_size
        file_label = f"{file_size / 1024 / 1024:.1f} MB (zstd compressed)"
    else:
        file_label = "?"

    print(f"tmux-stream stats for {date_str}")
    print(f"{'─' * 40}")
    print(f"  Segments     : {total}")
    print(f"  Commands     : {commands} (Fish-marked)")
    print(f"  Errors       : {errors} (non-zero exit)")
    print(f"  TUI segments : {tui_count}")
    print(f"  Gap markers  : {gaps}")
    print(f"  Output bytes : {total_bytes / 1024 / 1024:.1f} MB (text)")
    print(f"  File size    : {file_label}")
    print(f"  Panes        : {len(panes)}")
    print()
    print("  Segments by pane:")
    for pane, count in sorted(panes.items(), key=lambda x: -x[1]):
        print(f"    {pane:8s} : {count}")


def cmd_status(_args):
    """Show live system status."""
    del _args  # subcommand callback signature; no flags consumed
    # Check arbiter
    try:
        import socket as _socket
        with _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM) as s:
            s.settimeout(2)
            s.connect(str(ARBITER_SOCKET))
            s.sendall(b'{"type":"status"}\n')
            buf = b""
            while chunk := s.recv(4096):
                buf += chunk
                if b"\n" in buf:
                    break
        resp = json.loads(buf.split(b"\n")[0])
        pipes = resp.get("stream_pipes", "?")
        panes = resp.get("pane_count", "?")
        print(f"  Arbiter      : running ({panes} panes, {pipes} stream pipes)")
    except Exception:
        print("  Arbiter      : not running")

    # Check handlers
    try:
        r = subprocess.run(["pgrep", "-f", "stream-handler.py"], capture_output=True, timeout=2)
        handler_count = len(r.stdout.decode().strip().splitlines()) if r.returncode == 0 else 0
        print(f"  Handlers     : {handler_count} running")
    except Exception:
        print("  Handlers     : unknown")

    # Storage
    total_size = 0
    file_count = 0
    for f in STREAM_DIR.glob("*.jsonl*"):
        total_size += f.stat().st_size
        file_count += 1
    print(f"  Storage      : {total_size / 1024 / 1024:.1f} MB across {file_count} files")
    print(f"  Stream dir   : {STREAM_DIR}")

    # Today's file
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    today_file = STREAM_DIR / f"{today}.jsonl"
    if today_file.exists():
        lines = sum(1 for _ in open(today_file))
        size = today_file.stat().st_size
        print(f"  Today        : {lines} segments, {size / 1024:.0f} KB")
    else:
        print("  Today        : no data yet")

    # Timer
    try:
        r = subprocess.run(
            ["systemctl", "--user", "is-active", "tmux-stream-rotate.timer"],
            capture_output=True, timeout=2,
        )
        timer_state = r.stdout.decode().strip()
        print(f"  Rotation     : {timer_state}")
    except Exception:
        print("  Rotation     : unknown")


def main():
    parser = argparse.ArgumentParser(
        prog="tmux-stream",
        description="Search, browse, and inspect tmux-stream terminal history.",
    )
    sub = parser.add_subparsers(dest="command")

    # log
    p_log = sub.add_parser("log", help="Show recent segments")
    p_log.add_argument("--pane", help="Filter by pane ID (e.g., %%42)")
    p_log.add_argument("--session", help="Filter by tmux session name")
    p_log.add_argument("--since", help="Show segments since (e.g., 2h, 30m, 1d)")
    p_log.add_argument("--date", help="Show segments from date (YYYY-MM-DD)")
    p_log.add_argument("--errors", action="store_true", help="Only show non-zero exit codes")

    # search
    p_search = sub.add_parser("search", help="Search segment output")
    p_search.add_argument("pattern", help="Regex pattern to search for")
    p_search.add_argument("--pane", help="Filter by pane ID")
    p_search.add_argument("--since", help="Search since (e.g., 2h, 30m, 1d)")
    p_search.add_argument("--date", help="Search in date (YYYY-MM-DD)")

    # stats
    p_stats = sub.add_parser("stats", help="Show stream statistics")
    p_stats.add_argument("--date", help="Stats for date (YYYY-MM-DD), default today")

    # status
    sub.add_parser("status", help="Show live system status")

    args = parser.parse_args()

    if args.command == "log":
        cmd_log(args)
    elif args.command == "search":
        cmd_search(args)
    elif args.command == "stats":
        cmd_stats(args)
    elif args.command == "status":
        cmd_status(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
