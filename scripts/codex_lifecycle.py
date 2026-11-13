"""Read native Codex turn boundaries bound to a live tmux CLI.

No transcript text is returned. A CLI must hold both a root rollout and its
writer lock. Pane environment, process ancestry, and PID start time must agree.
Approval prompts are not persisted by Codex and remain the hook adapter's job.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from datetime import datetime
from pathlib import Path


MAX_READ = 4 * 1024 * 1024
MAX_BACKSCAN = 64 * 1024 * 1024
MAX_META = 1024 * 1024


def _start(proc_root: Path, pid: int) -> str:
    raw = (proc_root / str(pid) / "stat").read_text()
    return raw[raw.rindex(")") + 2:].split()[19]


def _under(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


class Collector:
    def __init__(self, home: Path = Path.home(), proc_root: Path = Path("/proc")):
        self.home = Path(home)
        self.proc_root = Path(proc_root)
        self._metadata: dict[tuple, dict] = {}
        self._tails: dict[Path, dict] = {}

    def _held_rollouts(self, process) -> tuple[set[Path], set[str]]:
        codex_home = Path(process.environment.get("CODEX_HOME") or self.home / ".codex")
        sessions = (codex_home / "sessions").resolve()
        locks = (codex_home / "thread-writer-locks").resolve()
        held_locks = set()
        rollouts = set()
        descriptors = self.proc_root / str(process.pid) / "fd"
        for index, fd in enumerate(descriptors.iterdir()):
            if index >= 4096:
                raise ValueError("too many descriptors to establish lifecycle ownership")
            try:
                target = Path(os.readlink(fd))
                if not target.is_absolute():
                    continue
                if target.parent == locks and target.suffix == ".lock":
                    held_locks.add(target.stem)
                elif target.suffix == ".jsonl" and _under(target, sessions):
                    rollouts.add(target)
            except OSError:
                continue
        return rollouts, held_locks

    def _session_metadata(self, path: Path) -> dict:
        stat = path.stat()
        key = (path, stat.st_dev, stat.st_ino)
        meta = self._metadata.get(key)
        if meta is None:
            with path.open("rb") as handle:
                line = handle.readline(MAX_META + 1)
            if len(line) > MAX_META or not line.endswith(b"\n"):
                raise ValueError("incomplete session metadata")
            row = json.loads(line)
            if row.get("type") != "session_meta":
                raise ValueError("missing session metadata")
            payload = row.get("payload", {})
            meta = {field: payload.get(field) for field in ("id", "parent_thread_id", "source")}
            self._metadata[key] = meta
        return meta

    def _root_rollout(self, process) -> tuple[Path, str] | None:
        rollouts, held_locks = self._held_rollouts(process)
        candidates = []
        for path in rollouts:
            try:
                meta = self._session_metadata(path)
                thread = str(uuid.UUID(meta.get("id", "")))
                if (thread not in held_locks or meta.get("parent_thread_id")
                        or meta.get("source") != "cli"):
                    continue
                candidates.append((path, thread))
            except (OSError, ValueError, TypeError, AttributeError):
                continue
        return candidates[0] if len(candidates) == 1 else None

    def _active_children(self, process, root: str) -> tuple[dict, set[Path]]:
        """Count direct workers from their own native turn boundaries.

        An open relationship is not a running turn. Unknown or changing held
        records make the count unavailable rather than asserting zero.
        """
        used = set()
        try:
            rollouts, held_locks = self._held_rollouts(process)
            seen, active = set(), 0
            for path in rollouts:
                meta = self._session_metadata(path)
                thread = str(uuid.UUID(meta.get("id", "")))
                if thread in seen:
                    raise ValueError("ambiguous held session")
                seen.add(thread)
                if meta.get("parent_thread_id") != root:
                    continue
                if thread not in held_locks:
                    raise ValueError("child writer ownership unavailable")
                used.add(path)
                boundary = self._boundary(path)
                if boundary.get("state") not in {"working", "idle"}:
                    raise ValueError("child turn boundary unavailable")
                active += boundary["state"] == "working"
            if held_locks - seen or self._held_rollouts(process) != (rollouts, held_locks):
                raise ValueError("held session ownership changed")
            return {"child_count_known": True, "active_agent_count": active}, used
        except (OSError, ValueError, TypeError, AttributeError):
            return {"child_count_known": False}, used

    @staticmethod
    def _record_boundary(line: bytes) -> dict | None:
        # Decode only native events, never response text to infer activity.
        if b'"event_msg"' not in line:
            return None
        row = json.loads(line)
        if row.get("type") != "event_msg":
            return None
        payload = row.get("payload", {})
        event = payload.get("type")
        if not isinstance(event, str):
            raise ValueError("invalid lifecycle event")
        if event not in {"task_started", "turn_started", "task_complete", "turn_complete", "turn_aborted"}:
            return None
        timestamp = datetime.fromisoformat(row["timestamp"].replace("Z", "+00:00")).timestamp()
        return {"state": "working" if event in {"task_started", "turn_started"} else "idle",
                "event": event, "turn_id": payload.get("turn_id", ""), "updated_at": timestamp}

    def _cold_boundary(self, path: Path, size: int) -> tuple[dict, int, bool]:
        """Find the newest complete boundary once, with bounded I/O and memory."""
        cursor, scanned, pending = size, 0, b""
        complete, offset = True, size
        with path.open("rb") as handle:
            while cursor and scanned < MAX_BACKSCAN:
                length = min(cursor, MAX_READ, MAX_BACKSCAN - scanned)
                begin = cursor - length
                handle.seek(begin)
                data = handle.read(length)
                if len(data) != length:
                    raise OSError("rollout changed during cold read")
                if not scanned:
                    end = data.rfind(b"\n") + 1
                    complete = end == len(data)
                    if not end:
                        # A record larger than our chunk cannot be validated.
                        return {}, begin, False
                    offset = begin + end
                    data = data[:end]
                lines = (data + pending).split(b"\n")
                pending = lines.pop(0) if begin else b""
                for line in reversed(lines):
                    try:
                        boundary = self._record_boundary(line)
                    except (ValueError, TypeError, KeyError, AttributeError):
                        # Never search through a possibly newer damaged event.
                        return {}, offset, complete
                    if boundary is not None:
                        return boundary, offset, complete
                if len(pending) > MAX_READ:
                    return {}, offset, complete
                cursor, scanned = begin, scanned + length
        return {}, offset, complete

    def _boundary(self, path: Path) -> dict:
        stat = path.stat()
        identity = (stat.st_dev, stat.st_ino)
        prior = self._tails.get(path, {})
        offset = prior.get("offset", 0)
        reset = prior.get("identity") != identity or stat.st_size < offset
        if reset:
            result, offset, complete = self._cold_boundary(path, stat.st_size)
            self._tails[path] = {"identity": identity, "offset": offset, "result": result}
            return dict(result) if complete else {}
        if stat.st_size - offset > MAX_READ:
            offset = max(0, stat.st_size - MAX_READ)
            result = {}
        else:
            result = dict(prior.get("result", {}))
        with path.open("rb") as handle:
            handle.seek(offset)
            data = handle.read(MAX_READ)
        # A cold tail can start mid-record. Ignore that fragment.
        if offset and stat.st_size - prior.get("offset", 0) > MAX_READ:
            newline = data.find(b"\n")
            if newline < 0:
                self._tails[path] = {"identity": identity, "offset": offset + len(data), "result": {}}
                return {}
            offset += newline + 1
            data = data[newline + 1:]
        end = data.rfind(b"\n") + 1
        for line in data[:end].splitlines():
            try:
                boundary = self._record_boundary(line)
                if boundary is not None:
                    result = boundary
            except (ValueError, TypeError, KeyError, AttributeError):
                # A damaged lifecycle record may be a newer turn boundary.
                # Retaining an earlier idle/working result would claim more
                # than this append proves.
                result = {}
        self._tails[path] = {"identity": identity, "offset": offset + end, "result": result}
        if end != len(data):
            # Do not expose the previous boundary while the writer is partway
            # through its next record. Keep the complete-record cursor so the
            # append is read again once its newline arrives.
            return {}
        return dict(result)

    def poll(self, panes: dict[str, dict], processes=None) -> dict[str, dict]:
        from agent_lifecycle import outer_harness_processes, read_processes

        if processes is None:
            processes = read_processes(self.proc_root)
        results = {}
        used_paths = set()
        for pane, info in panes.items():
            shell = int(info["pid"])
            owners = outer_harness_processes(shell, processes)
            # A nested CLI is a tool owned by its outer harness. Both provider
            # collectors use this same ownership decision before projecting.
            if (len(owners) != 1 or Path(owners[0].exe.removesuffix(" (deleted)")).name != "codex"
                    or "app-server" in owners[0].args):
                continue
            result = {"provider": "codex", "state": "unknown", "source": "codex-native-rollout"}
            results[pane] = result
            process = owners[0]
            result.update(process_id=process.pid, process_start=process.start, session_id="")
            try:
                env = process.environment
                server = env.get("TMUX", "").rsplit(",", 2)
                if (env.get("TMUX_PANE") != pane or len(server) != 3
                        or not Path(server[0]).is_absolute() or not server[1].isdigit()):
                    continue
                # Validate the pane shell belongs to this server too.
                current = processes.get(shell)
                seen = set()
                while current is not None and current.pid not in seen and current.pid != int(server[1]):
                    seen.add(current.pid)
                    current = processes.get(current.ppid)
                if current is None or current.pid != int(server[1]):
                    continue
                shell_start = _start(self.proc_root, shell)
                if (shell_start != processes[shell].start
                        or _start(self.proc_root, process.pid) != process.start):
                    continue
                binding = self._root_rollout(process)
                if binding is None:
                    continue
                path, thread = binding
                boundary = self._boundary(path)
                children, child_paths = self._active_children(process, thread)
                if (_start(self.proc_root, process.pid) != process.start
                        or _start(self.proc_root, shell) != shell_start
                        or self._root_rollout(process) != binding):
                    continue
                used_paths.add(path)
                used_paths.update(child_paths)
                result.update(session_id=thread, **boundary, **children)
                title = info.get("title", "")
                if title.startswith("● "):
                    title = title[2:]
                if any(title == prefix or title.startswith(prefix + " | ")
                       for prefix in ("[ ! ] Action Required", "[ . ] Action Required")):
                    # This is a positive, native TUI action-required signal.
                    # Missing title/activity config is never completion proof.
                    result.update(state="asking", event="action_required_title", updated_at=time.time())
            except (OSError, ValueError, TypeError, KeyError):
                continue
        self._tails = {path: value for path, value in self._tails.items() if path in used_paths}
        self._metadata = {key: value for key, value in self._metadata.items() if key[0] in used_paths}
        return results
