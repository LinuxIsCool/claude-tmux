import { describe, expect, test } from "bun:test";
import { fibonacciPack, type Rect } from "./fibonacciPack";

const W = 1000;
const H = 618;

function area(r: Rect): number {
  return r.w * r.h;
}

function overlaps(a: Rect, b: Rect): boolean {
  const eps = 1e-6;
  return (
    a.x + a.w > b.x + eps &&
    b.x + b.w > a.x + eps &&
    a.y + a.h > b.y + eps &&
    b.y + b.h > a.y + eps
  );
}

describe("fibonacciPack", () => {
  test("count 0 → empty", () => {
    expect(fibonacciPack(W, H, 0)).toEqual([]);
  });

  test("count 1 → fills the whole box", () => {
    expect(fibonacciPack(W, H, 1)).toEqual([{ x: 0, y: 0, w: W, h: H }]);
  });

  for (const n of [2, 3, 5, 8, 13, 21]) {
    describe(`n=${n}`, () => {
      const rects = fibonacciPack(W, H, n);

      test("produces exactly n rects", () => {
        expect(rects).toHaveLength(n);
      });

      test("exact tiling — areas sum to box area", () => {
        const total = rects.reduce((s, r) => s + area(r), 0);
        expect(total).toBeCloseTo(W * H, 3);
      });

      test("all rects in-bounds with positive size", () => {
        for (const r of rects) {
          expect(r.w).toBeGreaterThan(0);
          expect(r.h).toBeGreaterThan(0);
          expect(r.x).toBeGreaterThanOrEqual(-1e-6);
          expect(r.y).toBeGreaterThanOrEqual(-1e-6);
          expect(r.x + r.w).toBeLessThanOrEqual(W + 1e-6);
          expect(r.y + r.h).toBeLessThanOrEqual(H + 1e-6);
        }
      });

      test("no two rects overlap", () => {
        for (let i = 0; i < rects.length; i++) {
          for (let j = i + 1; j < rects.length; j++) {
            expect(overlaps(rects[i]!, rects[j]!)).toBe(false);
          }
        }
      });
    });
  }

  test("works on extreme aspect ratios", () => {
    for (const [w, h] of [[2000, 100], [100, 2000], [500, 500]] as const) {
      const rects = fibonacciPack(w, h, 7);
      expect(rects).toHaveLength(7);
      const total = rects.reduce((s, r) => s + area(r), 0);
      expect(total).toBeCloseTo(w * h, 2);
    }
  });

  test("tiles shrink toward the spiral center (first tile larger than last)", () => {
    const rects = fibonacciPack(W, H, 6);
    expect(area(rects[0]!)).toBeGreaterThan(area(rects[4]!));
  });
});
