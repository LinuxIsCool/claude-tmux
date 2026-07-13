/**
 * fibonacciPack.ts — pack N window tiles into a session box along a golden spiral.
 *
 * At each step we cut the box at the golden point (minor = 0.382·dim, major =
 * 0.618·dim), peel the MINOR slice off as one tile, and recurse into the MAJOR
 * slice with one fewer tile. The cut side rotates right→bottom→left→top with
 * depth, so tiles spiral inward; the final tile is the innermost remainder.
 *
 * Properties (golden-tested): exact tiling (Σarea == box area), no overlap,
 * all rects in-bounds, works for any aspect ratio and any count ≥ 1.
 *
 * Pure, dependency-free.
 */

export type Rect = { x: number; y: number; w: number; h: number };

const PHI = 1.618_033_988_749_895;
const MINOR = 1 - 1 / PHI; // ≈ 0.381966 — the peeled-tile fraction

/**
 * @param width  box width
 * @param height box height
 * @param count  number of tiles (windows)
 * @returns `count` non-overlapping rects that exactly tile the box, spiraling inward.
 */
export function fibonacciPack(width: number, height: number, count: number): Rect[] {
  if (count <= 0) return [];
  const rects: Rect[] = [];

  function rec(x: number, y: number, w: number, h: number, n: number, depth: number): void {
    if (n === 1) {
      rects.push({ x, y, w, h });
      return;
    }
    switch (depth % 4) {
      case 0: {
        // vertical cut, tile on the right
        const minor = w * MINOR;
        const major = w - minor;
        rects.push({ x: x + major, y, w: minor, h });
        rec(x, y, major, h, n - 1, depth + 1);
        break;
      }
      case 1: {
        // horizontal cut, tile on the bottom
        const minor = h * MINOR;
        const major = h - minor;
        rects.push({ x, y: y + major, w, h: minor });
        rec(x, y, w, major, n - 1, depth + 1);
        break;
      }
      case 2: {
        // vertical cut, tile on the left
        const minor = w * MINOR;
        const major = w - minor;
        rects.push({ x, y, w: minor, h });
        rec(x + minor, y, major, h, n - 1, depth + 1);
        break;
      }
      default: {
        // horizontal cut, tile on the top
        const minor = h * MINOR;
        const major = h - minor;
        rects.push({ x, y, w, h: minor });
        rec(x, y + minor, w, major, n - 1, depth + 1);
        break;
      }
    }
  }

  rec(0, 0, width, height, count, 0);
  return rects;
}
