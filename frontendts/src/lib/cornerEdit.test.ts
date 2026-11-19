import { describe, expect, it } from 'vitest';
import { corners, fewCorners, grabbed, merged } from '@/lib/cornerEdit';
import type { Pixel } from '@/lib/traceEdge';

// A 100 by 100 square whose sides are made of many small steps that wobble by `wobble`, as a model draws it.
function ragged(wobble: number): Pixel[] {
  const out: Pixel[] = [];
  const side = (from: Pixel, to: Pixel, bumpX: number, bumpY: number) => {
    for (let k = 0; k < 20; k++) {
      const t = k / 20;
      const w = k === 0 ? 0 : k % 2 ? wobble : -wobble;
      out.push({ x: from.x + (to.x - from.x) * t + bumpX * w, y: from.y + (to.y - from.y) * t + bumpY * w });
    }
  };
  side({ x: 0, y: 0 }, { x: 100, y: 0 }, 0, 1);
  side({ x: 100, y: 0 }, { x: 100, y: 100 }, 1, 0);
  side({ x: 100, y: 100 }, { x: 0, y: 100 }, 0, 1);
  side({ x: 0, y: 100 }, { x: 0, y: 0 }, 1, 0);
  return out;
}

describe('moving an outline by its corners', () => {
  it('finds the four corners of a ragged square, as the same points', () => {
    const ring = ragged(0.8);
    const found = corners(ring, 2);
    expect(found).toHaveLength(4);
    expect(found.map((p) => [p.x, p.y])).toEqual([
      [0, 0],
      [100, 0],
      [100, 100],
      [0, 100],
    ]);
    for (const p of found) expect(ring).toContain(p); // the ring's own points, not copies
  });

  it('keeps a real bend and leaves a small ring alone', () => {
    const bent = [...ragged(0.5).slice(0, 10), { x: 50, y: -30 }, ...ragged(0.5).slice(10)];
    expect(corners(bent, 2).some((p) => p.x === 50 && p.y === -30)).toBe(true);
    const triangle = [
      { x: 0, y: 0 },
      { x: 10, y: 0 },
      { x: 0, y: 10 },
    ];
    expect(corners(triangle, 100)).toBe(triangle);
  });

  it('grows the tolerance until few enough corners are left', () => {
    const ring = ragged(3);
    expect(corners(ring, 0.5).length).toBeGreaterThan(12);
    const few = fewCorners(ring, 0.5, 12);
    expect(few.length).toBeLessThanOrEqual(12);
    expect(few.length).toBeGreaterThanOrEqual(4);
  });

  const square: Pixel[] = [
    { x: 0, y: 0 },
    { x: 100, y: 0 },
    { x: 100, y: 100 },
    { x: 0, y: 100 },
  ];

  it('a press near a corner takes that corner, and adds nothing', () => {
    const held = grabbed(square, { x: 108, y: 95 }, 28);
    expect(held).toEqual({ ring: square, index: 2, added: false });
  });

  it('a press on a side away from the corners makes a corner there', () => {
    const held = grabbed(square, { x: 50, y: 10 }, 28);
    expect(held?.added).toBe(true);
    expect(held?.index).toBe(1);
    expect(held?.ring).toHaveLength(5);
    expect(held?.ring[1]).toEqual({ x: 50, y: 0 });
    expect(held?.ring[0]).toBe(square[0]); // the other corners are the same points
    expect(held?.ring[2]).toBe(square[1]);
  });

  it('a press away from the line takes hold of nothing', () => {
    expect(grabbed(square, { x: 50, y: 50 }, 28)).toBeNull();
    expect(grabbed(square.slice(0, 2), { x: 0, y: 0 }, 28)).toBeNull();
  });

  it('a corner let go on its neighbour is removed, but never the third last', () => {
    const five = ['a', 'b', 'c', 'd', 'e'];
    const places = [...square.slice(0, 2), { x: 104, y: 3 }, ...square.slice(2)];
    expect(merged(five, places, 2, 12)).toEqual(['a', 'b', 'd', 'e']); // on the corner before it
    expect(merged(five, places, 4, 12)).toBe(five); // far from both neighbours
    const three = ['a', 'b', 'c'];
    expect(merged(three, [square[0], { x: 1, y: 1 }, square[2]], 1, 12)).toBe(three);
  });
});
