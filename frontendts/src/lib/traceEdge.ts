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

const STEP = 6; // a pulled line gets a corner at least this often, so it can bend where there was a straight side
const MIN_REACH = 40; // a small pull still moves this much line each side of the press
const REACH_PER_PULL = 2; // a longer pull takes more line with it, so the bend stays gentle

/** The ring with a corner every STEP pixels at most, the first of them the place nearest `at` on the ring. */
function fine(ring: Pixel[], at: Pixel): Pixel[] {
  const start = nearestOnRing(ring, at);
  const out: Pixel[] = [start.point];
  const n = ring.length;
  // once round the ring from the pressed place: the rest of its side, every other side, then the side's first part
  const ends = [...Array.from({ length: n }, (_, k) => ring[(start.index + 1 + k) % n]), start.point];
  for (const end of ends) {
    const from = out[out.length - 1];
    const pieces = Math.max(1, Math.ceil(distance(from, end) / STEP));
    for (let k = 1; k <= pieces; k++)
      out.push({ x: from.x + ((end.x - from.x) * k) / pieces, y: from.y + ((end.y - from.y) * k) / pieces });
  }
  out.pop(); // the pressed place again
  return out;
}

/**
 * The ring after a press at `grab` (on or near the line) was dragged to `to`: the nearest place on the line moves by
 * as much as the finger did. The line within reach of that place (measured along the
 * line, both ways) follows: fully at the place itself, falling smoothly to nothing at the end of the reach, which
 * grows with the length of the pull and never takes more than most of the ring. null when the ring is no outline, or
 * the pull would leave `keep` (the spot the reader first tapped) outside it.
 */
export function pulled(ring: Pixel[], grab: Pixel, to: Pixel, keep: Pixel): Pixel[] | null {
  if (ring.length < 3) return null;
  const line = fine(ring, grab);
  const move = { x: to.x - grab.x, y: to.y - grab.y }; // by how far the finger went: the line does not jump to it
  const lengths = line.map((p, i) => distance(p, line[(i + 1) % line.length]));
  const around = lengths.reduce((sum, l) => sum + l, 0);
  const reach = Math.min(0.45 * around, Math.max(MIN_REACH, REACH_PER_PULL * Math.hypot(move.x, move.y)));
  let along = 0;
  const out = line.map((p, i) => {
    const away = Math.min(along, around - along);
    along += lengths[i];
    if (away >= reach) return p;
    const share = 0.5 * (1 + Math.cos((Math.PI * away) / reach));
    return { x: p.x + move.x * share, y: p.y + move.y * share };
  });
  return inside(out, keep) ? out : null;
}
