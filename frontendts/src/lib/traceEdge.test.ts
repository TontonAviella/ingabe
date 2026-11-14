import { describe, expect, it } from 'vitest';
import { capped, inside, nearestOnRing, reshaped, thinned } from '@/lib/traceEdge';

// a 100 px square, corners clockwise on screen from the top left
const SQUARE = [
  { x: 0, y: 0 },
  { x: 100, y: 0 },
  { x: 100, y: 100 },
  { x: 0, y: 100 },
];
const area = (ring: { x: number; y: number }[]) =>
  Math.abs(ring.reduce((sum, p, i) => sum + p.x * ring[(i + 1) % ring.length].y - ring[(i + 1) % ring.length].x * p.y, 0)) / 2;
const MIDDLE = { x: 50, y: 50 };

describe('reshaping an outline by drawing along the true edge', () => {
  it('finds the nearest place on the ring', () => {
    const near = nearestOnRing(SQUARE, { x: 108, y: 40 });
    expect(near).toMatchObject({ index: 1, point: { x: 100, y: 40 }, far: 8 });
    expect(near.along).toBeCloseTo(0.4);
    expect(inside(SQUARE, MIDDLE)).toBe(true);
    expect(inside(SQUARE, { x: 150, y: 50 })).toBe(false);
  });

  it('puts a drawn curve in place of the stretch of side between its ends, whichever way it was drawn', () => {
    const out = [
      { x: 100, y: 30 },
      { x: 130, y: 50 },
      { x: 100, y: 70 },
    ];
    for (const path of [out, [...out].reverse()]) {
      const ring = reshaped(SQUARE, path, MIDDLE) as { x: number; y: number }[];
      expect(area(ring)).toBeCloseTo(100 * 100 + (40 * 30) / 2); // the square plus the triangle drawn outside it
      for (const corner of SQUARE) expect(ring).toContainEqual(corner);
      expect(ring).toContainEqual({ x: 130, y: 50 });
    }
    // drawn inward, the same stroke takes a bite out of the field
    const bite = reshaped(
      SQUARE,
      [
        { x: 100, y: 30 },
        { x: 70, y: 50 },
        { x: 100, y: 70 },
      ],
      MIDDLE,
    ) as { x: number; y: number }[];
    expect(area(bite)).toBeCloseTo(100 * 100 - (40 * 30) / 2);
  });

  it('joins a stroke that ends off the line to the nearest place on it', () => {
    const ring = reshaped(
      SQUARE,
      [
        { x: 30, y: 0 },
        { x: 50, y: -20 },
        { x: 70, y: -8 },
      ],
      MIDDLE,
    ) as { x: number; y: number }[];
    expect(ring).toContainEqual({ x: 70, y: 0 }); // where the stroke was joined to the top side
    expect(area(ring)).toBeGreaterThan(100 * 100);
  });

  it('keeps the side of a cut that holds the spot first tapped', () => {
    // a stroke across the square from the top side to the bottom side, near the left
    const cut = [
      { x: 30, y: 0 },
      { x: 30, y: 100 },
    ];
    const left = reshaped(SQUARE, cut, { x: 10, y: 50 }) as { x: number; y: number }[];
    expect(area(left)).toBeCloseTo(30 * 100);
    const right = reshaped(SQUARE, cut, { x: 80, y: 50 }) as { x: number; y: number }[];
    expect(area(right)).toBeCloseTo(70 * 100);
    // a stroke round the tapped spot that would leave it outside either way changes nothing
    expect(reshaped(SQUARE, [{ x: 100, y: 30 }], MIDDLE)).toBeNull();
  });

  it('thins a stroke and caps an outline without losing its ends', () => {
    const line = Array.from({ length: 101 }, (_, i) => ({ x: i, y: 0 }));
    const thin = thinned(line, 10);
    expect(thin[0]).toEqual({ x: 0, y: 0 });
    expect(thin.at(-1)).toEqual({ x: 100, y: 0 });
    expect(thin.length).toBeLessThan(15);
    expect(capped(line, 20).length).toBeLessThanOrEqual(20);
    expect(capped(SQUARE, 20)).toBe(SQUARE); // already few enough: untouched
  });
});
