"""Exercise the real producer using private sockets and fake system commands."""
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "scripts/hook-payload.py"
HOOK = ROOT / "hooks/hook.sh"
SETUP = ROOT / "scripts/claude-tmux-setup.sh"

FAKE_COMMAND = '''#!{python}
import json, os, sys
from pathlib import Path
name = Path(sys.argv[0]).name
with open(os.environ["COMMAND_LOG"], "a") as stream:
    stream.write(json.dumps({{"command": name, "args": sys.argv[1:]}}) + "\\n")
if name == "systemctl":
    if "is-active" in sys.argv:
        sys.exit(0 if os.environ.get("UNIT_ACTIVE") == "1" else 1)
    if "show" in sys.argv:
        print(os.environ.get("UNIT_LOAD", "not-found"))
        sys.exit(int(os.environ.get("UNIT_SHOW_EXIT", "0")))
    if "start" in sys.argv:
        sys.exit(int(os.environ.get("UNIT_START_EXIT", "0")))
if name == "tmux":
    # Stop full setup before unrelated appearance/hook changes: no #I supplied.
    # Fallback hooks still execute their real state resolution against this fake.
    sys.exit(0)
sys.exit(0)
'''


class HookTransportTests(unittest.TestCase):
    def setUp(self):
        # Keep AF_UNIX paths short and isolate every child process from real HOME.
        self.temporary = tempfile.TemporaryDirectory(prefix="ct-hook-")
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.home = self.directory / "home"
        self.bin = self.directory / "bin"
        self.home.mkdir()
        self.bin.mkdir()
        self.log = self.directory / "commands.jsonl"
        for name in ("systemctl", "uv", "tmux"):
            path = self.bin / name
            path.write_text(FAKE_COMMAND.format(python=sys.executable))
            path.chmod(0o755)
        self.environment = dict(os.environ,
            HOME=str(self.home), XDG_CONFIG_HOME=str(self.home / ".config"),
            XDG_DATA_HOME=str(self.home / ".local/share"),
            XDG_RUNTIME_DIR=str(self.directory / "runtime"),
            TMUX_TMPDIR=str(self.directory),
            TMUX=f'{self.directory}/source,server.sock,123,0', TMUX_PANE="%0",
            CLAUDE_TMUX_SOCKET="never-live-test",
            CLAUDE_TMUX_THEME=str(ROOT / "theme.json"),
            PATH=str(self.bin) + os.pathsep + os.environ.get("PATH", ""),
            COMMAND_LOG=str(self.log), PYTHONDONTWRITEBYTECODE="1")

    def calls(self, name=None):
        rows = [json.loads(line) for line in self.log.read_text().splitlines()] if self.log.exists() else []
        return [r for r in rows if name is None or r["command"] == name]

    def test_symlinked_hook_resolves_its_canonical_helper_and_fallback(self):
        entry = self.directory / "linked-hook.sh"
        entry.symlink_to(HOOK)
        with patch(__name__ + ".HOOK", entry):
            result = self.hook({"session_id": "session-123"})
        self.assertEqual(result.stdout, b"{}\n")
        self.assertEqual(result.returncode, 0)
        self.assertTrue(self.calls("tmux"), "linked entry must reach the real fallback")

    def helper(self, data, event="PreToolUse", pane="%0", tmux=None):
        raw = data if isinstance(data, bytes) else json.dumps(data).encode()
        return subprocess.run([sys.executable, str(HELPER), event, pane,
                               tmux or self.environment["TMUX"]], input=raw,
                              capture_output=True, env=self.environment, timeout=5)

    def hook(self, data, event="PreToolUse"):
        raw = data if isinstance(data, bytes) else json.dumps(data).encode()
        return subprocess.run(["bash", str(HOOK), event], input=raw,
                              capture_output=True, env=self.environment, timeout=5)

    def test_helper_drops_bodies_and_preserves_lifecycle_fields(self):
        data = {"session_id": "session-123", "_provider": "codex", "cwd": "/project",
                "source": "resume", "permission_mode": "bypassPermissions", "tool_name": "Skill",
                "notification_type": "permission_prompt", "agent_type": "helper",
                "prompt": "PRIVATE-PROMPT", "tool_response": "PRIVATE-RESPONSE",
                "tool_input": {"file_path": '/project/quote"\nfile', "skill": "review",
                               "command": "PRIVATE-COMMAND", "content": "PRIVATE-BODY" * 100000}}
        started = time.time()
        result = self.helper(data)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout.count(b"\n"), 1)
        row = json.loads(result.stdout)
        self.assertEqual(row["source"]["socket_path"], self.environment["TMUX"].rsplit(",", 2)[0])
        self.assertEqual(row["source"]["provider"], "claude")
        self.assertEqual(row["source"]["session_id"], "session-123")
        self.assertLessEqual(started, row["source"]["sent_at"])
        self.assertLessEqual(row["source"]["sent_at"], time.time())
        self.assertEqual(row["data"]["_provider"], "claude")
        for key in ("session_id", "cwd", "source", "permission_mode", "tool_name", "notification_type", "agent_type"):
            self.assertEqual(row["data"][key], data[key])
        self.assertEqual(row["data"]["tool_input"], {"file_path": '/project/quote"\nfile', "skill": "review"})
        self.assertNotIn(b"PRIVATE-", result.stdout)
        self.assertLess(len(result.stdout), 48 * 1024)

    def test_worst_case_string_escaping_stays_below_receiver_limit(self):
        data = {key: "\x01" * 10000 for key in ("cwd", "source", "permission_mode", "tool_name", "notification_type", "agent_type")}
        data.update(session_id="s" * 256, tool_input={"file_path": "\x02" * 10000, "skill": "🟢" * 10000})
        result = self.helper(data, tmux="/" + "x" * 1023 + ",123,0")
        row = json.loads(result.stdout)
        self.assertLess(len(result.stdout), 48 * 1024)
        self.assertEqual(result.stdout.count(b"\n"), 1)
        self.assertLessEqual(len(row["data"]["cwd"].encode()), 2048)
        self.assertLessEqual(len(row["data"]["agent_type"].encode()), 256)
        self.assertLessEqual(len(row["data"]["tool_input"]["skill"].encode()), 512)

    def test_malformed_or_oversized_stdin_never_emits_an_event(self):
        for raw in (b"{", b"[]", b"null", b'"text"', b"\xff", b"[" * 1200,
                    b" " * (4 * 1024 * 1024 + 1)):
            with self.subTest(size=len(raw)):
                result = self.helper(raw)
                self.assertEqual(result.returncode, 0)
                self.assertEqual(result.stdout, b"")
                self.assertEqual(result.stderr, b"")

    def test_identity_is_exact_or_rejected_and_event_cannot_inject_ndjson(self):
        for args in ({"event": 'Stop"\n{}'}, {"pane": "%0\n%1"}, {"tmux": "relative,123,0"},
                     {"tmux": "/tmp/sock,not-a-pid,0"}):
            with self.subTest(args=args):
                self.assertEqual(self.helper({"session_id": "s"}, **args).stdout, b"")
        self.assertEqual(self.helper({"session_id": "s" * 257}).stdout, b"")

    def private_event(self, response=b""):
        if not shutil.which("socat"):
            self.skipTest("socat unavailable")
        path = self.home / ".claude/local/tmux/arbiter.sock"
        path.parent.mkdir(parents=True)
        received = []
        with socket.socket(socket.AF_UNIX) as listener:
            listener.bind(str(path))
            listener.listen(1)
            listener.settimeout(3)
            def receive():
                with listener.accept()[0] as client:
                    client.settimeout(3)
                    data = b""
                    while not data.endswith(b"\n"):
                        part = client.recv(65536)
                        if not part:
                            break
                        data += part
                    received.append(data)
                    if response:
                        client.sendall(response)
            thread = threading.Thread(target=receive)
            thread.start()
            try:
                result = self.hook({"session_id": "session-123", "tool_name": "Write",
                                    "tool_input": {"file_path": "/p", "content": "SECRET-BODY" * 100000}})
            finally:
                thread.join(4)
        self.assertFalse(thread.is_alive())
        return result, received

    def test_real_private_socket_receives_one_bounded_ndjson_record(self):
        result, received = self.private_event()
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, b"{}\n")
        self.assertEqual(len(received), 1)
        self.assertEqual(received[0].count(b"\n"), 1)
        row = json.loads(received[0])
        self.assertEqual(row["event"], "PreToolUse")
        self.assertEqual(row["data"]["tool_input"], {"file_path": "/p"})
        self.assertEqual(row["source"]["socket_path"], self.environment["TMUX"].rsplit(",", 2)[0])
        self.assertNotIn(b"SECRET-BODY", received[0])
        self.assertLess(len(received[0]), 65536)
        self.assertFalse(self.calls("tmux"), "transport must not invoke fallback state writes")

    def test_receiver_rejection_does_not_leak_into_hook_stdout(self):
        result, received = self.private_event(b'{"type":"event_rejected","reason":"wrong session"}\n')
        self.assertEqual(len(received), 1)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, b"{}\n")
        self.assertEqual(result.stderr, b"")

    def test_malformed_hook_is_nonfatal_and_does_not_touch_tmux(self):
        result = self.hook(b"not-json")
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, b"{}\n")
        self.assertFalse(self.calls())

    def test_missing_or_invalid_binding_never_guesses_the_focused_pane(self):
        for missing, replacement in (("TMUX_PANE", None), ("TMUX_PANE", "%bad"),
                                     ("TMUX", None), ("TMUX", "not-a-server")):
            with self.subTest(missing=missing, replacement=replacement):
                original = dict(self.environment)
                if replacement is None:
                    self.environment.pop(missing, None)
                else:
                    self.environment[missing] = replacement
                result = self.hook({"session_id": "s"})
                self.environment = original
                self.assertEqual(result.returncode, 0)
                self.assertEqual(result.stdout, b"{}\n")
                self.assertFalse(self.calls(), "missing binding must not query default focus")

    def test_missing_socket_preserves_fallback_state_resolution(self):
        result = self.hook({"session_id": "s", "tool_name": "Write", "tool_input": {"content": "PRIVATE"}})
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, b"{}\n")
        sets = [r["args"] for r in self.calls("tmux") if r["args"][:1] == ["set"]]
        self.assertIn(["set", "-p", "-t", "%0", "@claude_state", "🟢"], sets)
        self.assertIn(["set", "-p", "-t", "%0", "@claude_color", "#a6e3a1"], sets)

    def setup_script(self):
        result = subprocess.run(["bash", str(SETUP)], env=self.environment, capture_output=True, timeout=5)
        self.assertEqual(result.returncode, 1)  # fake tmux intentionally has no #I
        return self.calls("systemctl"), self.calls("uv")

    def test_loaded_unit_starts_under_systemd(self):
        self.environment["UNIT_LOAD"] = "loaded"
        systemctl, uv = self.setup_script()
        self.assertTrue(any("start" in c["args"] for c in systemctl))
        self.assertFalse(uv)

    def test_failed_or_masked_installed_unit_never_falls_back_to_uv(self):
        self.environment.update(UNIT_LOAD="masked", UNIT_START_EXIT="1")
        systemctl, uv = self.setup_script()
        self.assertTrue(any("start" in c["args"] for c in systemctl))
        self.assertFalse(uv)

    def test_unit_file_stays_managed_when_systemd_query_is_unavailable(self):
        self.environment.update(UNIT_LOAD="", UNIT_SHOW_EXIT="1", UNIT_START_EXIT="1")
        unit = self.home / ".config/systemd/user/claude-tmux-arbiter.service"
        unit.parent.mkdir(parents=True)
        unit.write_text("[Service]\nExecStart=/not-executed\n")
        systemctl, uv = self.setup_script()
        self.assertTrue(any("start" in c["args"] for c in systemctl))
        self.assertFalse(uv)

    def test_no_installed_unit_retains_direct_launcher(self):
        self.environment["UNIT_LOAD"] = "not-found"
        systemctl, uv = self.setup_script()
        self.assertFalse(any("start" in c["args"] for c in systemctl))
        self.assertEqual(len(uv), 1)
        self.assertEqual(uv[0]["args"], ["run", str(ROOT / "scripts/tmux-arbiter.py")])

    def test_active_unit_needs_no_other_start(self):
        self.environment.update(UNIT_ACTIVE="1", UNIT_LOAD="loaded")
        systemctl, uv = self.setup_script()
        self.assertFalse(any("start" in c["args"] for c in systemctl))
        self.assertFalse(uv)


if __name__ == "__main__":
    unittest.main()
