"""Validate lifecycle messages against independently observed native owners.

Pane IDs are local to a tmux server. A saved badge or an adapter's historical
pane mapping cannot establish which running execution owns an event.
"""
from __future__ import annotations

import math
from pathlib import Path
import re


MAX_EVENT_BYTES = 64 * 1024


def rejection_reason(message: dict, binding: dict | None, socket_path: str,
                     now: float, max_age: float) -> str | None:
    """Return a stable diagnostic code, or None for a bound lifecycle event.

    Legacy adapters without a source envelope still need an exact native
    provider/session match. The receiver separately verifies the live process
    and pane shell before calling this policy.
    """
    if (not isinstance(message, dict) or message.get("type") != "event"
            or not isinstance(message.get("event"), str)
            or not message["event"] or len(message["event"]) > 80
            or not isinstance(message.get("pane"), str)
            or not re.fullmatch(r"%\d+", message["pane"])
            or not isinstance(message.get("data"), dict)):
        return "malformed-event"
    data = message["data"]
    if not binding or not binding.get("session_id"):
        return "unbound-pane"
    if data.get("_provider", "claude") != binding.get("provider"):
        return "provider-mismatch"
    session_id = data.get("session_id")
    if not isinstance(session_id, str) or session_id != binding["session_id"]:
        return "session-mismatch"
    if (data.get("turn_id") and binding.get("turn_id")
            and data["turn_id"] != binding["turn_id"]):
        return "turn-mismatch"
    if "source" not in message:
        return None
    source = message["source"]
    if not isinstance(source, dict):
        return "malformed-source"
    if source.get("provider") != binding["provider"]:
        return "provider-mismatch"
    if source.get("session_id") != session_id:
        return "session-mismatch"
    if (not socket_path or not isinstance(source.get("socket_path"), str)
            or not Path(source["socket_path"]).is_absolute()
            or Path(source["socket_path"]) != Path(socket_path)):
        return "server-mismatch"
    sent_at = source.get("sent_at")
    if (isinstance(sent_at, bool) or not isinstance(sent_at, (int, float))
            or not math.isfinite(sent_at)):
        return "malformed-timestamp"
    if now - sent_at > max_age or sent_at - now > 5:
        return "stale-event"
    native_updated = binding.get("updated_at", binding.get("observed_at"))
    if isinstance(native_updated, (int, float)) and sent_at < native_updated:
        return "superseded-event"
    return None


def process_is_current(binding: dict, shell_pid: int, proc_root: Path = Path("/proc")) -> bool:
    """Recheck start time and ancestry, including PID reuse between polls."""
    try:
        pid = int(binding["process_id"])
        expected_start = str(binding["process_start"])
        seen = set()
        while pid > 1 and pid not in seen and len(seen) < 128:
            seen.add(pid)
            raw = (proc_root / str(pid) / "stat").read_text()
            fields = raw[raw.rindex(")") + 2:].split()
            if fields[0] == "Z":
                return False
            if len(seen) == 1 and fields[19] != expected_start:
                return False
            if pid == shell_pid:
                return True
            pid = int(fields[1])
    except (OSError, KeyError, ValueError, IndexError, TypeError):
        pass
    return False
