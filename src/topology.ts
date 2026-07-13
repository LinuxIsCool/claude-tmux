/**
 * topology.ts — assemble the live tmux topology for the webui.
 *
 * Pure-read: shells out to tmux + reads claude-tmux's pane-states.json. No writes,
 * no new state authority. The arbiter remains the source of truth for pane state.
 *
 * Join key (verified Phase 0): a window_layout leaf's trailing integer IS the pane
 * id number — leaf.paneIndex N ↔ tmux pane id `%N`.
 */

import { homedir } from "node:os";
import { join } from "node:path";
import { readFile } from "node:fs/promises";
import { parseWindowLayout, type ParsedLayout } from "./web/layout/parseWindowLayout";

const PANE_STATES_FILE = join(homedir(), ".claude/local/tmux/pane-states.json");

export type PaneState = {
  state_name: string;
  color: string;
  glyph: string;
  persona: string | null;
  updated_at: number | null;
  last_working_at: number | null;
  session_start_at: number | null;
};

export type TopologyPane = {
  pane_id: string;
  pane_index: number;
  pid: number | null;
  command: string;
  cwd: string;
  width: number;
  height: number;
  active: boolean;
} & Partial<PaneState>;

export type TopologyWindow = {
  index: number;
  name: string;
  active: boolean;
  layout: ParsedLayout | null;
  layout_raw: string;
  panes: TopologyPane[];
};

export type TopologySession = {
  name: string;
  attached: boolean;
  windows: TopologyWindow[];
};

export type Topology = {
  generated_at: string;
  session_count: number;
  window_count: number;
  pane_count: number;
  sessions: TopologySession[];
  skipped_panes: number;
};

const PANE_FIELDS = [
  "#{session_name}",
  "#{window_index}",
  "#{pane_index}",
  "#{pane_id}",
  "#{pane_pid}",
  "#{pane_current_command}",
  "#{pane_current_path}",
  "#{pane_width}",
  "#{pane_height}",
  "#{pane_active}",
  "#{session_attached}",
].join("\t");

const WIN_FIELDS = [
  "#{session_name}",
  "#{window_index}",
  "#{window_name}",
  "#{window_active}",
  "#{window_layout}",
].join("\t");

async function tmux(args: string[]): Promise<string> {
  const proc = Bun.spawn(["tmux", ...args], { stdout: "pipe", stderr: "pipe" });
  const [out, code] = await Promise.all([new Response(proc.stdout).text(), proc.exited]);
  if (code !== 0) {
    const err = await new Response(proc.stderr).text();
    throw new Error(`tmux ${args.join(" ")} failed (${code}): ${err.trim()}`);
  }
  return out;
}

async function loadPaneStates(): Promise<Record<string, PaneState>> {
  try {
    const raw = JSON.parse(await readFile(PANE_STATES_FILE, "utf-8"));
    const out: Record<string, PaneState> = {};
    for (const [id, v] of Object.entries(raw as Record<string, any>)) {
      out[id] = {
        state_name: v.state_name ?? "idle",
        color: v.color ?? "#89b4fa",
        glyph: v.glyph ?? "⬤",
        persona: typeof v.persona === "string" ? v.persona.trim() || null : null,
        updated_at: v.updated_at ?? null,
        last_working_at: v.last_working_at ?? null,
        session_start_at: v.session_start_at ?? null,
      };
    }
    return out;
  } catch {
    return {};
  }
}

/** Build the full topology snapshot. */
export async function getTopology(): Promise<Topology> {
  const [paneOut, winOut, states] = await Promise.all([
    tmux(["list-panes", "-aF", PANE_FIELDS]),
    tmux(["list-windows", "-aF", WIN_FIELDS]),
    loadPaneStates(),
  ]);

  let skipped = 0;

  // panes keyed by "session\twindow_index" → TopologyPane[]
  const panesByWindow = new Map<string, TopologyPane[]>();
  let paneCount = 0;
  for (const line of paneOut.split("\n")) {
    if (!line) continue;
    const p = line.split("\t");
    if (p.length !== 11) {
      skipped++; // tab-in-name corruption (tmux does not escape tabs)
      continue;
    }
    const [sname, widx, pidx, paneId, pid, cmd, cwd, w, h, pactive, sattached] = p;
    const st = states[paneId!];
    const pane: TopologyPane = {
      pane_id: paneId!,
      pane_index: Number.parseInt(pidx!, 10),
      pid: /^\d+$/.test(pid!) ? Number.parseInt(pid!, 10) : null,
      command: cmd!,
      cwd: cwd!,
      width: Number.parseInt(w!, 10),
      height: Number.parseInt(h!, 10),
      active: pactive === "1",
      ...(st ?? {}),
    };
    void sattached;
    const key = `${sname}\t${widx}`;
    (panesByWindow.get(key) ?? panesByWindow.set(key, []).get(key)!).push(pane);
    paneCount++;
  }

  // windows
  const sessions = new Map<string, TopologySession>();
  let windowCount = 0;
  for (const line of winOut.split("\n")) {
    if (!line) continue;
    const p = line.split("\t");
    if (p.length !== 5) {
      skipped++;
      continue;
    }
    const [sname, widx, wname, wactive, layoutRaw] = p;
    let layout: ParsedLayout | null = null;
    try {
      layout = parseWindowLayout(layoutRaw!);
    } catch {
      layout = null; // degrade: window renders flat if layout unparseable
    }
    const win: TopologyWindow = {
      index: Number.parseInt(widx!, 10),
      name: wname!,
      active: wactive === "1",
      layout,
      layout_raw: layoutRaw!,
      panes: panesByWindow.get(`${sname}\t${widx}`) ?? [],
    };
    let sess = sessions.get(sname!);
    if (!sess) {
      // attached flag lives on panes; derive from first pane lookup is unreliable,
      // so query is omitted here — attached resolved below from session list.
      sess = { name: sname!, attached: false, windows: [] };
      sessions.set(sname!, sess);
    }
    sess.windows.push(win);
    windowCount++;
  }

  // attached flag per session
  try {
    const sOut = await tmux(["list-sessions", "-F", "#{session_name}\t#{session_attached}"]);
    for (const line of sOut.split("\n")) {
      if (!line) continue;
      const [sname, att] = line.split("\t");
      const sess = sessions.get(sname!);
      if (sess) sess.attached = att === "1";
    }
  } catch {
    /* non-fatal */
  }

  const sessionList = [...sessions.values()].sort((a, b) => a.name.localeCompare(b.name));
  for (const s of sessionList) s.windows.sort((a, b) => a.index - b.index);

  return {
    generated_at: new Date().toISOString(),
    session_count: sessionList.length,
    window_count: windowCount,
    pane_count: paneCount,
    skipped_panes: skipped,
    sessions: sessionList,
  };
}
