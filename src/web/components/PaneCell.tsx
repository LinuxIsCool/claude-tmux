import React from "react";
import type { TopologyPane } from "../../topology";
import { C, stateColor } from "../theme";

/**
 * PaneCell — one tmux pane. Phase 1: state-colored border + facts placeholder.
 * Phase 2 replaces the body with a live capture-pane minimap.
 */
export function PaneCell({
  pane,
  paneIndex,
}: {
  pane: TopologyPane | undefined;
  paneIndex: number;
}) {
  const state = pane?.state_name;
  const color = stateColor(state);
  const working = state === "working" || state === "agents";

  return (
    <div
      title={
        pane
          ? `${pane.pane_id} · ${pane.command} · ${pane.width}×${pane.height}` +
            (state ? ` · ${state}` : "")
          : `%${paneIndex} (no pane record)`
      }
      style={{
        width: "100%",
        height: "100%",
        background: C.panel,
        border: `1.5px solid ${color}`,
        borderRadius: 4,
        boxShadow: working ? `0 0 8px ${color}66` : "none",
        animation: working ? "pulse 2.2s ease-in-out infinite" : undefined,
        display: "flex",
        flexDirection: "column",
        overflow: "hidden",
        minWidth: 0,
        minHeight: 0,
      }}
    >
      <div
        style={{
          display: "flex",
          alignItems: "center",
          gap: 4,
          padding: "2px 5px",
          fontSize: 10,
          color: C.textDim,
          borderBottom: `1px solid ${C.border}`,
          whiteSpace: "nowrap",
        }}
      >
        {pane?.persona ? <span>{pane.persona}</span> : null}
        <span style={{ color }}>{pane?.glyph ?? "·"}</span>
        <span style={{ color: C.text }}>{pane?.pane_id ?? `%${paneIndex}`}</span>
        <span style={{ overflow: "hidden", textOverflow: "ellipsis" }}>
          {pane?.command ?? "—"}
        </span>
      </div>
      <div
        style={{
          flex: 1,
          minHeight: 0,
          display: "flex",
          alignItems: "center",
          justifyContent: "center",
          color: C.textFaint,
          fontSize: 9,
        }}
      >
        {pane ? `${pane.width}×${pane.height}` : "no record"}
      </div>
    </div>
  );
}
