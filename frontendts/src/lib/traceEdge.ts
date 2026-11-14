// Reshaping an outline by drawing along the true edge: the reader starts on the outline, draws, and ends on it
// (or near it); the drawn path takes the place of the stretch of outline between its two ends. All in screen
// pixels: the map does not move while a path is drawn.

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

type Place = { index: number; along: number };

/** The corners passed going forward along the ring from one place to another, and the length of that way. */
function forward(ring: Pixel[], from: Place & { point: Pixel }, to: Place & { point: Pixel }): { corners: Pixel[]; length: number } {
  const n = ring.length;
  const sameSideAhead = from.index === to.index && to.along >= from.along;
  const steps = sameSideAhead ? 0 : (to.index - from.index + n) % n || n;
  const corners: Pixel[] = [];
  for (let k = 1; k <= steps; k++) corners.push(ring[(from.index + k) % n]);
  const way = [from.point, ...corners, to.point];
  return { corners, length: way.slice(1).reduce((sum, p, i) => sum + distance(way[i], p), 0) };
}

/** `points` without those closer than `gap` to the one kept before them (the first and last always stay). */
export function thinned(points: Pixel[], gap: number): Pixel[] {
  const out: Pixel[] = [];
  points.forEach((p, i) => {
    if (i === 0 || i === points.length - 1 || distance(out[out.length - 1], p) >= gap) out.push(p);
  });
  return out;
}

/** The ring with at most `most` corners: corners are dropped evenly, with a growing gap, until it fits. */
export function capped(ring: Pixel[], most: number): Pixel[] {
  let out = ring;
  for (let gap = 3; out.length > most && gap < 200; gap += 2) out = thinned(ring, gap);
  return out;
}

/**
 * The ring reshaped by a drawn path. The path's two ends are joined to the nearest places on the ring, and the path
 * takes the place of one of the two stretches of ring between them: the one that leaves `keep` (the spot the reader
 * first tapped) inside the outline, and when both do, the shorter one, since a reader corrects a stretch of the edge,
 * not most of the field. null when the path is too short to say anything or neither way keeps `keep` inside.
 */
export function reshaped(ring: Pixel[], path: Pixel[], keep: Pixel): Pixel[] | null {
  if (ring.length < 3 || path.length < 2) return null;
  const start = nearestOnRing(ring, path[0]);
  const end = nearestOnRing(ring, path[path.length - 1]);
  const drawn = [start.point, ...path, end.point];
  const ahead = forward(ring, start, end); // the stretch from the start of the path forward to its end
  const behind = forward(ring, end, start); // the rest of the ring
  const choices = [
    { ring: [...drawn, ...behind.corners], replaced: ahead.length },
    { ring: [...[...drawn].reverse(), ...ahead.corners], replaced: behind.length },
  ]
    .filter((c) => c.ring.length >= 3 && inside(c.ring, keep))
    .sort((a, b) => a.replaced - b.replaced);
  return choices.length ? choices[0].ring : null;
}
