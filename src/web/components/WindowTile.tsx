import React from "react";
import type { TopologyWindow, TopologyPane } from "../../topology";
import { C } from "../theme";
import { LayoutTreeRenderer } from "./LayoutTreeRenderer";

/** WindowTile — a window: header strip + its pane layout tree. */
export function WindowTile({ win }: { win: TopologyWindow }) {
  const panes = new Map<string, TopologyPane>(win.panes.map((p) => [p.pane_id, p]));

  return (
    <div
      style={{
        position: "absolute",
        inset: 0,
        padding: 4,
        display: "flex",
        flexDirection: "column",
        overflow: "hidden",
      }}
    >
      <div
        style={{
          display: "flex",
          alignItems: "center",
          gap: 5,
          fontSize: 10,
          color: win.active ? C.text : C.textDim,
          marginBottom: 3,
          whiteSpace: "nowrap",
          overflow: "hidden",
        }}
      >
        <span style={{ color: C.textFaint }}>{win.index}:</span>
        <span style={{ textOverflow: "ellipsis", overflow: "hidden" }}>{win.name}</span>
        <span style={{ color: C.textFaint }}>({win.panes.length})</span>
      </div>
      <div style={{ flex: 1, minHeight: 0, position: "relative" }}>
        {win.layout ? (
          <LayoutTreeRenderer node={win.layout.root} panes={panes} />
        ) : (
          <div
            style={{
              display: "flex",
              flexWrap: "wrap",
              gap: 2,
              width: "100%",
              height: "100%",
            }}
          >
            {win.panes.map((p) => (
              <div key={p.pane_id} style={{ flex: "1 1 40%", minWidth: 40 }}>
                <div
                  style={{
                    width: "100%",
                    height: "100%",
                    border: `1px solid ${C.border}`,
                    borderRadius: 3,
                    fontSize: 9,
                    color: C.textFaint,
                    padding: 2,
                  }}
                >
                  {p.pane_id}
                </div>
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}
