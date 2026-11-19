// Places on the screen and the two questions asked of an outline there: the nearest place on its line, and
// whether a spot lies inside it. Used by moving an outline by its corners (lib/cornerEdit) and by the taps that
// must fall inside a field (MyCrop, CountCheck). All in screen pixels.

export type Pixel = { x: number; y: number };

const distance = (a: Pixel, b: Pixel) => Math.hypot(a.x - b.x, a.y - b.y);

/** The nearest place on a closed ring: the side it lies on (from corner `index` to the next), how far along that
 * side (0..1), the place itself and how far away it is. */
export function nearestOnRing(ring: Pixel[], p: Pixel): { index: number; along: number; point: Pixel; far: number } {
  let best = { index: 0, along: 0, point: ring[0], far: Number.POSITIVE_INFINITY };
  ring.forEach((a, index) => {
    const b = ring[(index + 1) % ring.length];
    const dx = b.x - a.x;
    const dy = b.y - a.y;
    const length2 = dx * dx + dy * dy;
    const along = length2 ? Math.min(1, Math.max(0, ((p.x - a.x) * dx + (p.y - a.y) * dy) / length2)) : 0;
    const point = { x: a.x + along * dx, y: a.y + along * dy };
    const far = distance(point, p);
    if (far < best.far) best = { index, along, point, far };
  });
  return best;
}

/** Whether `p` lies inside the closed ring (even-odd rule). */
export function inside(ring: Pixel[], p: Pixel): boolean {
  let within = false;
  for (let i = 0, j = ring.length - 1; i < ring.length; j = i++) {
    const a = ring[i];
    const b = ring[j];
    if (a.y > p.y !== b.y > p.y && p.x < ((b.x - a.x) * (p.y - a.y)) / (b.y - a.y) + a.x) within = !within;
  }
  return within;
}
