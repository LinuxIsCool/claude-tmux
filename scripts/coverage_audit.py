#!/usr/bin/env python3
"""Focus resolution coverage audit.

Analyzes recent Claude Code session transcripts to measure what percentage
of file paths resolve to focus signals (venture, plugin, or domain).

Usage:
    uv run scripts/coverage_audit.py [--sessions N] [--verbose]
"""
# /// script
# requires-python = ">=3.11"
# ///

import json
import sys
from pathlib import Path

TRANSCRIPT_DIR = Path("~/.claude/projects").expanduser()
EMOJI_PATH = Path("~/.claude/local/emoji/emoji_flat.json").expanduser()
VENTURES_PATH = Path("~/.claude/local/emoji/ventures.json").expanduser()

# Domain fallback paths (must match tmux-arbiter.py _DOMAIN_PATHS)
_DOMAIN_PATHS = [
    (str(Path("~/Documents").expanduser()), "domain:documents"),
    (str(Path("~/docs").expanduser()), "domain:documents"),
    (str(Path("~/.config").expanduser()), "domain:config"),
    (str(Path("~/.claude/image-cache").expanduser()), "domain:config"),
    (str(Path("~/.claude/projects").expanduser()), "domain:config"),
    (str(Path("~/Workspace").expanduser()), "domain:workspace"),
]


def extract_file_paths(transcript_path: Path) -> list[str]:
    """Extract file_path values from tool_use blocks in a session transcript."""
    paths = []
    with open(transcript_path) as f:
        for line in f:
            try:
                d = json.loads(line)
                if d.get("type") != "assistant":
                    continue
                msg = d.get("message", {})
                content = msg.get("content", [])
                if isinstance(content, list):
                    for item in content:
                        if isinstance(item, dict) and item.get("type") == "tool_use":
                            fp = item.get("input", {}).get("file_path", "")
                            if fp:
                                paths.append(fp)
            except (json.JSONDecodeError, KeyError):
                continue
    return paths


def resolve_path(
    path: str,
    emoji: dict[str, str],
    ventures: dict[str, list[str]],
) -> tuple[str, str]:
    """Attempt to resolve a file path to venture + plugin/domain keys."""
    venture_key = ""
    plugin_key = ""
    local_prefix = str(Path("~/.claude/local/").expanduser())

    # Plugin detection
    if "/plugins/" in path:
        parts = path.split("/plugins/")
        if len(parts) > 1:
            name = parts[-1].split("/")[0]
            if name and name != "local" and not name.startswith("."):
                plugin_key = f"plugin:{name}"

    if not plugin_key and path.startswith(local_prefix):
        remainder = path[len(local_prefix):].lstrip("/")
        name = remainder.split("/")[0]
        if name and not name.startswith("."):
            candidate = f"claude-{name}"
            if f"plugin:{candidate}" in emoji:
                plugin_key = f"plugin:{candidate}"

    # Venture detection
    for slug, prefixes in ventures.items():
        for prefix in prefixes:
            if path.startswith(prefix):
                venture_key = f"venture:{slug}"
                break
        if venture_key:
            break

    # Domain fallback
    if not plugin_key:
        for prefix, domain_key in _DOMAIN_PATHS:
            if path.startswith(prefix):
                plugin_key = domain_key
                break

    return (venture_key, plugin_key)


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Focus resolution coverage audit")
    parser.add_argument("--sessions", type=int, default=5, help="Number of recent sessions")
    parser.add_argument("--verbose", action="store_true", help="Show unresolved paths")
    args = parser.parse_args()

    emoji = json.loads(EMOJI_PATH.read_text())
    ventures = json.loads(VENTURES_PATH.read_text())

    transcripts = sorted(TRANSCRIPT_DIR.glob("*.jsonl"), key=lambda f: f.stat().st_mtime)
    transcripts = transcripts[-args.sessions:]

    total = 0
    resolved = 0
    by_category: dict[str, int] = {}
    unresolved: dict[str, int] = {}

    for t in transcripts:
        paths = extract_file_paths(t)
        for p in paths:
            total += 1
            v, pl = resolve_path(p, emoji, ventures)
            if v or pl:
                resolved += 1
                cat = (pl or v).split(":")[0]
                by_category[cat] = by_category.get(cat, 0) + 1
            else:
                parts = p.split("/")
                key = "/".join(parts[:5]) if len(parts) > 4 else p
                unresolved[key] = unresolved.get(key, 0) + 1

    pct = 100 * resolved / total if total else 0
    print(f"Coverage: {resolved}/{total} ({pct:.1f}%)")
    print(f"Sessions: {len(transcripts)}")

    if by_category:
        print(f"\nBy category:")
        for cat, count in sorted(by_category.items(), key=lambda x: -x[1]):
            print(f"  {cat:10s}  {count:4d} ({100*count/total:.1f}%)")

    if unresolved and args.verbose:
        print(f"\nUnresolved ({len(unresolved)}):")
        for k, v in sorted(unresolved.items(), key=lambda x: -x[1]):
            print(f"  {v:4d}x  {k}")

    # Exit code: 0 = ≥90%, 1 = <90%
    sys.exit(0 if pct >= 90.0 else 1)


if __name__ == "__main__":
    main()
