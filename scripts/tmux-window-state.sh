#!/usr/bin/env bash
# Recompute the window lifecycle badge using the arbiter's aggregation rules.
# Voice options remain exclusively owned by voice_arbiter.py.
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
exec python3 - "$SCRIPT_DIR/tmux-arbiter.py" "${1:-}" <<'PY'
import importlib.util
import json
import socket
import subprocess
import sys

spec = importlib.util.spec_from_file_location("tmux_window_arbiter", sys.argv[1])
arbiter = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = arbiter
spec.loader.exec_module(arbiter)
# Focus hooks inherit the precise tmux socket, including absolute -S paths.
arbiter.TMUX_CMD = ["tmux"]
cmd = ["tmux", "display-message", "-p"]
if sys.argv[2]:
    cmd += ["-t", sys.argv[2]]
result = subprocess.run(cmd + ["#{pane_id}\t#{socket_path}"], capture_output=True, text=True)
parts = result.stdout.strip().split("\t")
if result.returncode or len(parts) != 2:
    sys.exit(0)
pane_id, socket_path = parts

# Keep live lifecycle projections serialized in the daemon. Older or stopped
# daemons fall back to the same aggregation code below.
try:
    with socket.socket(socket.AF_UNIX) as sock:
        sock.settimeout(1.0)
        sock.connect(str(arbiter.ARBITER_SOCKET))
        sock.sendall((json.dumps({"type": "window_refresh", "pane": pane_id,
                                 "mark_read": True, "socket_path": socket_path}) + "\n").encode())
        response = bytearray()
        while b"\n" not in response:
            chunk = sock.recv(4096)
            if not chunk:
                break
            response.extend(chunk)
        if response and json.loads(response).get("type") == "window_refresh_ack":
            sys.exit(0)
except (OSError, ValueError):
    pass

snapshot = arbiter.TmuxWriter._snapshot(pane_id)
if snapshot is None:
    sys.exit(0)
current, siblings = snapshot
theme = arbiter.ThemeLoader()
_, unread_color, _ = theme.resolve_state("unread")
state_name = current.get(("p", "@claude_state_name"))
unread = state_name == "unread" or (
    not state_name and current.get(("p", "@claude_color")) == unread_color
)
desired = []
if unread:
    # Preserve unread -> read when the daemon is unavailable. Update both
    # semantic state and glyph before recomputing the lifecycle aggregate.
    name, color, glyph = theme.resolve_state("idle")
    changes = {"state_name": name, "color": color, "state": glyph}
    desired = [("p", "@claude_" + field, value) for field, value in changes.items()]
    siblings.setdefault(pane_id, {}).update(changes)
arbiter.TmuxWriter._apply(
    pane_id, current, desired + arbiter.TmuxWriter._window_options(siblings),
)
PY
