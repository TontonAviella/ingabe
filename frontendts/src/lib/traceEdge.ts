// Moving an outline by pulling it: the reader presses on the line, drags, and lets go anywhere. The stretch of line
// around the press follows, most at the press and less further along, so the line bends smoothly and stays one line.
// All in screen pixels: the map does not move while the line is pulled.

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

const STEP = 6; // the pulled stretch gets a corner at least this often, so it can bend where there was a straight side
const MIN_REACH = 40; // a small pull still moves this much line each side of the press
const REACH_PER_PULL = 2; // a longer pull takes more line with it, so the bend stays gentle
const MOST_OF_RING = 0.45; // the reach each way is never more than this share of the outline's length
const STEEPEST = 0.6; // a pull longer than this share of its reach would fold the line over itself: it is shortened

/**
 * The ring after a press at `grab` (on or near the line) was dragged to `to`: the nearest place on the line moves by
 * as much as the finger did. The line within reach of that place (measured along the line, both ways) follows: fully
 * at the place itself, falling smoothly to nothing at the end of the reach, which grows with the length of the pull
 * and never takes most of the ring. Only that stretch gets new corners; every corner outside it stays exactly where
 * it was, however many pulls are made. null when the ring is no outline, or the pull would leave `keep` (the spot the
 * reader first tapped) outside it.
 */
export function pulled(ring: Pixel[], grab: Pixel, to: Pixel, keep: Pixel): Pixel[] | null {
  if (ring.length < 3) return null;
  const start = nearestOnRing(ring, grab);
  const n = ring.length;
  // once round from the pressed place: it, then every corner in order, ending on the corner before it
  const corners = [start.point, ...Array.from({ length: n }, (_, k) => ring[(start.index + 1 + k) % n])];
  const around = corners.reduce((sum, p, i) => sum + distance(p, corners[(i + 1) % corners.length]), 0);
  let move = { x: to.x - grab.x, y: to.y - grab.y }; // by how far the finger went: the line does not jump to it
  const reach = Math.min(MOST_OF_RING * around, Math.max(MIN_REACH, REACH_PER_PULL * Math.hypot(move.x, move.y)));
  if (!reach) return null;
  const long = Math.hypot(move.x, move.y) / (STEEPEST * reach);
  if (long > 1) move = { x: move.x / long, y: move.y / long };
  const moved = (p: Pixel, along: number) => {
    const away = Math.min(along, around - along);
    if (away >= reach) return p;
    const share = 0.5 * (1 + Math.cos((Math.PI * away) / reach));
    return { x: p.x + move.x * share, y: p.y + move.y * share };
  };
  const out: Pixel[] = [];
  let along = 0;
  corners.forEach((from, i) => {
    const end = corners[(i + 1) % corners.length];
    const length = distance(from, end);
    out.push(moved(from, along));
    // a side that lies partly within reach bends: it gets corners along it; a side out of reach stays one side
    if (along < reach || around - (along + length) < reach) {
      const pieces = Math.ceil(length / STEP);
      for (let k = 1; k < pieces; k++) {
        const t = k / pieces;
        const here = along + length * t;
        // only the part of the side that bends; one corner just past the reach pins the straight rest of it
        if (Math.min(here, around - here) >= reach + STEP) continue;
        out.push(moved({ x: from.x + (end.x - from.x) * t, y: from.y + (end.y - from.y) * t }, here));
      }
    }
    along += length;
  });
  return inside(out, keep) ? out : null;
}
