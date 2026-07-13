#!/usr/bin/env python3
"""tmux-tree.py — Hierarchical snapshot of all tmux sessions, windows, panes.

Part of task-181 Phase 2.5a (from the tmux-topology research, 2026-04-30
tmux-pane-observability/SYNTHESIS.md). Equivalent to `prefix + T`
(choose-tree) but printable, greppable, JSON-able, and joined with
claude-tmux's pane-states.json for persona/Claude-session context.

Usage:
    tmux-tree.py                    # rendered tree (default)
    tmux-tree.py --json             # structured JSON for programmatic consumers
    tmux-tree.py --session NAME     # filter to one session
    tmux-tree.py --no-state         # skip pane-states.json join (faster)
    tmux-tree.py --no-header        # omit header line in rendered output

Output (rendered):
    tmux-tree @ 2026-04-30T02:50Z — 5 sessions, 25 panes
    ● alice  (2 panes)
      ├─ *1:claude-alice
      │  └─ ◉ %33  claude  340x76  persona=🪷 state=working
      └─  2:claude-alice-notes
         └─ ◉ %35  claude  340x76

Glyphs:
    ● = attached session         ○ = detached
    * = active window            (space) = inactive
    ◉ = active pane              · = inactive

Edge cases:
    - Tab character in window/session names breaks the field parser
      (silent skip via len != 11). tmux does not escape tabs in -F output.
    - Non-UTF8 bytes in pane_current_command are replaced (errors="replace").
    - tmux server not running → exits 1 with stderr message.
"""
from __future__ import annotations

import argparse
import collections
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

PANE_STATES_FILE = Path("~/.claude/local/tmux/pane-states.json").expanduser()

PANE_FMT = "\t".join([
    "#{session_name}",
    "#{window_index}",
    "#{window_name}",
    "#{pane_index}",
    "#{pane_id}",
    "#{pane_pid}",
    "#{pane_current_command}",
    "#{pane_active}",
    "#{window_active}",
    "#{session_attached}",
    "#{pane_width}x#{pane_height}",
])


def load_pane_states() -> dict:
    """Read claude-tmux's per-pane state file. Empty dict on any failure."""
    try:
        if PANE_STATES_FILE.exists():
            return json.loads(PANE_STATES_FILE.read_text())
    except Exception:
        pass
    return {}


def collect_panes(
    session_filter: str | None = None, skip_state: bool = False
) -> list[dict]:
    """Return list of pane records joined with pane-states.json.

    skip_state=True skips the pane-states.json read for speed (no persona,
    claude_session, or state fields will appear on returned records).
    """
    try:
        out = subprocess.check_output(
            ["tmux", "list-panes", "-aF", PANE_FMT],
            timeout=2,
        ).decode("utf-8", errors="replace")
    except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired) as e:
        print(f"tmux-tree: cannot query tmux: {e}", file=sys.stderr)
        sys.exit(1)

    states = {} if skip_state else load_pane_states()
    panes = []
    for line in out.strip().split("\n"):
        if not line:
            continue
        parts = line.split("\t")
        if len(parts) != 11:
            # Likely tab-in-name corruption. Skip silently — caller can detect
            # via expected vs actual count if needed.
            continue
        s, wi, wn, pi, pane_id, ppid, cmd, pa, wa, sa, sz = parts
        if session_filter and s != session_filter:
            continue
        rec = {
            "session": s,
            "window_index": int(wi),
            "window_name": wn,
            "pane_index": int(pi),
            "pane_id": pane_id,
            "pane_pid": int(ppid) if ppid.isdigit() else None,
            "command": cmd,
            "size": sz,
            "pane_active": pa == "1",
            "window_active": wa == "1",
            "session_attached": sa == "1",
        }
        ps = states.get(pane_id, {}) if isinstance(states, dict) else {}
        if ps:
            persona = ps.get("persona")
            if isinstance(persona, str):
                persona = persona.strip() or None
            rec["persona"] = persona
            rec["claude_session"] = ps.get("claude_session_id")
            rec["state"] = ps.get("state")
        panes.append(rec)
    return panes


def render_tree(panes: list[dict], header: bool = True) -> str:
    """Render the rendered-tree view."""
    by_session: dict = collections.defaultdict(lambda: collections.defaultdict(list))
    sess_attached: dict = {}
    win_active: dict = {}
    for p in panes:
        s = p["session"]
        wkey = (p["window_index"], p["window_name"])
        by_session[s][wkey].append(p)
        sess_attached[s] = p["session_attached"]
        win_active[(s, p["window_index"])] = p["window_active"]

    lines: list[str] = []
    if header:
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%MZ")
        lines.append(
            f"tmux-tree @ {ts} — {len(by_session)} sessions, {len(panes)} panes"
        )
    for s in sorted(by_session):
        smark = "●" if sess_attached[s] else "○"
        n_panes = sum(len(v) for v in by_session[s].values())
        word = "pane" if n_panes == 1 else "panes"
        lines.append(f"{smark} {s}  ({n_panes} {word})")
        wins = sorted(by_session[s].keys())
        for i, (wi, wn) in enumerate(wins):
            last_w = i == len(wins) - 1
            wbranch = "└─" if last_w else "├─"
            wmark = "*" if win_active[(s, wi)] else " "
            lines.append(f"  {wbranch} {wmark}{wi}:{wn}")
            ps = sorted(by_session[s][(wi, wn)], key=lambda x: x["pane_index"])
            for j, pane in enumerate(ps):
                last_p = j == len(ps) - 1
                pre = "     " if last_w else "  │  "
                pbranch = "└─" if last_p else "├─"
                pmark = "◉" if pane["pane_active"] else "·"
                pid = pane["pane_id"]
                cmd = pane["command"]
                sz = pane["size"]
                persona = pane.get("persona") or ""
                state = pane.get("state") or ""
                extras = []
                if persona:
                    extras.append(f"persona={persona}")
                if state:
                    extras.append(f"state={state}")
                tail = "  " + " ".join(extras) if extras else ""
                lines.append(
                    f"{pre}{pbranch} {pmark} {pid:>4}  {cmd:<10}  {sz}{tail}"
                )
    return "\n".join(lines)


def render_json(panes: list[dict]) -> str:
    """Group into a nested session→windows→panes tree for JSON emit."""
    by_session: dict = collections.defaultdict(lambda: collections.defaultdict(list))
    sess_meta: dict = {}
    win_meta: dict = {}
    for p in panes:
        s = p["session"]
        wkey = (p["window_index"], p["window_name"])
        by_session[s][wkey].append(p)
        sess_meta.setdefault(s, {"name": s, "attached": p["session_attached"]})
        win_meta.setdefault(
            (s, p["window_index"]),
            {
                "index": p["window_index"],
                "name": p["window_name"],
                "active": p["window_active"],
            },
        )

    out = {"sessions": []}
    for s in sorted(by_session):
        sess = dict(sess_meta[s])
        sess["windows"] = []
        for wi, wn in sorted(by_session[s].keys()):
            w = dict(win_meta[(s, wi)])
            w["panes"] = sorted(
                [
                    {
                        "id": p["pane_id"],
                        "index": p["pane_index"],
                        "pid": p["pane_pid"],
                        "command": p["command"],
                        "size": p["size"],
                        "active": p["pane_active"],
                        **(
                            {"persona": p["persona"]}
                            if p.get("persona")
                            else {}
                        ),
                        **(
                            {"claude_session": p["claude_session"]}
                            if p.get("claude_session")
                            else {}
                        ),
                        **({"state": p["state"]} if p.get("state") else {}),
                    }
                    for p in by_session[s][(wi, wn)]
                ],
                key=lambda x: x["index"],
            )
            sess["windows"].append(w)
        out["sessions"].append(sess)
    return json.dumps(out, indent=2)


def main() -> None:
    ap = argparse.ArgumentParser(
        prog="tmux-tree",
        description="Hierarchical snapshot of all tmux sessions, windows, panes.",
    )
    ap.add_argument("--json", action="store_true", help="emit JSON instead of rendered tree")
    ap.add_argument("--session", help="filter to a single session by name")
    ap.add_argument(
        "--no-state",
        action="store_true",
        help="skip pane-states.json join (faster, no persona/state)",
    )
    ap.add_argument(
        "--no-header",
        action="store_true",
        help="omit the timestamp/count header line in rendered output",
    )
    args = ap.parse_args()

    panes = collect_panes(session_filter=args.session, skip_state=args.no_state)
    if args.json:
        print(render_json(panes))
    else:
        print(render_tree(panes, header=not args.no_header))


if __name__ == "__main__":
    main()
