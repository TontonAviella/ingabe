// Moving an outline by its corners: the outline is shown with a dot on each corner; the reader drags a dot to the
// true corner and the two sides that meet there stay straight. Pressing on a side, away from a dot, makes a new
// corner there and drags it. The way polygons are edited in most map tools, so there is nothing to learn.
//
// It replaces pulling a stretch of line like rubber (2026-11-14 to 2026-11-19): a straight side bulged where it
// was pulled, so following a straight field edge took dozens of small pulls (34 for one field on the live site).

import { nearestOnRing, type Pixel } from './traceEdge';

const distance = (a: Pixel, b: Pixel) => Math.hypot(a.x - b.x, a.y - b.y);

function offLine(p: Pixel, a: Pixel, b: Pixel): number {
  const dx = b.x - a.x;
  const dy = b.y - a.y;
  const length2 = dx * dx + dy * dy;
  const t = length2 ? Math.min(1, Math.max(0, ((p.x - a.x) * dx + (p.y - a.y) * dy) / length2)) : 0;
  return distance(p, { x: a.x + t * dx, y: a.y + t * dy });
}

/** The corners of an open line worth keeping: the ends, and every point further than `tolerance` from the line
 * through the corners kept around it (Douglas-Peucker). Returns their places in `line`, in order. */
function kept(line: Pixel[], from: number, to: number, tolerance: number): number[] {
  const out = [from];
  const todo: [number, number][] = [[from, to]];
  const keep = new Set<number>([from, to]);
  while (todo.length) {
    const [a, b] = todo.pop() as [number, number];
    let worst = -1;
    let far = tolerance;
    for (let i = a + 1; i < b; i++) {
      const d = offLine(line[i], line[a], line[b]);
      if (d > far) {
        far = d;
        worst = i;
      }
    }
    if (worst < 0) continue;
    keep.add(worst);
    todo.push([a, worst], [worst, b]);
  }
  for (let i = from + 1; i <= to; i++) if (keep.has(i)) out.push(i);
  return out;
}

/** The ring with only its real corners: points that sit within `tolerance` of a straight side are dropped. The
 * corners kept are the ring's own points (the same objects), in order. A ring of 3 corners or fewer is returned
 * as it is. */
export function corners<T extends Pixel>(ring: T[], tolerance: number): T[] {
  if (ring.length <= 3) return ring;
  // a closed line has no ends: it is cut at its first point and at the point furthest from it
  let far = 0;
  ring.forEach((p, i) => {
    if (distance(p, ring[0]) > distance(ring[far], ring[0])) far = i;
  });
  if (far === 0) return ring;
  const round = [...ring, ring[0]];
  const first = kept(round, 0, far, tolerance);
  const second = kept(round, far, ring.length, tolerance);
  const places = [...first, ...second.slice(1, -1)];
  return places.length >= 3 ? places.map((i) => ring[i]) : ring;
}

/** `corners`, with the tolerance grown until no more than `most` are left: an outline to drag by hand has few. */
export function fewCorners<T extends Pixel>(ring: T[], tolerance: number, most: number): T[] {
  let out = corners(ring, tolerance);
  for (let t = tolerance * 1.5; out.length > most && t < tolerance * 1000; t *= 1.5) out = corners(ring, t);
  return out;
}

/**
 * What a press takes hold of: the corner nearest to it, or, when the press is on a side away from any corner, a
 * new corner made there. Returns the ring to drag (with the new corner in it, when one was made), the place of the
 * held corner in it, and whether it is new. null when the press is further than `within` from the line: the map
 * pans as usual.
 */
export function grabbed(ring: Pixel[], press: Pixel, within: number): { ring: Pixel[]; index: number; added: boolean } | null {
  if (ring.length < 3) return null;
  let index = 0;
  ring.forEach((p, i) => {
    if (distance(p, press) < distance(ring[index], press)) index = i;
  });
  if (distance(ring[index], press) <= within) return { ring, index, added: false };
  const on = nearestOnRing(ring, press);
  if (on.far > within) return null;
  const next = [...ring.slice(0, on.index + 1), on.point, ...ring.slice(on.index + 1)];
  return { ring: next, index: on.index + 1, added: true };
}

/** The ring without corner `index` when it was let go on top of a neighbour (within `within`): dragging a corner
 * onto the next one removes it. Otherwise the ring as it is. Never leaves fewer than 3 corners. */
export function merged<T>(ring: T[], places: Pixel[], index: number, within: number): T[] {
  if (ring.length <= 3) return ring;
  const n = ring.length;
  const near = (other: number) => distance(places[index], places[other]) <= within;
  return near((index + 1) % n) || near((index + n - 1) % n) ? ring.filter((_, i) => i !== index) : ring;
}
