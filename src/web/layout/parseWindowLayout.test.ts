/**
 * Golden tests for parseWindowLayout against REAL tmux layout fixtures
 * captured 2026-06-02 (see ~/.claude/local/research/2026/06/02/tmux-webui-fixtures/).
 *
 * Run: bun test  (from plugins/claude-tmux/)
 */
import { describe, expect, test } from "bun:test";
import {
  LayoutParseError,
  leafPaneIndices,
  parseWindowLayout,
  type SplitNode,
} from "./parseWindowLayout";

describe("parseWindowLayout — real fixtures", () => {
  test("single-pane window (leaf root)", () => {
    // 0 1 claude-statusline cd7d,319x76,0,0,0
    const { checksum, root } = parseWindowLayout("cd7d,319x76,0,0,0");
    expect(checksum).toBe("cd7d");
    expect(root).toEqual({ kind: "leaf", w: 319, h: 76, x: 0, y: 0, paneIndex: 0 });
    expect(leafPaneIndices(root)).toEqual([0]);
  });

  test("nested split: two columns, right column split into two rows", () => {
    // 0 2 claude-ventures 4c73,319x76,0,0{159x76,0,0,2,159x76,160,0[159x38,160,0,7,159x37,160,39,15]}
    const { checksum, root } = parseWindowLayout(
      "4c73,319x76,0,0{159x76,0,0,2,159x76,160,0[159x38,160,0,7,159x37,160,39,15]}",
    );
    expect(checksum).toBe("4c73");
    expect(root.kind).toBe("split");

    const r = root as SplitNode;
    expect(r.dir).toBe("row"); // {} = columns side by side
    expect(r.w).toBe(319);
    expect(r.h).toBe(76);
    expect(r.children).toHaveLength(2);

    // left column: leaf pane 2
    expect(r.children[0]).toEqual({
      kind: "leaf",
      w: 159,
      h: 76,
      x: 0,
      y: 0,
      paneIndex: 2,
    });

    // right column: vertical split (rows) of panes 7 (top) and 15 (bottom)
    const right = r.children[1] as SplitNode;
    expect(right.kind).toBe("split");
    expect(right.dir).toBe("col"); // [] = rows stacked
    expect(right.x).toBe(160);
    expect(right.children).toHaveLength(2);
    expect(right.children[0]).toEqual({
      kind: "leaf",
      w: 159,
      h: 38,
      x: 160,
      y: 0,
      paneIndex: 7,
    });
    expect(right.children[1]).toEqual({
      kind: "leaf",
      w: 159,
      h: 37,
      x: 160,
      y: 39,
      paneIndex: 15,
    });

    // leaves in layout order
    expect(leafPaneIndices(root)).toEqual([2, 7, 15]);
  });

  test("simple two-pane horizontal split (synthetic, columns)", () => {
    const { root } = parseWindowLayout("bb62,208x50,0,0{104x50,0,0,1,103x50,105,0,2}");
    const r = root as SplitNode;
    expect(r.dir).toBe("row");
    expect(leafPaneIndices(root)).toEqual([1, 2]);
  });

  test("simple two-pane vertical split (synthetic, rows)", () => {
    const { root } = parseWindowLayout("abcd,80x24,0,0[80x12,0,0,3,80x11,0,13,4]");
    const r = root as SplitNode;
    expect(r.dir).toBe("col");
    expect(leafPaneIndices(root)).toEqual([3, 4]);
  });

  test("geometry is preserved exactly (no rounding)", () => {
    const { root } = parseWindowLayout(
      "4c73,319x76,0,0{159x76,0,0,2,159x76,160,0[159x38,160,0,7,159x37,160,39,15]}",
    );
    const right = (root as SplitNode).children[1] as SplitNode;
    // 38 + 1 (divider) + 37 = 76 — tmux reserves 1 row for the pane border
    const [top, bottom] = right.children;
    expect(top.h + 1 + bottom.h).toBe(right.h);
  });
});

describe("parseWindowLayout — error handling", () => {
  test("missing checksum throws", () => {
    expect(() => parseWindowLayout("319x76,0,0,0")).toThrow(LayoutParseError);
  });

  test("malformed dims throws", () => {
    expect(() => parseWindowLayout("cd7d,319x,0,0,0")).toThrow(LayoutParseError);
  });

  test("unterminated group throws", () => {
    expect(() => parseWindowLayout("cd7d,319x76,0,0{159x76,0,0,2")).toThrow(
      LayoutParseError,
    );
  });

  test("trailing garbage throws", () => {
    expect(() => parseWindowLayout("cd7d,319x76,0,0,0XYZ")).toThrow(LayoutParseError);
  });
});
