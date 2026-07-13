import React from "react";
import type { LayoutNode } from "../layout/parseWindowLayout";
import type { TopologyPane } from "../../topology";
import { PaneCell } from "./PaneCell";

/**
 * LayoutTreeRenderer — render a parsed tmux layout tree as nested flexboxes.
 *
 * `{}` split → flex-direction:row, children sized by their `w`.
 * `[]` split → flex-direction:column, children sized by their `h`.
 * Leaf → PaneCell, resolved from pane id `%<paneIndex>`.
 */
export function LayoutTreeRenderer({
  node,
  panes,
}: {
  node: LayoutNode;
  panes: Map<string, TopologyPane>;
}) {
  if (node.kind === "leaf") {
    const pane = panes.get(`%${node.paneIndex}`);
    return <PaneCell pane={pane} paneIndex={node.paneIndex} />;
  }

  const isRow = node.dir === "row";
  // basis proportional to the child's extent along the split axis
  const extents = node.children.map((c) => (isRow ? c.w : c.h));
  const total = extents.reduce((s, e) => s + e, 0) || 1;

  return (
    <div
      style={{
        display: "flex",
        flexDirection: isRow ? "row" : "column",
        width: "100%",
        height: "100%",
        gap: 2,
      }}
    >
      {node.children.map((child, i) => (
        <div
          key={i}
          style={{ flexGrow: extents[i]! / total, flexBasis: 0, minWidth: 0, minHeight: 0 }}
        >
          <LayoutTreeRenderer node={child} panes={panes} />
        </div>
      ))}
    </div>
  );
}
