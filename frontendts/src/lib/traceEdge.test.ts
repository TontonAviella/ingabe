import { describe, expect, it } from 'vitest';
import { capped, inside, nearestOnRing, pulled, thinned } from '@/lib/traceEdge';

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
const near = (ring: { x: number; y: number }[], x: number, y: number, within = 0.5) =>
  ring.some((p) => Math.hypot(p.x - x, p.y - y) <= within);

describe('moving an outline by pulling its line', () => {
  it('finds the nearest place on the ring', () => {
    const at = nearestOnRing(SQUARE, { x: 108, y: 40 });
    expect(at).toMatchObject({ index: 1, point: { x: 100, y: 40 }, far: 8 });
    expect(at.along).toBeCloseTo(0.4);
    expect(inside(SQUARE, MIDDLE)).toBe(true);
    expect(inside(SQUARE, { x: 150, y: 50 })).toBe(false);
  });

  it('takes the pressed place to where it was let go, bends the line around it smoothly, and leaves the rest alone', () => {
    // pressed a little off the middle of the right side, pulled 30 px outwards
    const out = pulled(SQUARE, { x: 103, y: 50 }, { x: 133, y: 50 }, MIDDLE) as { x: number; y: number }[];
    expect(near(out, 130, 50)).toBe(true); // the pressed place on the line (100, 50) moved by the pull
    expect(area(out)).toBeGreaterThan(10_000);
    // the far side did not move, and the corners furthest from the pull are where they were
    expect(near(out, 0, 0) && near(out, 0, 100)).toBe(true);
    expect(out.every((p) => p.x >= -0.001 && p.x <= 130.001)).toBe(true);
    // smooth: along the stretch that moved, no corner jumps
    const bent = out.filter((p) => p.x > 100.001).sort((a, b) => a.y - b.y); // down the side, whichever end the ring starts at
    expect(bent.length).toBeGreaterThan(8);
    expect(Math.max(...bent.slice(1).map((p, i) => Math.hypot(p.x - bent[i].x, p.y - bent[i].y)))).toBeLessThan(12);
    // pulled inwards the field gets smaller, by the same press
    const inwards = pulled(SQUARE, { x: 100, y: 50 }, { x: 80, y: 50 }, MIDDLE) as { x: number; y: number }[];
    expect(area(inwards)).toBeLessThan(10_000);
    expect(near(inwards, 80, 50)).toBe(true);
  });

  it('a longer pull takes more of the line with it, and a pull never takes the whole field', () => {
    const moved = (ring: { x: number; y: number }[]) => ring.filter((p) => p.x > 100.01).length * 6; // px of line that left the side
    const small = pulled(SQUARE, { x: 100, y: 50 }, { x: 105, y: 50 }, MIDDLE) as { x: number; y: number }[];
    const large = pulled(SQUARE, { x: 100, y: 50 }, { x: 140, y: 50 }, MIDDLE) as { x: number; y: number }[];
    expect(moved(large)).toBeGreaterThan(moved(small));
    const huge = pulled(SQUARE, { x: 100, y: 50 }, { x: 900, y: 50 }, MIDDLE) as { x: number; y: number }[];
    const west = Math.min(...huge.map((p) => p.x));
    expect(west).toBeGreaterThanOrEqual(-0.001); // the far side did not come along
    expect(west).toBeLessThan(3);
  });

  it('leaves every corner out of reach exactly where it was, pull after pull, and never folds the line', () => {
    // a field 3000 px a side with a notch: far more line than a pull reaches
    const big = [
      { x: 0, y: 0 },
      { x: 3000, y: 0 },
      { x: 3000, y: 3000 },
      { x: 1500, y: 3000 },
      { x: 1500, y: 2900 },
      { x: 1400, y: 2900 },
      { x: 1400, y: 3000 },
      { x: 0, y: 3000 },
    ];
    let ring = big;
    for (const y of [400, 900, 1400])
      ring = pulled(ring, { x: 3000, y }, { x: 3030, y }, { x: 1500, y: 1500 }) as { x: number; y: number }[];
    for (const corner of big.filter((c) => c.x !== 3000)) expect(ring).toContainEqual(corner); // not a pixel moved
    expect(ring.length).toBeLessThan(200); // new corners only where the line bent
    // a pull far longer than the line can follow is shortened, so the line bends and does not cross itself
    const along = pulled(SQUARE, { x: 100, y: 50 }, { x: 100, y: 5000 }, MIDDLE) as { x: number; y: number }[];
    const crosses = (r: { x: number; y: number }[]) => {
      const turn = (a: { x: number; y: number }, b: { x: number; y: number }, c: { x: number; y: number }) =>
        Math.sign((b.x - a.x) * (c.y - a.y) - (b.y - a.y) * (c.x - a.x));
      for (let i = 0; i < r.length; i++)
        for (let j = i + 2; j < r.length; j++) {
          if (i === 0 && j === r.length - 1) continue;
          const [a, b, c, d] = [r[i], r[(i + 1) % r.length], r[j], r[(j + 1) % r.length]];
          if (turn(a, b, c) * turn(a, b, d) < 0 && turn(c, d, a) * turn(c, d, b) < 0) return true;
        }
      return false;
    };
    expect(crosses(along)).toBe(false);
  });

  it('refuses a pull that would leave the first tapped spot outside the field', () => {
    expect(pulled(SQUARE, { x: 100, y: 50 }, { x: 20, y: 50 }, { x: 90, y: 50 })).toBeNull();
    expect(pulled(SQUARE.slice(0, 2), { x: 0, y: 0 }, { x: 5, y: 5 }, MIDDLE)).toBeNull();
  });

  it('thins a line and caps an outline without losing its ends', () => {
    const line = Array.from({ length: 101 }, (_, i) => ({ x: i, y: 0 }));
    const thin = thinned(line, 10);
    expect(thin[0]).toEqual({ x: 0, y: 0 });
    expect(thin[thin.length - 1]).toEqual({ x: 100, y: 0 });
    expect(thin.length).toBeLessThan(15);
    const round = Array.from({ length: 2000 }, (_, i) => ({
      x: 500 * Math.cos((i / 2000) * 2 * Math.PI),
      y: 500 * Math.sin((i / 2000) * 2 * Math.PI),
    }));
    expect(capped(round, 560).length).toBeLessThanOrEqual(560);
    expect(capped(SQUARE, 560)).toBe(SQUARE);
  });
});
