/** Metadata-only lifecycle bridge for Pi and Oh My Pi. */
import { mkdirSync, readFileSync, readlinkSync, renameSync, writeFileSync } from "node:fs";
import { homedir } from "node:os";
import { join } from "node:path";

type Context = {
  cwd: string;
  isIdle(): boolean;
  hasPendingMessages(): boolean;
  sessionManager: { getSessionId(): string; getSessionFile(): string | undefined };
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

  function emit(state: string, event: string, ctx = context) {
    if (!ctx) return;
    context = ctx;
    const record = {
      schema: 1, provider, pid: process.pid, process_start: start,
      pid_namespace: namespace, state, event, updated_at: Date.now() / 1000,
      session_id: ctx.sessionManager.getSessionId(),
      session_file: ctx.sessionManager.getSessionFile(), cwd: ctx.cwd,
    };
    try {
      mkdirSync(directory, { recursive: true, mode: 0o700 });
      const temporary = `${path}.tmp`;
      writeFileSync(temporary, JSON.stringify(record) + "\n", { mode: 0o600 });
      renameSync(temporary, path);
    } catch { /* An indicator failure must never interrupt the agent. */ }
  }

  for (const event of ["agent_start", "turn_start", "tool_call", "tool_execution_start", "session_before_compact"]) {
    api.on(event, (_event, ctx) => { generation++; emit("working", event, ctx); });
  }
  for (const event of ["session_start", "session_switch"]) {
    api.on(event, (_event, ctx) => { generation++; emit(ctx.isIdle() ? "idle" : "working", event, ctx); });
  }
  api.on("agent_end", (event, ctx) => {
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
        const settled = ctx.isIdle() && !ctx.hasPendingMessages() && !boundary.willContinue && !boundary.willRetry;
        emit(settled ? lastTerminal : "working", "agent_end", ctx);
      }
    });
  });
  api.on("agent_settled", (_event, ctx) => {
    generation++;
    emit(ctx.isIdle() && !ctx.hasPendingMessages() ? lastTerminal : "working", "agent_settled", ctx);
  });
  api.on("session_compact", (_event, ctx) => {
    generation++;
    emit(ctx.isIdle() && !ctx.hasPendingMessages() ? "idle" : "working", "session_compact", ctx);
  });
  api.on("session_shutdown", (_event, ctx) => {
    ended = true;
    generation++;
    emit("exited", "session_shutdown", ctx);
  });
  process.once("exit", () => emit("exited", "process_exit"));
}
