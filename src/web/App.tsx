import React, { useCallback, useEffect, useState } from "react";
import type { Topology } from "../topology";
import { C } from "./theme";
import { SessionBox } from "./components/SessionBox";

const BOX_W = 384;
const BOX_H = 340;
const POLL_MS = 3000;

export function App() {
  const [topo, setTopo] = useState<Topology | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  const load = useCallback(async () => {
    try {
      const res = await fetch("/api/topology");
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      setTopo(await res.json());
      setErr(null);
    } catch (e) {
      setErr(e instanceof Error ? e.message : "fetch failed");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    load();
    const id = setInterval(load, POLL_MS);
    return () => clearInterval(id);
  }, [load]);

  return (
    <div style={{ height: "100%", display: "flex", flexDirection: "column" }}>
      <header
        style={{
          display: "flex",
          alignItems: "center",
          gap: 14,
          padding: "10px 16px",
          background: C.panel,
          borderBottom: `1px solid ${C.border}`,
          fontSize: 13,
        }}
      >
        <strong style={{ color: C.text, letterSpacing: 0.3 }}>claude-tmux</strong>
        <span style={{ color: C.textDim }}>fleet map</span>
        {topo && (
          <span style={{ color: C.textFaint }}>
            {topo.session_count} sessions · {topo.window_count} windows ·{" "}
            {topo.pane_count} panes
            {topo.skipped_panes > 0 ? ` · ${topo.skipped_panes} skipped` : ""}
          </span>
        )}
        <span style={{ flex: 1 }} />
        {err && <span style={{ color: "#f38ba8" }}>⚠ {err}</span>}
        {topo && (
          <span style={{ color: C.textFaint, fontSize: 11 }}>
            {new Date(topo.generated_at).toLocaleTimeString()}
          </span>
        )}
        <button
          onClick={load}
          style={{
            background: C.bgElev,
            color: C.text,
            border: `1px solid ${C.border}`,
            borderRadius: 6,
            padding: "4px 10px",
            cursor: "pointer",
            fontFamily: "inherit",
            fontSize: 12,
          }}
        >
          ↻ refresh
        </button>
      </header>

      <main style={{ flex: 1, minHeight: 0, overflow: "auto", padding: 16 }}>
        {loading && !topo ? (
          <div style={{ color: C.textDim }}>loading topology…</div>
        ) : (
          <div style={{ display: "flex", flexWrap: "wrap", gap: 16, alignContent: "flex-start" }}>
            {topo?.sessions.map((s) => (
              <SessionBox key={s.name} session={s} width={BOX_W} height={BOX_H} />
            ))}
          </div>
        )}
      </main>
    </div>
  );
}
