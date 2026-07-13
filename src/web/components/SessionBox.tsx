import React from "react";
import type { TopologySession } from "../../topology";
import { fibonacciPack } from "../layout/fibonacciPack";
import { C } from "../theme";
import { WindowTile } from "./WindowTile";

const HEADER_H = 26;

/**
 * SessionBox — one tmux session. Header strip + a Fibonacci-spiral packing of its
 * windows into the body. Tile geometry is computed from the box's measured size.
 */
export function SessionBox({
  session,
  width,
  height,
}: {
  session: TopologySession;
  width: number;
  height: number;
}) {
  const bodyW = width - 2;
  const bodyH = height - HEADER_H - 2;
  const rects = fibonacciPack(bodyW, bodyH, session.windows.length);

  return (
    <div
      style={{
        width,
        height,
        background: C.bgElev,
        border: `2px solid ${session.attached ? C.borderActive : C.border}`,
        borderRadius: 8,
        overflow: "hidden",
        display: "flex",
        flexDirection: "column",
      }}
    >
      <div
        style={{
          height: HEADER_H,
          display: "flex",
          alignItems: "center",
          gap: 6,
          padding: "0 9px",
          fontSize: 12,
          fontWeight: 600,
          color: C.text,
          background: C.panel,
          borderBottom: `1px solid ${C.border}`,
        }}
      >
        <span style={{ color: session.attached ? "#a6e3a1" : C.textFaint }}>
          {session.attached ? "●" : "○"}
        </span>
        <span style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
          {session.name}
        </span>
        <span style={{ color: C.textFaint, fontWeight: 400 }}>
          {session.windows.length}w
        </span>
      </div>
      <div style={{ position: "relative", flex: 1, minHeight: 0 }}>
        {session.windows.map((win, i) => {
          const r = rects[i];
          if (!r) return null;
          return (
            <div
              key={win.index}
              style={{
                position: "absolute",
                left: r.x,
                top: r.y,
                width: r.w,
                height: r.h,
                border: `1px solid ${C.border}`,
                borderRadius: 5,
                background: C.bg,
                overflow: "hidden",
              }}
            >
              <WindowTile win={win} />
            </div>
          );
        })}
      </div>
    </div>
  );
}
