/**
 * parseWindowLayout.ts — tmux `window_layout` string → geometry tree.
 *
 * tmux encodes a window's exact pane geometry as a checksummed layout string:
 *
 *   4c73,319x76,0,0{159x76,0,0,2,159x76,160,0[159x38,160,0,7,159x37,160,39,15]}
 *   └──┘ └─────┘     └── child nodes, comma-separated ──────────────────────┘
 *   chk   root dims
 *
 * Grammar (recursive descent):
 *   layout := checksum ',' node
 *   node   := dims ( ',' paneIndex | group )?
 *   dims   := W 'x' H ',' X ',' Y
 *   group  := '{' node (',' node)* '}'    // children left→right  → flex row
 *           | '[' node (',' node)* ']'    // children top→bottom  → flex col
 *
 * `{}` = horizontal arrangement (columns side by side) → CSS flex-direction:row
 * `[]` = vertical arrangement   (rows stacked)         → CSS flex-direction:col
 *
 * Pure, dependency-free. The geometry tree maps directly onto nested flexboxes
 * with flex-basis proportional to the W (row) or H (col) field of each child.
 */

export type LeafNode = {
  kind: "leaf";
  w: number;
  h: number;
  x: number;
  y: number;
  paneIndex: number;
};

export type SplitNode = {
  kind: "split";
  dir: "row" | "col";
  w: number;
  h: number;
  x: number;
  y: number;
  children: LayoutNode[];
};

export type LayoutNode = LeafNode | SplitNode;

export type ParsedLayout = {
  checksum: string;
  root: LayoutNode;
};

class Cursor {
  constructor(public s: string, public i = 0) {}
  peek(): string {
    return this.s[this.i] ?? "";
  }
  eof(): boolean {
    return this.i >= this.s.length;
  }
  expect(ch: string): void {
    if (this.s[this.i] !== ch) {
      throw new LayoutParseError(
        `expected '${ch}' at index ${this.i}, found '${this.peek()}'`,
        this.s,
        this.i,
      );
    }
    this.i++;
  }
  /** Read a run of digits as a non-negative integer. */
  readInt(): number {
    const start = this.i;
    while (this.i < this.s.length && this.s[this.i]! >= "0" && this.s[this.i]! <= "9") {
      this.i++;
    }
    if (this.i === start) {
      throw new LayoutParseError(
        `expected integer at index ${this.i}, found '${this.peek()}'`,
        this.s,
        this.i,
      );
    }
    return Number.parseInt(this.s.slice(start, this.i), 10);
  }
}

export class LayoutParseError extends Error {
  constructor(message: string, public input: string, public index: number) {
    super(`tmux layout parse error: ${message}\n  input: ${input}`);
    this.name = "LayoutParseError";
  }
}

function parseDims(c: Cursor): { w: number; h: number; x: number; y: number } {
  const w = c.readInt();
  c.expect("x");
  const h = c.readInt();
  c.expect(",");
  const x = c.readInt();
  c.expect(",");
  const y = c.readInt();
  return { w, h, x, y };
}

function parseNode(c: Cursor): LayoutNode {
  const { w, h, x, y } = parseDims(c);
  const next = c.peek();

  if (next === "{" || next === "[") {
    const open = next;
    const close = open === "{" ? "}" : "]";
    const dir: "row" | "col" = open === "{" ? "row" : "col";
    c.expect(open);
    const children: LayoutNode[] = [parseNode(c)];
    while (c.peek() === ",") {
      c.expect(",");
      children.push(parseNode(c));
    }
    c.expect(close);
    return { kind: "split", dir, w, h, x, y, children };
  }

  if (next === ",") {
    c.expect(",");
    const paneIndex = c.readInt();
    return { kind: "leaf", w, h, x, y, paneIndex };
  }

  throw new LayoutParseError(
    `expected ',', '{', or '[' after dims at index ${c.i}, found '${next}'`,
    c.s,
    c.i,
  );
}

/**
 * Parse a tmux window_layout string into a geometry tree.
 * @throws {LayoutParseError} on malformed input.
 */
export function parseWindowLayout(layout: string): ParsedLayout {
  const comma = layout.indexOf(",");
  if (comma < 1) {
    throw new LayoutParseError("missing checksum prefix", layout, 0);
  }
  const checksum = layout.slice(0, comma);
  const c = new Cursor(layout, comma + 1);
  const root = parseNode(c);
  if (!c.eof()) {
    throw new LayoutParseError(
      `trailing characters after root node at index ${c.i}`,
      layout,
      c.i,
    );
  }
  return { checksum, root };
}

/** Flatten a parsed layout to its leaf pane indices, in layout order. */
export function leafPaneIndices(node: LayoutNode): number[] {
  if (node.kind === "leaf") return [node.paneIndex];
  return node.children.flatMap(leafPaneIndices);
}
