/** Metadata-only lifecycle bridge for Pi and Oh My Pi. */
import { mkdirSync, readFileSync, readlinkSync, renameSync, writeFileSync } from "node:fs";
import { homedir } from "node:os";
import { join } from "node:path";

type Context = {
  cwd: string;
  isIdle(): boolean;
  hasPendingMessages(): boolean;
  sessionManager: { getSessionId(): string; getSessionFile(): string | undefined };
  ui?: Record<string, (...args: any[]) => any>;
};
type API = { on(event: string, callback: (event: unknown, context: Context) => void): void };

export default function lifecycle(api: API) {
  const provider = process.title === "omp" || /(?:^|\/)bun$/.test(process.execPath) ? "omp" : "pi";
  const agentDir = process.env.PI_CODING_AGENT_DIR || process.env.OMP_CODING_AGENT_DIR
    || join(homedir(), provider === "omp" ? ".omp/agent" : ".pi/agent");
  const start = readFileSync("/proc/self/stat", "utf8").split(") ")[1].split(" ")[19];
  const namespace = readlinkSync("/proc/self/ns/pid");
  const directory = join(agentDir, "tmux-lifecycle");
  const path = join(directory, `${process.pid}-${start}.json`);
  let context: Context | undefined;
  let generation = 0;
  let ended = false;
  let lastTerminal = "idle";
  let baseState = "idle";
  let sessionEpoch = 0;
  let nextDialog = 0;
  const dialogs = new Set<number>();
  const observedUIs = new WeakMap<object, { epoch: number; ctx: Context }>();

  function emit(state: string, event: string, ctx = context, updateBase = true) {
    if (!ctx) return;
    context = ctx;
    if (updateBase) baseState = state;
    try {
      const record = {
        schema: 1, provider, pid: process.pid, process_start: start,
        pid_namespace: namespace, state: state === "exited" ? state : dialogs.size ? "asking" : state,
        event, updated_at: Date.now() / 1000,
        session_id: ctx.sessionManager.getSessionId(),
        session_file: ctx.sessionManager.getSessionFile(), cwd: ctx.cwd,
      };
      mkdirSync(directory, { recursive: true, mode: 0o700 });
      const temporary = `${path}.tmp`;
      writeFileSync(temporary, JSON.stringify(record) + "\n", { mode: 0o600 });
      renameSync(temporary, path);
    } catch { /* An indicator failure must never interrupt the agent. */ }
  }

  function settledState(ctx: Context, terminal = "idle") {
    try { return ctx.isIdle() && !ctx.hasPendingMessages() ? terminal : "working"; }
    catch { return "unknown"; }
  }

  function observeNativeUI(ctx: Context) {
    let ui: Context["ui"];
    try {
      ui = ctx.ui;
      // OMP gives each handler a signal-scoped UI Proxy. Its parent context
      // owns the shared native UI. Wrapping the Proxy would write through to
      // that target while capturing a delegating method, causing recursion.
      const parent = Object.getPrototypeOf(ctx);
      if (parent && Object.prototype.hasOwnProperty.call(parent, "ui")) ui = parent.ui;
    } catch { return; }
    if (!ui || typeof ui !== "object") return;
    const previous = observedUIs.has(ui);
    observedUIs.set(ui, { epoch: sessionEpoch, ctx });
    if (previous) return;
    for (const method of ["select", "confirm", "input", "editor", "custom", "askDialog"]) {
      const original = ui[method];
      if (typeof original !== "function") continue;
      // Keep the SDK method's receiver, exact return value and rejection.
      // Observing a native UI promise does not invent a permission request.
      ui[method] = function (this: unknown, ...args: any[]) {
        const binding = observedUIs.get(ui!);
        if (!binding || ended || binding.epoch !== sessionEpoch) return original.apply(this, args);
        const token = ++nextDialog;
        dialogs.add(token);
        emit(baseState, `ui_${method}`, binding.ctx, false);
        const finish = () => {
          if (ended || binding.epoch !== sessionEpoch || !dialogs.delete(token)) return;
          // A lifecycle event may have changed the underlying state while the
          // dialog was open. Never query a possibly replaced command context.
          emit(baseState, `ui_${method}_end`, context, false);
        };
        try {
          const value = original.apply(this, args);
          if (value && typeof value.then === "function") Promise.resolve(value).then(finish, finish);
          else finish();
          return value;
        } catch (error) {
          finish();
          throw error;
        }
      };
    }
  }

  for (const event of ["agent_start", "turn_start", "tool_call", "tool_execution_start", "session_before_compact"]) {
    api.on(event, (_event, ctx) => { if (ended) return; generation++; emit("working", event, ctx); });
  }
  for (const event of ["session_start", "session_switch"]) {
    api.on(event, (_event, ctx) => {
      generation++;
      sessionEpoch++;
      ended = false;
      dialogs.clear();
      observeNativeUI(ctx);
      emit(settledState(ctx), event, ctx);
    });
  }
  api.on("agent_end", (event, ctx) => {
    if (ended) return;
    const boundary = event as { willContinue?: boolean; willRetry?: boolean; messages?: Array<{role?: string; stopReason?: string}> };
    const current = ++generation;
    const final = boundary.messages?.at(-1);
    lastTerminal = final?.role === "assistant" && final.stopReason === "error" ? "error" : "idle";
    // Pi's agent_end precedes automatic retry/compaction. Its installed native
    // agent_settled event is the actual completion boundary.
    if (provider === "pi") {
      emit("working", "agent_end", ctx);
      return;
    }
    // Let immediate queue continuations publish their start before settling.
    setImmediate(() => {
      if (generation === current && !ended) {
        const settled = settledState(ctx) === "idle" && !boundary.willContinue && !boundary.willRetry;
        emit(settled ? lastTerminal : "working", "agent_end", ctx);
      }
    });
  });
  api.on("agent_settled", (_event, ctx) => {
    if (ended) return;
    generation++;
    emit(settledState(ctx, lastTerminal), "agent_settled", ctx);
  });
  api.on("session_compact", (_event, ctx) => {
    if (ended) return;
    generation++;
    emit(settledState(ctx), "session_compact", ctx);
  });
  api.on("session_shutdown", (_event, ctx) => {
    ended = true;
    generation++;
    sessionEpoch++;
    dialogs.clear();
    emit("exited", "session_shutdown", ctx);
  });
  process.once("exit", () => emit("exited", "process_exit"));
}
