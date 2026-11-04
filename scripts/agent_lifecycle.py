"""Read process-bound native lifecycle metadata without retaining message bodies.

Claude's session registry is authoritative even during a silent provider wait.
Pi/OMP transcripts cover persisted messages, not the first unflushed provider
request or extension dialogs. Missing authority is reported as unknown.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
import json
from pathlib import Path
import re


ENV_KEYS = {"TMUX", "TMUX_PANE", "CODEX_HOME", "PI_CODING_AGENT_DIR", "OMP_CODING_AGENT_DIR"}
MAX_READ = 512 * 1024
MAX_LINE = 256 * 1024


@dataclass
class Process:
    pid: int
    ppid: int
    start: str
    comm: str
    args: tuple[str, ...] = ()
    cwd: str = ""
    environment: dict[str, str] = field(default_factory=dict)
    exe: str = ""


def read_processes(proc_root: Path = Path("/proc")) -> dict[int, Process]:
    """One bounded metadata pass, shared by provider collectors."""
    result = {}
    for directory in proc_root.iterdir():
        if not directory.name.isdigit():
            continue
        try:
            raw = (directory / "stat").read_text()
            end = raw.rindex(")")
            fields = raw[end + 2:].split()
            if fields[0] == "Z":
                continue
            comm = raw[raw.index("(") + 1:end]
            process = Process(int(directory.name), int(fields[1]), fields[19], comm)
            result[process.pid] = process
            # Only harness candidates need args/environment. Wrapper ancestry
            # needs just pid/ppid, including nested bubblewrap PID namespaces.
            if comm in {"claude", "pi", "omp", "codex", "node", "bun"}:
                process.args = tuple(x.decode(errors="replace") for x in
                    (directory / "cmdline").read_bytes()[:MAX_READ].split(b"\0") if x)
                environment = (directory / "environ").read_bytes()[:MAX_READ]
                process.environment = {k: v for x in environment.decode(errors="replace").split("\0")
                    if "=" in x for k, v in [x.split("=", 1)] if k in ENV_KEYS}
                process.cwd = str((directory / "cwd").readlink())
                process.exe = str((directory / "exe").readlink())
        except (OSError, ValueError, IndexError):
            continue  # Process exited while its metadata was being sampled.
    return result


def pane_processes(root_pid: int, processes: dict[int, Process]) -> list[Process]:
    """Descendants by ancestry, independent of pane_current_command or titles."""
    children: dict[int, list[Process]] = {}
    for process in processes.values():
        children.setdefault(process.ppid, []).append(process)
    pending = [processes[root_pid]] if root_pid in processes else []
    result, visited = [], set()
    while pending:
        process = pending.pop()
        if process.pid in visited:
            continue
        visited.add(process.pid)
        result.append(process)
        pending.extend(children.get(process.pid, ()))
    return result


def _provider(process: Process) -> str | None:
    if process.comm in {"claude", "pi", "omp"}:
        return process.comm
    for argument in process.args[:3]:
        if "/@earendil-works/pi-coding-agent/" in argument:
            return "pi"
        if "/@oh-my-pi/pi-coding-agent/" in argument:
            return "omp"
    return None


def outer_harness_processes(root_pid: int, processes: dict[int, Process]) -> list[Process]:
    """The outer supported harness owns its pane across provider collectors."""
    candidates = [p for p in pane_processes(root_pid, processes)
        if _provider(p) or (Path(p.exe).name == "codex" and "app-server" not in p.args)]
    candidate_ids = {p.pid for p in candidates}
    result = []
    for candidate in candidates:
        current, visited, nested = candidate, set(), False
        while current.ppid in processes and current.ppid not in visited:
            if current.ppid in candidate_ids:
                nested = True
                break
            visited.add(current.ppid)
            current = processes[current.ppid]
        if not nested:
            result.append(candidate)
    return result


def _json(path: Path, limit: int = MAX_LINE) -> dict:
    with path.open("rb") as stream:
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise ValueError("metadata too large")
    result = json.loads(data)
    if not isinstance(result, dict):
        raise ValueError("metadata must be an object")
    return result


def _timestamp(value) -> float | None:
    try:
        if isinstance(value, (float, int)):
            return value / 1000 if value > 10**11 else float(value)
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError, AttributeError):
        return None


class Transcript:
    """Incremental JSONL reader with bounded reads and partial-line buffering."""
    def __init__(self):
        self.identity = None
        self.offset = 0
        self.partial = b""
        self.dropping = False
        self.state = "unknown"
        self.observed_at = None
        self.session_id = ""

    def poll(self, path: Path) -> dict:
        stat = path.stat()
        identity = (stat.st_dev, stat.st_ino)
        if identity != self.identity or stat.st_size < self.offset:
            self.__init__()
            self.identity = identity
            # Initial tail avoids replaying entire large conversations. Discard
            # the first partial line rather than interpreting truncated JSON.
            self.offset = max(0, stat.st_size - MAX_READ)
            self.dropping = self.offset > 0
        with path.open("rb") as stream:
            stream.seek(self.offset)
            chunk = stream.read(MAX_READ)
        self.offset += len(chunk)
        for segment in chunk.splitlines(keepends=True):
            ended = segment.endswith(b"\n")
            if self.dropping:
                if ended:
                    self.dropping = False
                continue
            if len(self.partial) + len(segment) > MAX_LINE:
                self.partial = b""
                self.dropping = not ended
                self.state = "unknown"
                continue
            self.partial += segment
            if not ended:
                continue
            raw, self.partial = self.partial, b""
            try:
                record = json.loads(raw)
                self._record(record)
            except (ValueError, TypeError, AttributeError):
                self.state = "unknown"
        if self.partial or self.dropping or self.offset < stat.st_size:
            return {"state": "unknown", "observed_at": self.observed_at}
        return {"state": self.state, "observed_at": self.observed_at}

    def _record(self, record: dict):
        if record.get("type") == "session":
            self.session_id = str(record.get("id", ""))
        if record.get("type") != "message":
            return
        message = record.get("message", {})
        role = message.get("role")
        if role in {"user", "toolResult"}:
            self.state = "working"
        elif role == "assistant":
            reason = message.get("stopReason")
            self.state = {"toolUse": "working", "stop": "idle", "length": "idle",
                          "aborted": "idle", "error": "error"}.get(reason, "unknown")
        else:
            return
        self.observed_at = _timestamp(record.get("timestamp"))


class Collector:
    def __init__(self, home: Path = Path.home(), proc_root: Path = Path("/proc")):
        self.home, self.proc_root = Path(home), Path(proc_root)
        self.transcripts: dict[tuple[int, str, str], Transcript] = {}

    def poll(self, panes: dict, processes: dict[int, Process] | None = None) -> dict:
        processes = read_processes(self.proc_root) if processes is None else processes
        result = {}
        live_keys = set()
        for pane, metadata in panes.items():
            candidates = outer_harness_processes(int(metadata["pid"]), processes)
            if not candidates:
                continue
            if len(candidates) != 1:
                result[pane] = {"provider": "unknown", "state": "unknown", "source": "ambiguous-process",
                                "session_id": "", "process_id": 0, "process_start": ""}
                continue
            process = candidates[0]
            provider = _provider(process)
            if provider is None:  # The outer Codex owner has its own collector.
                continue
            row = {"provider": provider, "state": "unknown", "source": "process",
                   "session_id": "", "process_id": process.pid, "process_start": process.start}
            try:
                if provider == "claude":
                    row.update(self._claude(process, pane))
                else:
                    native = self._native(process, provider)
                    if native:
                        row.update(native)
                        result[pane] = row
                        continue
                    path, session_id = self._session(process, provider)
                    if path:
                        key = (process.pid, process.start, str(path))
                        live_keys.add(key)
                        transcript = self.transcripts.setdefault(key, Transcript())
                        observation = transcript.poll(path)
                        row.update(observation)
                        # Transcript messages lack live agent boundaries. Even
                        # an explicit launch path can become historical after
                        # a session switch, so this is always advisory.
                        row["last_message_state"] = row["state"]
                        row["state"] = "unknown"
                        row.update(source="transcript", session_id=session_id, source_path=str(path))
            except (OSError, ValueError, TypeError, KeyError, AttributeError, IndexError):
                pass
            result[pane] = row
        self.transcripts = {k: v for k, v in self.transcripts.items() if k in live_keys}
        return result

    def _native(self, process: Process, provider: str) -> dict:
        environment = process.environment
        directory = environment.get("PI_CODING_AGENT_DIR") or environment.get("OMP_CODING_AGENT_DIR")
        base = Path(directory) if directory else self.home / (".pi/agent" if provider == "pi" else ".omp/agent")
        # process.pid inside a bubblewrap PID namespace differs from host PID.
        local_pid = process.pid
        try:
            status = (self.proc_root / str(process.pid) / "status").read_text()
            match = re.search(r"^NSpid:\s+([\d\s]+)$", status, re.MULTILINE)
            if match:
                local_pid = int(match.group(1).split()[-1])
            path = base / "tmux-lifecycle" / f"{local_pid}-{process.start}.json"
            record = _json(path)
            namespace = str((self.proc_root / str(process.pid) / "ns/pid").readlink())
        except (OSError, ValueError):
            return {}
        if (record.get("schema") != 1 or record.get("pid") != local_pid
                or str(record.get("process_start")) != process.start
                or record.get("pid_namespace") != namespace
                or record.get("cwd") != process.cwd
                or record.get("provider") != provider):
            return {}
        raw = (self.proc_root / str(process.pid) / "stat").read_text()
        if raw[raw.rindex(")") + 2:].split()[19] != process.start:
            return {}
        state = record.get("state")
        return {"state": state if state in {"working", "idle", "asking", "error"} else "unknown",
                "source": "native-extension", "source_path": str(path),
                "session_id": str(record.get("session_id", "")),
                "observed_at": _timestamp(record.get("updated_at")), "event": record.get("event")}

    def _claude(self, process: Process, pane: str) -> dict:
        path = self.home / ".claude/sessions" / f"{process.pid}.json"
        record = _json(path)
        if record.get("pid") != process.pid or str(record.get("procStart")) != process.start:
            return {}
        # Pane IDs can recur on another server. Verify both process ancestry
        # above and the native registration's exact pane when it supplies one.
        registration = record.get("tmux")
        if registration and registration.rsplit(".", 1)[-1] != pane:
            return {}
        domain = record.get("pidDomain")
        if domain:
            machine = (self.proc_root / str(process.pid) / "root/etc/machine-id").read_text().strip()
            namespace = (self.proc_root / str(process.pid) / "ns/pid").readlink()
            if domain != f"linux:{machine}:{namespace}":
                return {}
        # The process can exit or its PID can be reused during file reads.
        raw = (self.proc_root / str(process.pid) / "stat").read_text()
        if raw[raw.rindex(")") + 2:].split()[19] != process.start:
            return {}
        # Claude publishes shell only when its base state is idle and a
        # background shell remains. It does not mean the model is generating.
        state = {"busy": "working", "shell": "idle", "idle": "idle", "waiting": "asking"}
        return {"state": state.get(record.get("status"), "unknown"), "source": "claude-session",
                "session_id": str(record.get("sessionId", "")), "source_path": str(path),
                "observed_at": _timestamp(record.get("statusUpdatedAt")), "native_status": record.get("status")}

    def _session(self, process: Process, provider: str) -> tuple[Path | None, str]:
        args = process.args
        # Explicit path is an identity binding, unlike choosing the newest log
        # for a cwd shared by several agents. Bare session IDs are unresolved.
        for index, argument in enumerate(args[:-1]):
            if argument in {"--session", "-s"} and args[index + 1].endswith(".jsonl"):
                path = Path(args[index + 1])
                if not path.is_absolute():
                    path = Path(process.cwd) / path
                header = self._header(path)
                if header.get("cwd") == process.cwd:
                    return path, str(header.get("id", ""))
                return None, ""
        # Cwd, creation time and uniqueness cannot bind a transcript to a PID.
        # Another agent in the same directory can create the only new file.
        return None, ""

    @staticmethod
    def _header(path: Path) -> dict:
        with path.open("rb") as stream:
            raw = stream.readline(MAX_LINE + 1)
        if len(raw) > MAX_LINE or not raw.endswith(b"\n"):
            raise ValueError("invalid session header")
        header = json.loads(raw)
        if not isinstance(header, dict) or header.get("type") != "session":
            raise ValueError("invalid session header")
        return header
