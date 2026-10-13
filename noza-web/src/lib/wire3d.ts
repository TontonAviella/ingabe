// Line drawings in three dimensions for the capability cards: each model is a set of segments that the card
// turns slowly and projects into an SVG path. "accent" segments are what the drone found (drawn in caramel),
// "dashed" ones are what it is not sure about.

export type V = [number, number, number];
export type Seg = [V, V];

export interface Model {
  base: Seg[];
  accent: Seg[];
  dashed: Seg[];
  caption?: string;
}

export type ModelName = "crops" | "count" | "gaps" | "tower" | "vegetation" | "corridor";

// ---------- helpers ----------

const circle = (cx: number, cy: number, z: number, r: number, n = 14): Seg[] => {
  const s: Seg[] = [];
  for (let k = 0; k < n; k++) {
    const a = (k / n) * Math.PI * 2;
    const b = ((k + 1) / n) * Math.PI * 2;
    s.push([
      [cx + Math.cos(a) * r, cy + Math.sin(a) * r, z],
      [cx + Math.cos(b) * r, cy + Math.sin(b) * r, z],
    ]);
  }
  return s;
};

const box = (x0: number, y0: number, z0: number, x1: number, y1: number, z1: number): Seg[] => {
  const c = (i: number): V => [i & 1 ? x1 : x0, i & 2 ? y1 : y0, i & 4 ? z1 : z0];
  const pairs = [
    [0, 1],
    [1, 3],
    [3, 2],
    [2, 0],
    [4, 5],
    [5, 7],
    [7, 6],
    [6, 4],
    [0, 4],
    [1, 5],
    [2, 6],
    [3, 7],
  ];
  return pairs.map(([a, b]) => [c(a), c(b)]);
};

const ground = (x: number, y: number) => 3 * Math.sin(x * 0.05) * Math.cos(y * 0.04);

function plant(x: number, y: number, h: number, z = ground(x, y)): Seg[] {
  const top: V = [x, y, z + h];
  const s: Seg[] = [[[x, y, z], top]];
  for (const [dx, dy, k] of [
    [1, 0.3, 0.45],
    [-1, -0.2, 0.6],
    [0.2, 1, 0.75],
    [-0.3, -1, 0.9],
  ]) {
    const at: V = [x, y, z + h * k];
    s.push([at, [x + dx * h * 0.35, y + dy * h * 0.35, z + h * k + h * 0.12]]);
  }
  return s;
}

function terrain(x0: number, y0: number, x1: number, y1: number, step: number): Seg[] {
  const s: Seg[] = [];
  for (let x = x0; x <= x1 + 0.01; x += step)
    for (let y = y0; y < y1 - 0.01; y += step / 2) s.push([[x, y, ground(x, y)], [x, y + step / 2, ground(x, y + step / 2)]]);
  for (let y = y0; y <= y1 + 0.01; y += step)
    for (let x = x0; x < x1 - 0.01; x += step / 2) s.push([[x, y, ground(x, y)], [x + step / 2, y, ground(x + step / 2, y)]]);
  return s;
}

function outline(x0: number, y0: number, x1: number, y1: number, lift = 0.4): Seg[] {
  const pts: V[] = [];
  const n = 8;
  const add = (x: number, y: number) => pts.push([x, y, ground(x, y) + lift]);
  for (let k = 0; k < n; k++) add(x0 + ((x1 - x0) * k) / n, y0);
  for (let k = 0; k < n; k++) add(x1, y0 + ((y1 - y0) * k) / n);
  for (let k = 0; k < n; k++) add(x1 - ((x1 - x0) * k) / n, y1);
  for (let k = 0; k < n; k++) add(x0, y1 - ((y1 - y0) * k) / n);
  return pts.map((p, k) => [p, pts[(k + 1) % pts.length]]);
}

function lattice(x: number, y: number, H: number, w: number, armsAt: [number, number][]): { base: Seg[]; insulators: Seg[]; tips: V[] } {
  const base: Seg[] = [];
  const insulators: Seg[] = [];
  const tips: V[] = [];
  const half = (z: number) => w * (1 - (0.72 * Math.min(z, H * 0.7)) / (H * 0.7));
  const levels: number[] = [];
  for (let z = 0; z < H * 0.95; z += H / 9) levels.push(z);
  levels.push(H * 0.95);
  const c: [number, number][] = [
    [1, 1],
    [1, -1],
    [-1, -1],
    [-1, 1],
  ];
  for (let l = 0; l < levels.length - 1; l++) {
    const z0 = levels[l];
    const z1 = levels[l + 1];
    for (let k = 0; k < 4; k++) {
      const [a, b] = c[k];
      const [a2, b2] = c[(k + 1) % 4];
      const p = (u: number, v: number, z: number, h: number): V => [x + u * h, y + v * h, z];
      base.push([p(a, b, z0, half(z0)), p(a, b, z1, half(z1))]);
      base.push([p(a, b, z0, half(z0)), p(a2, b2, z1, half(z1))]);
      base.push([p(a2, b2, z0, half(z0)), p(a, b, z1, half(z1))]);
      base.push([p(a, b, z1, half(z1)), p(a2, b2, z1, half(z1))]);
    }
  }
  base.push([[x + half(H * 0.95), y, H * 0.95], [x, y, H * 1.08]]);
  base.push([[x - half(H * 0.95), y, H * 0.95], [x, y, H * 1.08]]);
  for (const [z, reach] of armsAt) {
    const h = half(z);
    for (const s of [-1, 1]) {
      base.push([[x + h, y + s * h, z], [x, y + s * reach, z]]);
      base.push([[x - h, y + s * h, z], [x, y + s * reach, z]]);
      base.push([[x + h, y + s * h, z - 3], [x, y + s * reach, z]]);
      const tip: V = [x, y + s * reach, z];
      const low: V = [x, y + s * reach, z - 5];
      insulators.push([tip, low]);
      tips.push(low);
    }
  }
  return { base, insulators, tips };
}

function sagLine(a: V, b: V, depth: number, n = 16): Seg[] {
  const s: Seg[] = [];
  const at = (t: number): V => [a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t, a[2] + (b[2] - a[2]) * t - depth * 4 * t * (1 - t)];
  for (let k = 0; k < n; k++) s.push([at(k / n), at((k + 1) / n)]);
  return s;
}

function tree(x: number, y: number, h: number, r: number): Seg[] {
  const z = ground(x, y);
  return [
    [[x, y, z], [x, y, z + h * 0.45]],
    ...circle(x, y, z + h * 0.5, r * 0.8, 10),
    ...circle(x, y, z + h * 0.75, r, 10),
    ...circle(x, y, z + h, r * 0.45, 8),
    [[x - r, y, z + h * 0.75], [x, y, z + h * 1.08]],
    [[x + r, y, z + h * 0.75], [x, y, z + h * 1.08]],
    [[x, y - r, z + h * 0.75], [x, y, z + h * 1.08]],
    [[x, y + r, z + h * 0.75], [x, y, z + h * 1.08]],
  ];
}

function house(x: number, y: number, s: number): Seg[] {
  const z = ground(x, y);
  const w = 7 * s;
  const d = 5 * s;
  const h = 5 * s;
  return [
    ...box(x - w, y - d, z, x + w, y + d, z + h),
    [[x - w, y - d, z + h], [x - w, y, z + h + 3.5 * s]],
    [[x - w, y + d, z + h], [x - w, y, z + h + 3.5 * s]],
    [[x + w, y - d, z + h], [x + w, y, z + h + 3.5 * s]],
    [[x + w, y + d, z + h], [x + w, y, z + h + 3.5 * s]],
    [[x - w, y, z + h + 3.5 * s], [x + w, y, z + h + 3.5 * s]],
  ];
}

// ---------- models ----------

export function model(name: ModelName, t: number): Model {
  const base: Seg[] = [];
  const accent: Seg[] = [];
  const dashed: Seg[] = [];
  let caption: string | undefined;

  if (name === "crops") {
    base.push(...terrain(-60, -60, 60, 60, 20));
    const plots: [number, number, number, number, number][] = [
      [-60, -60, -10, -10, 7],
      [-10, -60, 60, -20, 9],
      [-60, -10, 0, 60, 5],
      [0, -20, 60, 60, 8],
    ];
    plots.forEach(([x0, y0, x1, y1, h], k) => {
      for (let x = x0 + 6; x < x1 - 3; x += 9) for (let y = y0 + 6; y < y1 - 3; y += 9) base.push(...plant(x, y, h));
      if (k === 1) accent.push(...outline(x0 + 1, y0 + 1, x1 - 1, y1 - 1));
      else if (k === 2) dashed.push(...outline(x0 + 1, y0 + 1, x1 - 1, y1 - 1));
      else base.push(...outline(x0 + 1, y0 + 1, x1 - 1, y1 - 1, 0.2));
    });
  }

  if (name === "count") {
    base.push(...outline(-50, -50, 50, 50, 0.2));
    const pts: [number, number][] = [];
    for (let x = -42; x <= 42; x += 12) for (let y = -42; y <= 42; y += 12) pts.push([x + ((y * 7) % 3), y + ((x * 5) % 3)]);
    const shown = Math.floor(((t % 9) / 7) * pts.length);
    pts.forEach(([x, y], k) => {
      base.push(...plant(x, y, 9));
      if (k < shown) accent.push(...circle(x, y, ground(x, y) + 0.5, 4.2, 10));
    });
    caption = `${Math.min(shown, pts.length)} plants`;
  }

  if (name === "gaps") {
    base.push(...outline(-60, -50, 60, 50, 0.2));
    const holes: [number, number, number][] = [
      [-25, -12, 16],
      [28, 22, 12],
    ];
    for (let x = -54; x <= 54; x += 8)
      for (let y = -44; y <= 44; y += 11) {
        if (holes.some(([hx, hy, r]) => Math.hypot(x - hx, y - hy) < r)) continue;
        base.push(...plant(x, y, 7));
      }
    const pulse = 1 + 0.06 * Math.sin(t * 2.4);
    for (const [hx, hy, r] of holes) accent.push(...circle(hx, hy, ground(hx, hy) + 0.4, r * pulse, 22));
  }

  if (name === "tower") {
    const tw = lattice(0, 0, 78, 13, [
      [58, 26],
      [68, 19],
    ]);
    base.push(...tw.base);
    base.push(...box(-16, -16, 0, 16, 16, 0.01).slice(0, 4));
    accent.push(tw.insulators[1], ...box(-3.5, 21.5, 51, 3.5, 30.5, 60));
    base.push(tw.insulators[0], ...tw.insulators.slice(2));
    for (const tip of tw.tips) base.push([tip, [tip[0] + 40, tip[1], tip[2] - 6]], [tip, [tip[0] - 40, tip[1], tip[2] - 6]]);
  }

  if (name === "vegetation" || name === "corridor") {
    const a = lattice(-55, 0, 62, 9, [[46, 19]]);
    const b = lattice(55, 0, 62, 9, [[46, 19]]);
    base.push(...a.base, ...b.base, ...a.insulators, ...b.insulators);
    for (let k = 0; k < a.tips.length; k++) base.push(...sagLine(a.tips[k], b.tips[k], 12));
    if (name === "vegetation") {
      base.push(...terrain(-60, -40, 60, 40, 20));
      base.push(...tree(-20, -32, 18, 6), ...tree(30, 30, 16, 5.5), ...tree(-35, 28, 14, 5));
      const tall = tree(12, -17, 33, 7);
      accent.push(...tall);
      // Clearance from the treetop to the nearest wire.
      const top: V = [12, -17, ground(12, -17) + 35];
      const wireZ = a.tips[0][2] - 12 * 4 * ((12 + 55) / 110) * (1 - (12 + 55) / 110);
      const gap: V = [12, -19, wireZ];
      dashed.push([top, gap]);
    } else {
      base.push(...terrain(-60, -45, 60, 45, 30));
      // Right of way: a strip either side of the line that should stay clear of houses.
      for (const y of [-26, 26]) for (let x = -60; x < 60; x += 6) dashed.push([[x, y, ground(x, y) + 0.3], [x + 3, y, ground(x + 3, y) + 0.3]]);
      base.push(...house(-30, 38, 0.9), ...house(28, -40, 0.8));
      accent.push(...house(18, 18, 0.9));
    }
  }

  return { base, accent, dashed, caption };
}

// ---------- projection ----------

export function toPath(segs: Seg[], angle: number, scale: number, cx: number, cy: number, tilt = 0.52): string {
  const ca = Math.cos(angle);
  const sa = Math.sin(angle);
  const st = Math.sin(tilt);
  const ct = Math.cos(tilt);
  let d = "";
  const p = ([x, y, z]: V) => {
    const rx = x * ca - y * sa;
    const ry = x * sa + y * ca;
    return `${(cx + rx * scale).toFixed(1)},${(cy + (ry * st - z * ct) * scale).toFixed(1)}`;
  };
  for (const [a, b] of segs) d += `M${p(a)}L${p(b)}`;
  return d;
}
