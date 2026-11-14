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
    // smooth: no corner of the pulled line jumps more than the pull's own slope allows
    const steps = out.map((p, i) => Math.hypot(p.x - out[(i + 1) % out.length].x, p.y - out[(i + 1) % out.length].y));
    expect(Math.max(...steps)).toBeLessThan(12);
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
    expect(near(huge, 0, 50, 3)).toBe(true); // the middle of the far side stayed
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
