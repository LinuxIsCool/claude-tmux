#!/usr/bin/env python3
"""Normalize a Claude hook into bounded lifecycle metadata, without tool bodies."""
from __future__ import annotations

import json
import re
import sys
import time

MAX_INPUT = 4 * 1024 * 1024
MAX_PAYLOAD = 48 * 1024
FIELDS = {
    "cwd": 2048,
    "source": 256,
    "permission_mode": 256,
    "tool_name": 256,
    "notification_type": 256,
    "agent_type": 256,
}


def bounded(value: str, limit: int) -> str:
    return value.encode("utf-8")[:limit].decode("utf-8", errors="ignore")


def normalize(event: str, pane: str, tmux: str, data: dict) -> dict | None:
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,63}", event):
        return None
    if not re.fullmatch(r"%[0-9]{1,30}", pane):
        return None
    server = tmux.rsplit(",", 2)
    if (len(server) != 3 or not server[0].startswith("/")
            or not server[1].isdigit() or not server[2].isdigit()
            or len(server[0].encode("utf-8")) > 1024):
        return None
    session = data.get("session_id", "")
    # Identity fields must remain exact. Never turn an oversized ID into a prefix.
    if not isinstance(session, str) or len(session.encode("utf-8")) > 256:
        return None
    selected = {"_provider": "claude", "session_id": session}
    for field, limit in FIELDS.items():
        if isinstance(value := data.get(field), str):
            selected[field] = bounded(value, limit)
    tool_input = data.get("tool_input")
    if isinstance(tool_input, dict):
        selected["tool_input"] = {
            field: bounded(value, limit)
            for field, limit in (("file_path", 2048), ("skill", 512))
            if isinstance(value := tool_input.get(field), str)
        }
    return {"type": "event", "event": event, "pane": pane,
            "source": {"socket_path": server[0], "provider": "claude",
                       "session_id": session, "sent_at": time.time()},
            "data": selected}


def main() -> None:
    try:
        if len(sys.argv) != 4:
            return
        raw = sys.stdin.buffer.read(MAX_INPUT + 1)
        if len(raw) > MAX_INPUT:
            return
        data = json.loads(raw)
        if not isinstance(data, dict):
            return
        result = normalize(*sys.argv[1:], data)
        if result is None:
            return
        line = json.dumps(result, ensure_ascii=False, separators=(",", ":")) + "\n"
        if len(line.encode("utf-8")) < MAX_PAYLOAD:
            sys.stdout.write(line)
    except (ValueError, TypeError, UnicodeError, RecursionError):
        # A malformed hook is a no-op. No input contents appear in diagnostics.
        return


if __name__ == "__main__":
    main()
