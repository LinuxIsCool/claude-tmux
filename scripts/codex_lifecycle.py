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

    def _root_rollout(self, process) -> tuple[Path, str] | None:
        codex_home = Path(process.environment.get("CODEX_HOME") or self.home / ".codex")
        sessions = (codex_home / "sessions").resolve()
        locks = (codex_home / "thread-writer-locks").resolve()
        held_locks = set()
        rollouts = set()
        descriptors = self.proc_root / str(process.pid) / "fd"
        for index, fd in enumerate(descriptors.iterdir()):
            if index >= 4096:
                return None
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
        candidates = []
        for path in rollouts:
            try:
                stat = path.stat()
                key = (path, stat.st_dev, stat.st_ino)
                meta = self._metadata.get(key)
                if meta is None:
                    with path.open("rb") as handle:
                        line = handle.readline(MAX_META + 1)
                    if len(line) > MAX_META or not line.endswith(b"\n"):
                        continue
                    row = json.loads(line)
                    if row.get("type") != "session_meta":
                        continue
                    payload = row.get("payload", {})
                    meta = {field: payload.get(field) for field in ("id", "parent_thread_id", "source")}
                    self._metadata[key] = meta
                thread = str(uuid.UUID(meta.get("id", "")))
                if (thread not in held_locks or meta.get("parent_thread_id")
                        or meta.get("source") != "cli"):
                    continue
                candidates.append((path, thread))
            except (OSError, ValueError, TypeError, AttributeError):
                continue
        return candidates[0] if len(candidates) == 1 else None

    def _boundary(self, path: Path) -> dict:
        stat = path.stat()
        identity = (stat.st_dev, stat.st_ino)
        prior = self._tails.get(path, {})
        offset = prior.get("offset", 0)
        reset = prior.get("identity") != identity or stat.st_size < offset
        if reset or stat.st_size - offset > MAX_READ:
            offset = max(0, stat.st_size - MAX_READ)
            result = {}
        else:
            result = dict(prior.get("result", {}))
        with path.open("rb") as handle:
            handle.seek(offset)
            data = handle.read(MAX_READ)
        # A cold tail can start mid-record. Ignore that fragment.
        if offset and (reset or stat.st_size - prior.get("offset", 0) > MAX_READ):
            newline = data.find(b"\n")
            if newline < 0:
                self._tails[path] = {"identity": identity, "offset": offset + len(data), "result": {}}
                return {}
            offset += newline + 1
            data = data[newline + 1:]
        end = data.rfind(b"\n") + 1
        for line in data[:end].splitlines():
            # Filter before decoding unrelated records, including large text.
            if b'"event_msg"' not in line:
                continue
            try:
                row = json.loads(line)
                if row.get("type") != "event_msg":
                    continue
                payload = row.get("payload", {})
                event = payload.get("type")
                if not isinstance(event, str):
                    raise ValueError("invalid lifecycle event")
                if event not in {
                    "task_started", "turn_started", "task_complete", "turn_complete", "turn_aborted"
                }:
                    continue
                timestamp = datetime.fromisoformat(row["timestamp"].replace("Z", "+00:00")).timestamp()
                result = {
                    "state": "working" if event in {"task_started", "turn_started"} else "idle",
                    "event": event,
                    "turn_id": payload.get("turn_id", ""),
                    "updated_at": timestamp,
                }
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
            if (len(owners) != 1 or Path(owners[0].exe).name != "codex"
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
                if (_start(self.proc_root, process.pid) != process.start
                        or _start(self.proc_root, shell) != shell_start
                        or self._root_rollout(process) != binding):
                    continue
                used_paths.add(path)
                result.update(session_id=thread, **boundary)
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
