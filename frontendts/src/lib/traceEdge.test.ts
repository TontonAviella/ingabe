import { describe, expect, it } from 'vitest';
import { inside, nearestOnRing } from '@/lib/traceEdge';

// a 100 px square, corners clockwise on screen from the top left
const SQUARE = [
  { x: 0, y: 0 },
  { x: 100, y: 0 },
  { x: 100, y: 100 },
  { x: 0, y: 100 },
];

describe('an outline on the screen', () => {
  it('finds the nearest place on the ring', () => {
    const at = nearestOnRing(SQUARE, { x: 108, y: 40 });
    expect(at).toMatchObject({ index: 1, point: { x: 100, y: 40 }, far: 8 });
    expect(at.along).toBeCloseTo(0.4);
  });

  it('says whether a spot is inside', () => {
    expect(inside(SQUARE, { x: 50, y: 50 })).toBe(true);
    expect(inside(SQUARE, { x: 150, y: 50 })).toBe(false);
  });
});
