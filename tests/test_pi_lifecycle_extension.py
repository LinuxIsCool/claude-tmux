"""Native extension contract, driven without paid providers or user sessions."""
import json
import importlib.util
import os
import queue
from pathlib import Path
import shutil
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest


EXTENSION = Path(__file__).parents[1] / "scripts/pi-lifecycle-extension.ts"
spec = importlib.util.spec_from_file_location("agent_lifecycle", EXTENSION.with_name("agent_lifecycle.py"))
lifecycle_module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = lifecycle_module
spec.loader.exec_module(lifecycle_module)


def test_native_extension_quiet_wait_completion_continuation_and_shutdown(tmp_path):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node with TypeScript stripping unavailable")
    source = """
import lifecycle from __EXTENSION__;
import { readFileSync, readdirSync } from 'node:fs';
const handlers = new Map();
lifecycle({on(name, handler) {handlers.set(name, handler);}});
let idle = true, queued = false;
const ctx = {cwd: process.cwd(), isIdle: () => idle, hasPendingMessages: () => queued,
  sessionManager: {getSessionId: () => 'test-session', getSessionFile: () => '/test/session.jsonl'}};
const dir = process.env.PI_CODING_AGENT_DIR + '/tmux-lifecycle/';
function snapshot() { return JSON.parse(readFileSync(dir + readdirSync(dir)[0], 'utf8')); }
const results = [];
handlers.get('session_start')({}, ctx); results.push(snapshot());
idle = false; handlers.get('agent_start')({}, ctx);
await new Promise(resolve => setTimeout(resolve, 70)); results.push(snapshot());
idle = true; handlers.get('agent_end')({}, ctx);
await new Promise(resolve => setImmediate(resolve)); results.push(snapshot());
handlers.get('agent_settled')({}, ctx);
await new Promise(resolve => setImmediate(resolve)); results.push(snapshot());
// A queued continuation cannot briefly publish idle.
queued = true; handlers.get('agent_settled')({}, ctx);
await new Promise(resolve => setImmediate(resolve)); results.push(snapshot());
// An immediate new start invalidates the previous deferred completion.
queued = false; handlers.get('agent_end')({}, ctx);
idle = false; handlers.get('agent_start')({}, ctx);
await new Promise(resolve => setImmediate(resolve)); results.push(snapshot());
handlers.get('session_shutdown')({}, ctx); results.push(snapshot());
console.log(JSON.stringify(results));
""".replace("__EXTENSION__", json.dumps(EXTENSION.as_uri()))
    environment = {"PATH": os.environ.get("PATH", ""), "PI_CODING_AGENT_DIR": str(tmp_path)}
    result = subprocess.run([node, "--input-type=module", "-e", source], env=environment,
                            text=True, capture_output=True, timeout=15)
    assert result.returncode == 0, result.stderr
    snapshots = json.loads(result.stdout)
    assert [row["state"] for row in snapshots] == ["idle", "working", "working", "idle", "working", "working", "exited"]
    assert all(row["schema"] == 1 and row["process_start"] and row["pid_namespace"] for row in snapshots)
    assert all(row["session_id"] == "test-session" for row in snapshots)
    assert not list((tmp_path / "tmux-lifecycle").glob("*.tmp"))


def test_native_ui_promises_preserve_input_state_results_errors_and_session_ownership(tmp_path):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node with TypeScript stripping unavailable")
    source = """
import lifecycle from __EXTENSION__;
import assert from 'node:assert/strict';
import { readFileSync, readdirSync } from 'node:fs';
const handlers = new Map();
lifecycle({on(name, handler) {handlers.set(name, handler);}});
let idle = true, invalid = false;
const pending = [];
const nativeReturns = [];
const failure = new Error('native dialog failed');
const ui = {
  select() { assert.equal(this, ui); const value = new Promise((resolve, reject) => pending.push({resolve, reject})); nativeReturns.push(value); return value; },
  confirm() { assert.equal(this, ui); const value = new Promise((resolve, reject) => pending.push({resolve, reject})); nativeReturns.push(value); return value; },
  input() { throw failure; },
  editor() { return undefined; },
  custom() { return this.select(); },
  notify() { return 'notification'; },
};
const originalNotify = ui.notify;
const ctx = {cwd: process.cwd(), ui,
  isIdle() { if (invalid) throw new Error('replaced context'); return idle; },
  hasPendingMessages() { assert.equal(invalid, false, 'must never inspect replaced context queues'); return false; },
  sessionManager: {getSessionId: () => 'ui-session', getSessionFile: () => undefined}};
const dir = process.env.PI_CODING_AGENT_DIR + '/tmux-lifecycle/';
function record() { return JSON.parse(readFileSync(dir + readdirSync(dir)[0], 'utf8')); }
function state(expected) { assert.equal(record().state, expected); }
async function flush() { await new Promise(resolve => setImmediate(resolve)); }
handlers.get('session_start')({}, ctx); state('idle');
assert.equal(ui.notify, originalNotify); assert.equal(ui.notify(), 'notification'); state('idle');

// Concurrent dialogs retain asking until the last promise settles. Values
// and the original rejected Error are delivered unchanged to callers.
const first = ui.select(); state('asking');
const second = ui.confirm(); state('asking');
assert.equal(first, nativeReturns[0]); assert.equal(second, nativeReturns[1]);
pending.shift().resolve('chosen'); assert.equal(await first, 'chosen'); await flush(); state('asking');
const secondPending = pending.shift();
const caught = second.catch(error => error); secondPending.reject(failure);
assert.equal(await caught, failure); await flush(); state('idle');
assert.throws(() => ui.input(), error => error === failure); state('idle');
assert.equal(ui.editor(), undefined); state('idle');

// Cancellation is a native undefined return and nested UI calls do not
// prematurely clear the outside dialog's pending state.
const nested = ui.custom(); state('asking');
pending.shift().resolve(undefined); assert.equal(await nested, undefined); await flush(); state('idle');

// New native work while a dialog is open stays asking, then restores work.
const duringWork = ui.select(); state('asking');
idle = false; handlers.get('agent_start')({}, ctx); state('asking');
pending.shift().resolve('resume'); await duringWork; await flush(); state('working');
idle = true; handlers.get('agent_end')({}, ctx); state('working');
handlers.get('agent_settled')({}, ctx); state('idle');

// A late settlement from a shut-down session must not overwrite exited or
// inspect invalid queues. A new session sharing the UI object still works.
const oldDialog = ui.select(); state('asking');
const oldPending = pending.shift();
handlers.get('session_shutdown')({}, ctx); state('exited');
invalid = true; oldPending.resolve('late'); await oldDialog; await flush(); state('exited');
handlers.get('agent_settled')({}, ctx); state('exited');
handlers.get('agent_start')({}, ctx); state('exited');
handlers.get('session_compact')({}, ctx); state('exited');
invalid = false; handlers.get('session_start')({}, ctx); state('idle');
const afterRestart = ui.confirm(); state('asking');
pending.shift().resolve(false); assert.equal(await afterRestart, false); await flush(); state('idle');

// Switching sessions invalidates old dialogs without waiting for them.
const switched = ui.select(); const switchedPending = pending.shift(); state('asking');
handlers.get('session_switch')({}, ctx); state('idle');
const current = ui.select(); state('asking');
switchedPending.resolve('stale'); await switched; await flush(); state('asking');
pending.shift().resolve('current'); await current; await flush(); state('idle');

// OMP's per-handler UI Proxy delegates into the shared parent UI. Observe
// the native parent instead of capturing a proxy method that would recurse.
const parentContext = {...ctx, ui};
const scopedContext = Object.create(parentContext);
const scopedUI = new Proxy(ui, {get(target, key) {
  if (key === 'select') return (...args) => target.select(...args);
  return Reflect.get(target, key, target);
}});
Object.defineProperty(scopedContext, 'ui', {value: scopedUI});
handlers.get('session_start')({}, scopedContext); state('idle');
const scoped = scopedContext.ui.select(); state('asking');
pending.shift().resolve('scoped'); assert.equal(await scoped, 'scoped'); await flush(); state('idle');
console.log('native dialog contract passed');
""".replace("__EXTENSION__", json.dumps(EXTENSION.as_uri()))
    result = subprocess.run([node, "--input-type=module", "-e", source],
        env={"PATH": os.environ.get("PATH", ""), "PI_CODING_AGENT_DIR": str(tmp_path)},
        text=True, capture_output=True, timeout=15)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "native dialog contract passed"


@pytest.mark.parametrize("harness", ["pi", "omp"])
@pytest.mark.parametrize("response", [{"value": "Done"}, {"cancelled": True}], ids=["choice", "cancel"])
def test_real_native_rpc_dialog_publishes_asking_then_idle_without_model_request(tmp_path, harness, response):
    executable = shutil.which(harness)
    if not executable:
        pytest.skip(f"{harness} CLI unavailable")
    profile = tmp_path / "profile"
    profile.mkdir()
    # A closed loopback port makes any accidental model request fail. The
    # fixture opens the actual SDK dialog directly, without a model turn.
    (profile / ("models.json" if harness == "pi" else "models.yml")).write_text(json.dumps({
        "providers": {"native-ui-local": {"baseUrl": "http://127.0.0.1:1/v1",
        "api": "openai-completions", "apiKey": "local-fixture", "models": [
            {"id": "test", "reasoning": False, "contextWindow": 8192, "maxTokens": 64}]}}}))
    fixture = tmp_path / "native-dialog.ts"
    fixture.write_text("""
export default function (api) {
  api.on('session_start', (_event, ctx) => {
    setTimeout(() => { void ctx.ui.select('Native UI acceptance', ['Done']).catch(() => {}); }, 80);
  });
}
""")
    arguments = [executable, "--no-extensions", "--no-skills", "--mode", "rpc",
        "--extension", str(EXTENSION), "--extension", str(fixture)]
    arguments += (["--offline", "--no-context-files", "--no-prompt-templates", "--provider", "native-ui-local", "--model", "test"]
        if harness == "pi" else ["--no-tools", "--no-rules", "--no-lsp", "--no-title", "--no-prewalk", "--model", "native-ui-local/test"])
    process = subprocess.Popen(arguments, cwd=tmp_path,
        env={"HOME": str(tmp_path), "PATH": os.environ.get("PATH", ""),
             "PI_CODING_AGENT_DIR": str(profile), "PI_OFFLINE": "1"},
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    requests = queue.Queue()

    def read_protocol():
        for line in process.stdout:
            try:
                frame = json.loads(line)
            except ValueError:
                continue
            if frame.get("type") == "extension_ui_request" and frame.get("method") == "select":
                requests.put(frame)

    threading.Thread(target=read_protocol, daemon=True).start()
    try:
        request = requests.get(timeout=15)
        files = list((profile / "tmux-lifecycle").glob("*.json"))
        assert len(files) == 1
        observed = json.loads(files[0].read_text())
        assert observed["state"] == "asking"
        assert observed["event"] == "ui_select"
        assert observed["pid"] == process.pid
        row = lifecycle_module.Collector(home=tmp_path).poll({"%fixture": {"pid": process.pid}})["%fixture"]
        assert row["state"] == "asking"
        assert row["source"] == "native-extension"
        process.stdin.write(json.dumps({"type": "extension_ui_response", "id": request["id"], **response}) + "\n")
        process.stdin.flush()
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            final = json.loads(files[0].read_text())
            if final["state"] == "idle" and final["event"] == "ui_select_end":
                assert final["session_id"] == observed["session_id"]
                assert final["process_start"] == observed["process_start"]
                break
            time.sleep(0.02)
        else:
            pytest.fail("native SDK dialog resolution did not return lifecycle to idle")
    finally:
        process.terminate()
        process.wait(timeout=10)


@pytest.mark.parametrize("harness", ["pi", "omp"])
def test_real_rpc_cli_quiet_provider_wait_and_native_completion(tmp_path, harness):
    executable = shutil.which(harness)
    if not executable:
        pytest.skip(f"{harness} CLI unavailable")
    requested, release = threading.Event(), threading.Event()

    class Provider(BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length", 0)))
            requested.set()
            if not release.wait(15):
                self.send_error(503)
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            chunks = [
                {"id": "local-test", "object": "chat.completion.chunk", "model": "test", "created": 1,
                 "choices": [{"index": 0, "delta": {"role": "assistant", "content": "done"}, "finish_reason": None}]},
                {"id": "local-test", "object": "chat.completion.chunk", "model": "test", "created": 1,
                 "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                 "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}},
            ]
            for chunk in chunks:
                self.wfile.write(("data: " + json.dumps(chunk) + "\n\n").encode())
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Provider)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    agent_dir = tmp_path / "agent"
    agent_dir.mkdir()
    (agent_dir / ("models.json" if harness == "pi" else "models.yml")).write_text(json.dumps({"providers": {"lifecycle-local": {
        "baseUrl": f"http://127.0.0.1:{server.server_port}/v1", "api": "openai-completions",
        "apiKey": "local-fixture", "models": [{"id": "test", "reasoning": False,
            "contextWindow": 8192, "maxTokens": 128}]}}}))
    (agent_dir / ("settings.json" if harness == "pi" else "config.yml")).write_text(json.dumps({"retry": {"enabled": False},
        "compaction": {"enabled": False}}))
    environment = {"HOME": str(tmp_path), "PATH": os.environ.get("PATH", ""),
        "PI_CODING_AGENT_DIR": str(agent_dir), "PI_OFFLINE": "1"}
    arguments = [executable, "--no-extensions", "--no-skills", "--extension", str(EXTENSION), "--mode", "rpc"]
    arguments += (["--offline", "--no-context-files", "--no-prompt-templates", "--provider", "lifecycle-local", "--model", "test"]
        if harness == "pi" else ["--no-tools", "--no-rules", "--no-lsp", "--no-title", "--no-prewalk", "--model", "lifecycle-local/test"])
    process = subprocess.Popen(arguments,
        cwd=tmp_path, env=environment, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL, text=True)

    def wait_state(expected):
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            files = list((agent_dir / "tmux-lifecycle").glob("*.json"))
            if files:
                row = json.loads(files[0].read_text())
                if row["state"] == expected:
                    return row
            assert process.poll() is None, "isolated Pi exited before native lifecycle observation"
            time.sleep(0.02)
        pytest.fail(f"isolated Pi did not publish native state {expected}")

    try:
        idle = wait_state("idle")
        process.stdin.write(json.dumps({"id": "fixture", "type": "prompt", "message": "local fixture"}) + "\n")
        process.stdin.flush()
        assert requested.wait(15), "isolated CLI did not request loopback fixture"
        working = wait_state("working")
        # Exceed the old CPU fallback's six-second idle holdoff. No native
        # heartbeat or terminal output is needed to preserve working.
        time.sleep(6.5)
        assert wait_state("working")["updated_at"] == working["updated_at"]
        row = lifecycle_module.Collector(home=tmp_path).poll({"%fixture": {"pid": process.pid}})["%fixture"]
        assert row["state"] == "working"
        assert row["source"] == "native-extension"
        release.set()
        completed = wait_state("idle")
        assert completed["event"] == ("agent_settled" if harness == "pi" else "agent_end")
        assert completed["session_id"] == idle["session_id"]
        assert completed["pid"] == process.pid
    finally:
        release.set()
        process.terminate()
        process.wait(timeout=10)
        server.shutdown()
        server.server_close()


@pytest.mark.parametrize("harness", ["pi", "omp"])
def test_default_extension_directory_autoloads_without_explicit_flag(tmp_path, harness):
    executable = shutil.which(harness)
    if not executable:
        pytest.skip(f"{harness} CLI unavailable")
    agent_dir = tmp_path / (".pi/agent" if harness == "pi" else ".omp/agent")
    extensions = agent_dir / "extensions"
    extensions.mkdir(parents=True)
    shutil.copyfile(EXTENSION, extensions / "legion-tmux.ts")
    model_file = agent_dir / ("models.json" if harness == "pi" else "models.yml")
    model_file.write_text(json.dumps({"providers": {"lifecycle-local": {
        "baseUrl": "http://127.0.0.1:1/v1", "api": "openai-completions", "apiKey": "local-fixture",
        "models": [{"id": "test", "reasoning": False, "contextWindow": 8192, "maxTokens": 128}]}}}))
    environment = {"HOME": str(tmp_path), "PATH": os.environ.get("PATH", ""), "PI_OFFLINE": "1"}
    arguments = [executable, "--mode", "rpc", "--no-skills"]
    arguments += (["--offline", "--no-context-files", "--no-prompt-templates", "--provider", "lifecycle-local", "--model", "test"]
        if harness == "pi" else ["--no-tools", "--no-rules", "--no-lsp", "--no-title", "--no-prewalk", "--model", "lifecycle-local/test"])
    process = subprocess.Popen(arguments, cwd=tmp_path, env=environment,
        stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, text=True)
    try:
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            files = list((agent_dir / "tmux-lifecycle").glob("*.json"))
            if files:
                row = json.loads(files[0].read_text())
                assert row["state"] == "idle"
                assert row["event"] == "session_start"
                assert row["provider"] == harness
                assert row["pid"] == process.pid
                return
            assert process.poll() is None, "isolated harness exited before default extension loaded"
            time.sleep(0.02)
        pytest.fail(f"{harness} did not auto-discover its default agent/extensions directory")
    finally:
        process.terminate()
        process.wait(timeout=10)
