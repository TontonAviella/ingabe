// Miniature worlds: small isometric dioramas of Rwandan land drawn on a canvas.
// Each world is drawn twice, once as it looks ("solid") and once as the drone reads it ("wire"),
// so the page can sweep from one to the other. Everything is generated from a seed; no images.

export type Preset = "hero" | "hills" | "marsh" | "tea" | "scheme" | "corridor" | "telecom" | "distribution";

type Vec3 = [number, number, number];
type Pt = [number, number];

type CellKind = "maize" | "cassava" | "beans" | "banana" | "bare" | "fallow" | "road" | "water" | "rice" | "tea" | "grass" | "yard";

interface Cell {
  kind: CellKind;
  plot: number;
  rows: 0 | 1;
}

interface Plot {
  id: number;
  i0: number;
  j0: number;
  i1: number;
  j1: number;
  kind: CellKind;
}

type ObjKind = "tree" | "eucalyptus" | "banana" | "house" | "shelter" | "tower" | "mast" | "pole";

interface Obj {
  kind: ObjKind;
  x: number;
  y: number;
  s: number;
  roof?: string;
  rot?: Pt;
}

export interface Detection {
  label: string;
  unsure?: boolean;
  plot?: number;
  obj?: number;
}

interface Line {
  towers: number[]; // indexes into objects
  kind: "transmission" | "distribution";
}

export interface Scene {
  W: number;
  D: number;
  T: number;
  heights: number[][];
  cells: Cell[][];
  plots: Plot[];
  objects: Obj[];
  lines: Line[];
  detections: Detection[];
  corridor?: { a: Pt; b: Pt; z: number };
}

export interface Rendered {
  solid: HTMLCanvasElement;
  wire: HTMLCanvasElement;
  project: (p: Vec3) => Pt;
  boxes: { x: number; y: number; w: number; h: number; label: string; unsure: boolean; poly?: Pt[] }[];
  scene: Scene;
  bounds: { x0: number; x1: number; y0: number; y1: number };
}

// ---------- random ----------

function rng(seed: number) {
  let a = seed >>> 0;
  return () => {
    a = (a + 0x6d2b79f5) >>> 0;
    let t = a;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

// ---------- colours (crops are the only green on the page) ----------

const CROP: Record<CellKind, string> = {
  maize: "#7d8a3c",
  cassava: "#5d7838",
  beans: "#93994c",
  banana: "#4f6a31",
  bare: "#9b6f4a",
  fallow: "#b39567",
  road: "#cdbb9f",
  water: "#7b9493",
  rice: "#9aa84f",
  tea: "#3f5c30",
  grass: "#7f8f4a",
  yard: "#c4ab88",
};

function shade(hex: string, k: number): string {
  const n = parseInt(hex.slice(1), 16);
  const r = Math.min(255, Math.max(0, Math.round(((n >> 16) & 255) * k)));
  const g = Math.min(255, Math.max(0, Math.round(((n >> 8) & 255) * k)));
  const b = Math.min(255, Math.max(0, Math.round((n & 255) * k)));
  return `rgb(${r},${g},${b})`;
}

const INK = "#1A1310";
const COCOA = "#4A3326";
const CARAMEL = "#D9A066";

// ---------- scene building ----------

function heightField(preset: Preset, W: number, D: number, r: () => number): number[][] {
  const p1 = r() * 6;
  const p2 = r() * 6;
  const h: number[][] = [];
  for (let i = 0; i <= W; i++) {
    h.push([]);
    for (let j = 0; j <= D; j++) {
      const x = i / W;
      const y = j / D;
      let z = 0;
      if (preset === "marsh" || preset === "scheme") {
        z = 0.15 * Math.sin(x * 3 + p1) * Math.cos(y * 2 + p2);
      } else if (preset === "hills" || preset === "tea") {
        z = 2.6 * Math.exp(-((x - 0.35) ** 2 + (y - 0.4) ** 2) * 3.2) + 0.5 * Math.sin(x * 5 + p1);
        z = Math.round(z * 3) / 3; // terraces
      } else if (preset === "telecom") {
        z = 2.4 * Math.exp(-((x - 0.5) ** 2 + (y - 0.45) ** 2) * 6);
      } else {
        z = 0.9 * Math.sin(x * 4.2 + p1) * Math.cos(y * 3.1 + p2) + 0.7 * Math.sin((x + y) * 2.4 + p2);
      }
      h[i].push(z);
    }
  }
  return h;
}

function cropMix(preset: Preset): CellKind[] {
  switch (preset) {
    case "marsh":
      return ["rice", "rice", "rice", "water", "rice"];
    case "tea":
      return ["tea", "tea", "tea", "tea", "banana"];
    case "scheme":
      return ["maize", "beans", "maize", "fallow", "beans", "rice"];
    case "telecom":
      return ["grass", "fallow", "grass", "cassava"];
    default:
      return ["maize", "cassava", "beans", "maize", "banana", "bare", "cassava", "fallow"];
  }
}

export function buildScene(preset: Preset, seed = 7): Scene {
  const r = rng(seed);
  const W = preset === "hero" ? 24 : 16;
  const D = preset === "hero" ? 24 : 16;
  const T = preset === "hero" ? 2.2 : 1.6;
  const heights = heightField(preset, W, D, r);
  const cells: Cell[][] = Array.from({ length: W }, () =>
    Array.from({ length: D }, () => ({ kind: "grass" as CellKind, plot: -1, rows: 0 as 0 | 1 })),
  );

  // A road along one side for the places where people live.
  const road = preset !== "marsh" && preset !== "tea";
  const roadJ = Math.floor(D * (preset === "distribution" ? 0.5 : 0.78));
  if (road) {
    for (let i = 0; i < W; i++) {
      const j = roadJ + Math.round(Math.sin(i * 0.35 + seed) * 0.8);
      if (j >= 0 && j < D) cells[i][j].kind = "road";
    }
  }

  // Plots: greedy rectangles of 2-4 by 2-5 cells.
  const plots: Plot[] = [];
  const mix = cropMix(preset);
  const big = preset === "scheme" || preset === "marsh";
  for (let i = 0; i < W; i++) {
    for (let j = 0; j < D; j++) {
      if (cells[i][j].plot !== -1 || cells[i][j].kind === "road") continue;
      const w = big ? 3 + Math.floor(r() * 2) : 2 + Math.floor(r() * 3);
      const d = big ? 4 + Math.floor(r() * 2) : 2 + Math.floor(r() * 4);
      const kind = mix[Math.floor(r() * mix.length)];
      const id = plots.length;
      let i1 = i;
      let j1 = j;
      for (let a = i; a < Math.min(W, i + w); a++) {
        for (let b = j; b < Math.min(D, j + d); b++) {
          if (cells[a][b].plot !== -1 || cells[a][b].kind === "road") continue;
          cells[a][b] = { kind, plot: id, rows: r() > 0.5 ? 1 : 0 };
          i1 = Math.max(i1, a);
          j1 = Math.max(j1, b);
        }
      }
      const rows: 0 | 1 = r() > 0.5 ? 1 : 0;
      for (let a = i; a <= i1; a++) for (let b = j; b <= j1; b++) if (cells[a][b].plot === id) cells[a][b].rows = rows;
      plots.push({ id, i0: i, j0: j, i1, j1, kind });
    }
  }

  // Canals in an irrigation scheme and channels in the marshland.
  if (preset === "scheme" || preset === "marsh") {
    for (let i = 0; i < W; i++) for (const j of [Math.floor(D / 3), Math.floor((2 * D) / 3)]) cells[i][j].kind = "water";
  }

  const objects: Obj[] = [];
  const lines: Line[] = [];
  const detections: Detection[] = [];
  const free = (x: number, y: number) => {
    const c = cells[Math.floor(x)]?.[Math.floor(y)];
    return !!c && c.kind !== "road" && c.kind !== "water";
  };

  // Power: a transmission line crossing the land, or poles along the road.
  let corridor: Scene["corridor"];
  if (preset === "hero" || preset === "corridor") {
    const a: Pt = [0.6, D * 0.62];
    const b: Pt = [W - 0.6, D * 0.12];
    const n = preset === "hero" ? 4 : 3;
    const len = Math.hypot(b[0] - a[0], b[1] - a[1]);
    const u: Pt = [(b[0] - a[0]) / len, (b[1] - a[1]) / len];
    const ids: number[] = [];
    for (let k = 0; k < n; k++) {
      const t = (k + 0.5) / n;
      ids.push(objects.length);
      objects.push({ kind: "tower", x: a[0] + (b[0] - a[0]) * t, y: a[1] + (b[1] - a[1]) * t, s: 1, rot: u });
    }
    lines.push({ towers: ids, kind: "transmission" });
    corridor = { a, b, z: 6.2 };
    // Keep the right-of-way mostly clear, with one tree grown close to the line.
    const t = 0.62;
    const px = a[0] + (b[0] - a[0]) * t - u[1] * 0.9;
    const py = a[1] + (b[1] - a[1]) * t + u[0] * 0.9;
    detections.push({ label: "tree close to line", obj: objects.length });
    objects.push({ kind: "eucalyptus", x: px, y: py, s: 1.25 });
    detections.push({ label: "tower 2 · insulators", obj: ids[1] });
  }
  if (preset === "distribution") {
    const ids: number[] = [];
    for (let i = 1; i < W; i += 3) {
      ids.push(objects.length);
      objects.push({ kind: "pole", x: i + 0.5, y: roadJ - 0.6, s: 1 });
    }
    lines.push({ towers: ids, kind: "distribution" });
  }
  if (preset === "telecom") {
    objects.push({ kind: "mast", x: W * 0.5, y: D * 0.45, s: 1 });
    objects.push({ kind: "shelter", x: W * 0.5 + 1.6, y: D * 0.45 + 0.8, s: 1 });
    objects.push({ kind: "shelter", x: W * 0.5 - 1.4, y: D * 0.45 + 1.6, s: 0.8 });
    for (let i = 0; i < 2; i++) cells[Math.floor(W * 0.5) + i - 1][Math.floor(D * 0.45)].kind = "yard";
  }

  // Houses along the road, trees and bananas around them.
  const nearLine = (x: number, y: number) => {
    if (!corridor) return false;
    const { a, b } = corridor;
    const dx = b[0] - a[0];
    const dy = b[1] - a[1];
    const t = ((x - a[0]) * dx + (y - a[1]) * dy) / (dx * dx + dy * dy);
    const qx = a[0] + dx * t;
    const qy = a[1] + dy * t;
    return Math.hypot(x - qx, y - qy) < 1.6;
  };
  if (road) {
    const houses = preset === "hero" ? 9 : preset === "distribution" ? 7 : 4;
    for (let k = 0; k < houses; k++) {
      const x = 1 + r() * (W - 2.5);
      const y = roadJ + (r() > 0.5 ? 1.3 : -1.0) + r() * 0.4;
      if (!free(x, y) || nearLine(x, y)) continue;
      objects.push({ kind: "house", x, y, s: 0.8 + r() * 0.35, roof: r() > 0.4 ? "#9c9a95" : "#9a5a3e" });
    }
  }
  const trees = preset === "marsh" || preset === "scheme" ? 8 : preset === "tea" ? 10 : 22;
  for (let k = 0; k < trees; k++) {
    const x = 0.5 + r() * (W - 1);
    const y = 0.5 + r() * (D - 1);
    if (!free(x, y) || nearLine(x, y)) continue;
    const c = cells[Math.floor(x)][Math.floor(y)];
    const kind: ObjKind = c.kind === "banana" ? "banana" : r() > 0.55 ? "eucalyptus" : "tree";
    objects.push({ kind, x, y, s: 0.7 + r() * 0.5 });
  }
  for (const p of plots) {
    if (p.kind !== "banana") continue;
    for (let a = p.i0; a <= p.i1; a++)
      for (let b = p.j0; b <= p.j1; b++) if (cells[a][b].plot === p.id && r() > 0.35) objects.push({ kind: "banana", x: a + 0.5, y: b + 0.5, s: 0.8 });
  }

  // What the drone reads on the plots: two looks that agree name the crop, otherwise "not sure".
  if (preset === "hero") {
    const sized = plots
      .filter((p) => ["maize", "cassava", "beans"].includes(p.kind) && (p.i1 - p.i0 + 1) * (p.j1 - p.j0 + 1) >= 6)
      .sort((p, q) => p.i0 - p.j0 - (q.i0 - q.j0));
    const pick = [sized[Math.floor(sized.length * 0.12)], sized[Math.floor(sized.length * 0.45)], sized[Math.floor(sized.length * 0.8)]];
    pick.forEach((p, k) => {
      if (!p) return;
      if (k === 1) detections.push({ label: "not sure · maize or beans?", plot: p.id, unsure: true });
      else detections.push({ label: p.kind, plot: p.id });
    });
  }

  return { W, D, T, heights, cells, plots, objects, lines, detections, corridor };
}

// ---------- drawing ----------

function hAt(scene: Scene, x: number, y: number): number {
  const { heights, W, D } = scene;
  const cx = Math.max(0, Math.min(W - 0.0001, x));
  const cy = Math.max(0, Math.min(D - 0.0001, y));
  const i = Math.floor(cx);
  const j = Math.floor(cy);
  const fx = cx - i;
  const fy = cy - j;
  const a = heights[i][j];
  const b = heights[i + 1][j];
  const c = heights[i][j + 1];
  const d = heights[i + 1][j + 1];
  return a * (1 - fx) * (1 - fy) + b * fx * (1 - fy) + c * (1 - fx) * fy + d * fx * fy;
}

export { hAt };

const ZS = 0.52; // screen units per unit of height

function iso([x, y, z]: Vec3): Pt {
  return [(x - y) * 0.866, (x + y) * 0.5 - z * ZS];
}

function poly(ctx: CanvasRenderingContext2D, pts: Pt[]) {
  ctx.beginPath();
  ctx.moveTo(pts[0][0], pts[0][1]);
  for (let k = 1; k < pts.length; k++) ctx.lineTo(pts[k][0], pts[k][1]);
  ctx.closePath();
}

function towerSegments(o: Obj, base: number): [Vec3, Vec3, boolean][] {
  // A lattice transmission tower, oriented with its arms across the line.
  const u = o.rot ?? [1, 0];
  const p: Pt = [-u[1], u[0]];
  const segs: [Vec3, Vec3, boolean][] = [];
  const at = (a: number, b: number, z: number): Vec3 => [o.x + u[0] * a + p[0] * b, o.y + u[1] * a + p[1] * b, base + z];
  const H = 5.2;
  const half = (z: number) => 0.42 - (0.3 * Math.min(z, 3.6)) / 3.6;
  const levels = [0, 0.9, 1.75, 2.5, 3.1, 3.6, 4.3, 5.0];
  const corners: Pt[] = [
    [1, 1],
    [1, -1],
    [-1, -1],
    [-1, 1],
  ];
  for (let l = 0; l < levels.length - 1; l++) {
    const z0 = levels[l];
    const z1 = levels[l + 1];
    const h0 = half(z0);
    const h1 = half(z1);
    for (let c = 0; c < 4; c++) {
      const [a, b] = corners[c];
      const [a2, b2] = corners[(c + 1) % 4];
      segs.push([at(a * h0, b * h0, z0), at(a * h1, b * h1, z1), false]);
      segs.push([at(a * h0, b * h0, z0), at(a2 * h1, b2 * h1, z1), false]);
      segs.push([at(a2 * h0, b2 * h0, z0), at(a * h1, b * h1, z1), false]);
      segs.push([at(a * h1, b * h1, z1), at(a2 * h1, b2 * h1, z1), false]);
    }
  }
  // Cross-arms at two heights and the earth-wire peak.
  for (const [z, w] of [
    [3.6, 1.5],
    [4.3, 1.15],
  ] as const) {
    const h = half(z);
    segs.push([at(h, -w, z), at(h, w, z), false]);
    segs.push([at(-h, -w, z), at(-h, w, z), false]);
    segs.push([at(0, -w, z), at(h, -h, z - 0.35), false]);
    segs.push([at(0, w, z), at(h, h, z - 0.35), false]);
    for (const s of [-w, w]) segs.push([at(0, s, z), at(0, s, z - 0.4), true]);
  }
  segs.push([at(0.12, 0.12, 5.0), at(0, 0, H + 0.35), false]);
  segs.push([at(-0.12, -0.12, 5.0), at(0, 0, H + 0.35), false]);
  return segs;
}

function attachPoints(o: Obj, base: number): Vec3[] {
  const u = o.rot ?? [1, 0];
  const p: Pt = [-u[1], u[0]];
  const pts: Vec3[] = [];
  for (const [z, w] of [
    [3.2, 1.5],
    [3.9, 1.15],
  ] as const)
    for (const s of [-w, w]) pts.push([o.x + p[0] * s, o.y + p[1] * s, base + z]);
  return pts;
}

function mastSegments(o: Obj, base: number): [Vec3, Vec3, boolean][] {
  // A telecom / monitoring mast: a three-legged lattice with panels near the top.
  const segs: [Vec3, Vec3, boolean][] = [];
  const H = 7.5;
  const half = (z: number) => 0.38 * (1 - z / (H * 1.15));
  const corner = (k: number, z: number): Vec3 => {
    const ang = (k / 3) * Math.PI * 2 + 0.3;
    return [o.x + Math.cos(ang) * half(z), o.y + Math.sin(ang) * half(z), base + z];
  };
  for (let z = 0; z < H; z += 0.75) {
    for (let k = 0; k < 3; k++) {
      segs.push([corner(k, z), corner(k, z + 0.75), false]);
      segs.push([corner(k, z), corner((k + 1) % 3, z + 0.75), false]);
      segs.push([corner(k, z + 0.75), corner((k + 1) % 3, z + 0.75), false]);
    }
  }
  for (let k = 0; k < 3; k++) {
    const ang = (k / 3) * Math.PI * 2 + 0.3;
    const x = o.x + Math.cos(ang) * 0.42;
    const y = o.y + Math.sin(ang) * 0.42;
    segs.push([[x, y, base + H - 1.4], [x, y, base + H - 0.4], true]);
  }
  segs.push([[o.x, o.y, base + H], [o.x, o.y, base + H + 1.0], false]);
  return segs;
}

function drawSegs(ctx: CanvasRenderingContext2D, segs: [Vec3, Vec3, boolean][], P: (v: Vec3) => Pt, col: string, accent: string, lw: number) {
  ctx.lineWidth = lw;
  ctx.strokeStyle = col;
  ctx.beginPath();
  for (const [a, b, acc] of segs) {
    if (acc) continue;
    const pa = P(a);
    const pb = P(b);
    ctx.moveTo(pa[0], pa[1]);
    ctx.lineTo(pb[0], pb[1]);
  }
  ctx.stroke();
  ctx.strokeStyle = accent;
  ctx.lineWidth = lw * 1.6;
  ctx.beginPath();
  for (const [a, b, acc] of segs) {
    if (!acc) continue;
    const pa = P(a);
    const pb = P(b);
    ctx.moveTo(pa[0], pa[1]);
    ctx.lineTo(pb[0], pb[1]);
  }
  ctx.stroke();
}

function wires(scene: Scene, line: Line, base: (o: Obj) => number): [Vec3, Vec3][] {
  const spans: [Vec3, Vec3][] = [];
  const towers = line.towers.map((k) => scene.objects[k]);
  if (line.kind === "transmission") {
    const { a, b } = scene.corridor!;
    const first: Obj = { kind: "tower", x: a[0], y: a[1], s: 1, rot: towers[0].rot };
    const last: Obj = { kind: "tower", x: b[0], y: b[1], s: 1, rot: towers[0].rot };
    const chain = [first, ...towers, last];
    for (let k = 0; k < chain.length - 1; k++) {
      const pa = attachPoints(chain[k], k === 0 ? base(towers[0]) : base(chain[k]));
      const pb = attachPoints(chain[k + 1], k + 1 === chain.length - 1 ? base(towers[towers.length - 1]) : base(chain[k + 1]));
      for (let w = 0; w < pa.length; w++) spans.push([pa[w], pb[w]]);
    }
  } else {
    for (let k = 0; k < towers.length - 1; k++) {
      for (const off of [-0.35, 0, 0.35]) {
        const ta = towers[k];
        const tb = towers[k + 1];
        spans.push([
          [ta.x, ta.y + off * 0.3, base(ta) + 2.3],
          [tb.x, tb.y + off * 0.3, base(tb) + 2.3],
        ]);
      }
    }
  }
  return spans;
}

function sag(ctx: CanvasRenderingContext2D, a: Vec3, b: Vec3, P: (v: Vec3) => Pt, depth: number) {
  ctx.beginPath();
  for (let k = 0; k <= 16; k++) {
    const t = k / 16;
    const v: Vec3 = [a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t, a[2] + (b[2] - a[2]) * t - depth * 4 * t * (1 - t)];
    const p = P(v);
    if (k === 0) ctx.moveTo(p[0], p[1]);
    else ctx.lineTo(p[0], p[1]);
  }
  ctx.stroke();
}

type Mode = "solid" | "wire";

function drawWorld(ctx: CanvasRenderingContext2D, scene: Scene, P: (v: Vec3) => Pt, px: number, mode: Mode) {
  const { W, D, T, cells, plots, objects } = scene;
  const h = (i: number, j: number) => scene.heights[i][j];
  const solid = mode === "solid";

  // Shadow under the block.
  if (solid) {
    ctx.save();
    ctx.shadowColor = "rgba(26,19,16,0.28)";
    ctx.shadowBlur = 40 * px;
    ctx.shadowOffsetY = 18 * px;
    poly(ctx, [P([0, 0, -T]), P([W, 0, -T]), P([W, D, -T]), P([0, D, -T])]);
    ctx.fillStyle = "rgba(246,241,235,1)";
    ctx.fill();
    ctx.restore();
  }

  // The two visible sides of the soil block.
  const side = (pts: Vec3[], fill: string, k: number) => {
    poly(ctx, pts.map(P));
    if (solid) {
      ctx.fillStyle = shade(fill, k);
      ctx.fill();
    } else {
      ctx.fillStyle = "rgba(246,241,235,0.92)";
      ctx.fill();
      ctx.strokeStyle = "rgba(74,51,38,0.35)";
      ctx.lineWidth = 0.7 * px;
      ctx.stroke();
    }
  };
  for (let j = 0; j < D; j++)
    side(
      [
        [W, j, h(W, j)],
        [W, j + 1, h(W, j + 1)],
        [W, j + 1, -T],
        [W, j, -T],
      ],
      "#6b4a35",
      0.82,
    );
  for (let i = 0; i < W; i++)
    side(
      [
        [i, D, h(i, D)],
        [i + 1, D, h(i + 1, D)],
        [i + 1, D, -T],
        [i, D, -T],
      ],
      "#6b4a35",
      1.0,
    );
  // Soil layers on the sides.
  ctx.lineWidth = 0.8 * px;
  ctx.strokeStyle = solid ? "rgba(26,19,16,0.22)" : "rgba(74,51,38,0.25)";
  for (const f of [0.35, 0.7]) {
    ctx.beginPath();
    const z = -T * f;
    let p = P([0, D, z + (h(0, D) + T) * 0.05]);
    ctx.moveTo(p[0], p[1]);
    p = P([W, D, z]);
    ctx.lineTo(p[0], p[1]);
    p = P([W, 0, z + (h(W, 0) + T) * 0.05]);
    ctx.lineTo(p[0], p[1]);
    ctx.stroke();
  }

  // The land, back to front.
  for (let s = 0; s <= W + D - 2; s++) {
    for (let i = Math.max(0, s - (D - 1)); i <= Math.min(W - 1, s); i++) {
      const j = s - i;
      const c = cells[i][j];
      const q: Vec3[] = [
        [i, j, h(i, j)],
        [i + 1, j, h(i + 1, j)],
        [i + 1, j + 1, h(i + 1, j + 1)],
        [i, j + 1, h(i, j + 1)],
      ];
      const pts = q.map(P);
      poly(ctx, pts);
      if (solid) {
        const dzdx = (h(i + 1, j) + h(i + 1, j + 1) - h(i, j) - h(i, j + 1)) / 2;
        const dzdy = (h(i, j + 1) + h(i + 1, j + 1) - h(i, j) - h(i + 1, j)) / 2;
        const k = 1 - dzdx * 0.22 + dzdy * 0.1;
        ctx.fillStyle = shade(CROP[c.kind], k);
        ctx.fill();
        ctx.strokeStyle = shade(CROP[c.kind], k * 0.97);
        ctx.lineWidth = 0.6 * px;
        ctx.stroke();
        // Crop rows.
        if (["maize", "cassava", "beans", "rice", "tea"].includes(c.kind)) {
          ctx.strokeStyle = shade(CROP[c.kind], c.kind === "tea" ? 0.72 : 0.84);
          ctx.lineWidth = (c.kind === "tea" ? 1.6 : 0.8) * px;
          ctx.beginPath();
          for (const f of [0.25, 0.5, 0.75]) {
            const a: Vec3 = c.rows ? [i + f, j, hAt(scene, i + f, j)] : [i, j + f, hAt(scene, i, j + f)];
            const b: Vec3 = c.rows ? [i + f, j + 1, hAt(scene, i + f, j + 1)] : [i + 1, j + f, hAt(scene, i + 1, j + f)];
            const pa = P(a);
            const pb = P(b);
            ctx.moveTo(pa[0], pa[1]);
            ctx.lineTo(pb[0], pb[1]);
          }
          ctx.stroke();
        }
        if (c.kind === "water") {
          ctx.strokeStyle = "rgba(255,255,255,0.35)";
          ctx.lineWidth = 0.8 * px;
          ctx.beginPath();
          const a = P([i + 0.2, j + 0.5, h(i, j) + 0.02]);
          const b = P([i + 0.7, j + 0.5, h(i, j) + 0.02]);
          ctx.moveTo(a[0], a[1]);
          ctx.lineTo(b[0], b[1]);
          ctx.stroke();
        }
      } else {
        ctx.fillStyle = "rgba(246,241,235,0.96)";
        ctx.fill();
        ctx.strokeStyle = "rgba(74,51,38,0.09)";
        ctx.lineWidth = 0.5 * px;
        ctx.stroke();
        if (c.kind !== "road" && c.kind !== "grass" && c.kind !== "yard") {
          // A plant mark per cell quarter: what the drone counts.
          ctx.fillStyle = c.kind === "bare" || c.kind === "fallow" ? "rgba(74,51,38,0.12)" : "rgba(74,51,38,0.32)";
          for (const [fx, fy] of [
            [0.3, 0.3],
            [0.7, 0.3],
            [0.3, 0.7],
            [0.7, 0.7],
          ]) {
            if (c.kind === "bare" || c.kind === "fallow") continue;
            const p = P([i + fx, j + fy, hAt(scene, i + fx, j + fy)]);
            ctx.beginPath();
            ctx.arc(p[0], p[1], 0.75 * px, 0, Math.PI * 2);
            ctx.fill();
          }
        }
        if (c.kind === "road") {
          ctx.fillStyle = "rgba(74,51,38,0.08)";
          poly(ctx, pts);
          ctx.fill();
        }
      }
    }
  }

  // Plot boundaries (bunds).
  ctx.lineWidth = (solid ? 0.9 : 1.0) * px;
  ctx.strokeStyle = solid ? "rgba(26,19,16,0.28)" : "rgba(74,51,38,0.55)";
  for (const p of plots) {
    ctx.beginPath();
    plotOutline(scene, p).forEach((v, k) => {
      const q = P(v);
      if (k === 0) ctx.moveTo(q[0], q[1]);
      else ctx.lineTo(q[0], q[1]);
    });
    ctx.closePath();
    ctx.stroke();
  }

  // Things standing on the land, back to front, with the wires last.
  const base = (o: Obj) => hAt(scene, o.x, o.y);
  const order = [...objects].sort((o, q) => o.x + o.y - (q.x + q.y));
  for (const o of order) drawObject(ctx, scene, o, base(o), P, px, mode);
  for (const line of scene.lines) {
    ctx.strokeStyle = solid ? "rgba(26,19,16,0.62)" : "rgba(26,19,16,0.7)";
    ctx.lineWidth = 0.7 * px;
    for (const [a, b] of wires(scene, line, base)) sag(ctx, a, b, P, line.kind === "transmission" ? 0.8 : 0.25);
  }
}

function plotOutline(scene: Scene, p: Plot): Vec3[] {
  const pts: Vec3[] = [];
  const add = (x: number, y: number) => pts.push([x, y, hAt(scene, x, y) + 0.02]);
  for (let i = p.i0; i <= p.i1 + 1; i++) add(i, p.j0);
  for (let j = p.j0 + 1; j <= p.j1 + 1; j++) add(p.i1 + 1, j);
  for (let i = p.i1; i >= p.i0; i--) add(i, p.j1 + 1);
  for (let j = p.j1; j > p.j0; j--) add(p.i0, j);
  return pts;
}

function drawObject(ctx: CanvasRenderingContext2D, _scene: Scene, o: Obj, z: number, P: (v: Vec3) => Pt, px: number, mode: Mode) {
  const solid = mode === "solid";
  const ground = P([o.x, o.y, z]);
  const unit = Math.hypot(P([1, 0, 0])[0] - P([0, 0, 0])[0], P([1, 0, 0])[1] - P([0, 0, 0])[1]);
  if (o.kind === "tree" || o.kind === "eucalyptus" || o.kind === "banana") {
    const tall = o.kind === "eucalyptus" ? 1.9 : o.kind === "banana" ? 0.7 : 1.15;
    const r = (o.kind === "eucalyptus" ? 0.34 : o.kind === "banana" ? 0.3 : 0.42) * o.s * unit;
    const top = P([o.x, o.y, z + tall * o.s]);
    if (solid) {
      ctx.fillStyle = "rgba(26,19,16,0.22)";
      ctx.beginPath();
      ctx.ellipse(ground[0] + r * 0.6, ground[1] + r * 0.15, r * 1.1, r * 0.45, 0, 0, Math.PI * 2);
      ctx.fill();
      ctx.strokeStyle = "#4a3326";
      ctx.lineWidth = 1.1 * px;
      ctx.beginPath();
      ctx.moveTo(ground[0], ground[1]);
      ctx.lineTo(top[0], top[1] + r * 0.6);
      ctx.stroke();
      if (o.kind === "banana") {
        ctx.strokeStyle = "#617f3b";
        ctx.lineWidth = 1.8 * px;
        for (let k = 0; k < 6; k++) {
          const a = (k / 6) * Math.PI * 2;
          ctx.beginPath();
          ctx.moveTo(top[0], top[1]);
          ctx.quadraticCurveTo(top[0] + Math.cos(a) * r, top[1] - r * 0.6, top[0] + Math.cos(a) * r * 1.5, top[1] + Math.sin(a) * r * 0.6);
          ctx.stroke();
        }
      } else {
        const blobs = o.kind === "eucalyptus" ? 3 : 2;
        for (let k = 0; k < blobs; k++) {
          const cy = top[1] + (k - blobs / 2) * r * 0.7;
          ctx.fillStyle = k === blobs - 1 ? "#55703c" : "#3d5530";
          ctx.beginPath();
          ctx.ellipse(top[0] + (k % 2 ? r * 0.25 : -r * 0.15), cy, r * (o.kind === "eucalyptus" ? 0.75 : 1), r * 0.85, 0, 0, Math.PI * 2);
          ctx.fill();
        }
        ctx.fillStyle = "rgba(255,240,200,0.16)";
        ctx.beginPath();
        ctx.ellipse(top[0] - r * 0.3, top[1] - r * 0.6, r * 0.45, r * 0.3, 0, 0, Math.PI * 2);
        ctx.fill();
      }
    } else {
      ctx.strokeStyle = "rgba(74,51,38,0.55)";
      ctx.lineWidth = 0.8 * px;
      ctx.beginPath();
      ctx.moveTo(ground[0], ground[1]);
      ctx.lineTo(top[0], top[1]);
      ctx.stroke();
      ctx.beginPath();
      ctx.ellipse(top[0], top[1], r, r * (o.kind === "eucalyptus" ? 1.4 : 0.9), 0, 0, Math.PI * 2);
      ctx.fillStyle = "rgba(246,241,235,0.9)";
      ctx.fill();
      ctx.stroke();
      ctx.beginPath();
      ctx.ellipse(ground[0], ground[1], r * 0.9, r * 0.4, 0, 0, Math.PI * 2);
      ctx.stroke();
    }
    return;
  }
  if (o.kind === "house" || o.kind === "shelter") {
    const w = 0.55 * o.s;
    const d = (o.kind === "shelter" ? 0.4 : 0.38) * o.s;
    const hgt = (o.kind === "shelter" ? 0.45 : 0.5) * o.s;
    const ridge = o.kind === "shelter" ? 0.05 : 0.32 * o.s;
    const x0 = o.x - w;
    const x1 = o.x + w;
    const y0 = o.y - d;
    const y1 = o.y + d;
    const front: Vec3[] = [
      [x0, y1, z],
      [x1, y1, z],
      [x1, y1, z + hgt],
      [x0, y1, z + hgt],
    ];
    const right: Vec3[] = [
      [x1, y0, z],
      [x1, y1, z],
      [x1, y1, z + hgt],
      [x1, o.y, z + hgt + ridge],
      [x1, y0, z + hgt],
    ];
    const roofFront: Vec3[] = [
      [x0 - 0.05, y1 + 0.06, z + hgt - 0.02],
      [x1 + 0.05, y1 + 0.06, z + hgt - 0.02],
      [x1 + 0.05, o.y, z + hgt + ridge],
      [x0 - 0.05, o.y, z + hgt + ridge],
    ];
    if (solid) {
      ctx.fillStyle = "rgba(26,19,16,0.2)";
      poly(ctx, [P([x0 + 0.2, y0 + 0.2, z]), P([x1 + 0.35, y0 + 0.2, z]), P([x1 + 0.35, y1 + 0.3, z]), P([x0 + 0.2, y1 + 0.3, z])]);
      ctx.fill();
      poly(ctx, front.map(P));
      ctx.fillStyle = o.kind === "shelter" ? "#d8d4cc" : "#dccab0";
      ctx.fill();
      poly(ctx, right.map(P));
      ctx.fillStyle = o.kind === "shelter" ? "#b9b4ab" : "#bea684";
      ctx.fill();
      poly(ctx, roofFront.map(P));
      ctx.fillStyle = shade(o.roof ?? "#9c9a95", 1.06);
      ctx.fill();
      ctx.strokeStyle = "rgba(26,19,16,0.25)";
      ctx.lineWidth = 0.5 * px;
      ctx.stroke();
      // A door.
      const dA = P([o.x - 0.08, y1, z]);
      const dB = P([o.x + 0.08, y1, z + hgt * 0.65]);
      ctx.fillStyle = "#4a3326";
      ctx.fillRect(dA[0], dB[1], dB[0] - dA[0], dA[1] - dB[1]);
    } else {
      ctx.strokeStyle = "rgba(74,51,38,0.6)";
      ctx.lineWidth = 0.8 * px;
      ctx.fillStyle = "rgba(246,241,235,0.95)";
      for (const f of [front, right, roofFront]) {
        poly(ctx, f.map(P));
        ctx.fill();
        ctx.stroke();
      }
    }
    return;
  }
  if (o.kind === "tower") {
    drawSegs(ctx, towerSegments(o, z), P, solid ? "rgba(70,66,62,0.85)" : "rgba(26,19,16,0.78)", solid ? "rgba(200,196,188,0.95)" : "rgba(26,19,16,0.9)", 0.6 * px);
    return;
  }
  if (o.kind === "mast") {
    const segs = mastSegments(o, z);
    if (solid) {
      // Red and white bands, as on Rwanda's telecom masts.
      ctx.lineWidth = 0.75 * px;
      for (const [a, b, acc] of segs) {
        const band = Math.floor((a[2] - z) / 1.5) % 2 === 0;
        ctx.strokeStyle = acc ? "#d8d4cc" : band ? "#a14d34" : "#e6e1d8";
        ctx.beginPath();
        const pa = P(a);
        const pb = P(b);
        ctx.moveTo(pa[0], pa[1]);
        ctx.lineTo(pb[0], pb[1]);
        ctx.stroke();
      }
    } else drawSegs(ctx, segs, P, "rgba(26,19,16,0.78)", "rgba(26,19,16,0.9)", 0.6 * px);
    // A dish on the mast.
    const dish = P([o.x + 0.45, o.y + 0.45, z + 5.2]);
    ctx.beginPath();
    ctx.ellipse(dish[0], dish[1], 0.42 * unit, 0.5 * unit, -0.4, 0, Math.PI * 2);
    ctx.fillStyle = solid ? "#ece8e0" : "rgba(246,241,235,0.95)";
    ctx.fill();
    ctx.strokeStyle = solid ? "rgba(26,19,16,0.35)" : "rgba(26,19,16,0.7)";
    ctx.lineWidth = 0.7 * px;
    ctx.stroke();
    return;
  }
  if (o.kind === "pole") {
    const top = P([o.x, o.y, z + 2.4]);
    ctx.strokeStyle = solid ? "#5a4636" : "rgba(26,19,16,0.8)";
    ctx.lineWidth = 1.4 * px;
    ctx.beginPath();
    ctx.moveTo(ground[0], ground[1]);
    ctx.lineTo(top[0], top[1]);
    ctx.stroke();
    const a = P([o.x, o.y - 0.45, z + 2.3]);
    const b = P([o.x, o.y + 0.45, z + 2.3]);
    ctx.lineWidth = 1 * px;
    ctx.beginPath();
    ctx.moveTo(a[0], a[1]);
    ctx.lineTo(b[0], b[1]);
    ctx.stroke();
  }
}

// ---------- public: render a world into two canvases ----------

export function renderWorld(scene: Scene, cssW: number, cssH: number, dpr: number, padding = 0.06): Rendered {
  const px = dpr;
  const W = Math.round(cssW * dpr);
  const H = Math.round(cssH * dpr);
  // Fit the block and the tallest things standing on it (towers, masts, the drone's height) into the canvas.
  const corners: Vec3[] = [];
  for (const [x, y] of [
    [0, 0],
    [scene.W, 0],
    [scene.W, scene.D],
    [0, scene.D],
  ] as const) {
    corners.push([x, y, -scene.T], [x, y, hAt(scene, x, y) + 0.6]);
  }
  for (const o of scene.objects) {
    const tall = o.kind === "tower" ? 6.2 : o.kind === "mast" ? 9 : o.kind === "eucalyptus" ? 2.6 : 1.2;
    corners.push([o.x, o.y, hAt(scene, o.x, o.y) + tall]);
  }
  if (scene.corridor) {
    const { a, b, z } = scene.corridor;
    for (const t of [0, 0.5, 1]) {
      const x = a[0] + (b[0] - a[0]) * t;
      const y = a[1] + (b[1] - a[1]) * t;
      corners.push([x, y, hAt(scene, x, y) + z + 2.4]);
    }
  }
  const pr = corners.map(iso);
  const minX = Math.min(...pr.map((p) => p[0]));
  const maxX = Math.max(...pr.map((p) => p[0]));
  const minY = Math.min(...pr.map((p) => p[1]));
  const maxY = Math.max(...pr.map((p) => p[1]));
  const k = Math.min((W * (1 - 2 * padding)) / (maxX - minX), (H * (1 - 2 * padding)) / (maxY - minY));
  const ox = (W - (maxX - minX) * k) / 2 - minX * k;
  const oy = (H - (maxY - minY) * k) / 2 - minY * k;
  const project = (v: Vec3): Pt => {
    const p = iso(v);
    return [p[0] * k + ox, p[1] * k + oy];
  };
  const make = (mode: Mode) => {
    const c = document.createElement("canvas");
    c.width = W;
    c.height = H;
    const ctx = c.getContext("2d")!;
    ctx.lineJoin = "round";
    ctx.lineCap = "round";
    drawWorld(ctx, scene, project, px * Math.max(0.7, Math.min(1.25, k / 22)), mode);
    return c;
  };
  const solid = make("solid");
  const wire = make("wire");

  // Boxes around what the drone reads.
  const boxes: Rendered["boxes"] = [];
  for (const d of scene.detections) {
    let pts: Pt[] = [];
    let poly: Pt[] | undefined;
    if (d.plot !== undefined) {
      poly = plotOutline(scene, scene.plots[d.plot]).map(project);
      pts = poly;
    } else if (d.obj !== undefined) {
      const o = findObject(scene, d);
      if (!o) continue;
      const z = hAt(scene, o.x, o.y);
      const tall = o.kind === "tower" ? 5.6 : o.kind === "eucalyptus" ? 2.6 * o.s : 1.6;
      const r = o.kind === "tower" ? 1.6 : 0.6;
      pts = [project([o.x - r, o.y, z]), project([o.x + r, o.y, z + tall]), project([o.x, o.y - r, z]), project([o.x, o.y + r, z + tall])];
      if (o.kind === "tower") pts = [project([o.x, o.y, z + 2.9]), project([o.x - 1.2, o.y + 1.2, z + 4.8]), project([o.x + 1.2, o.y - 1.2, z + 4.8])];
    }
    if (!pts.length) continue;
    const xs = pts.map((p) => p[0]);
    const ys = pts.map((p) => p[1]);
    const x = Math.min(...xs);
    const y = Math.min(...ys);
    boxes.push({ x, y, w: Math.max(...xs) - x, h: Math.max(...ys) - y, label: d.label, unsure: !!d.unsure, poly });
  }
  boxes.sort((a, b) => a.x - b.x);
  return {
    solid,
    wire,
    project,
    boxes,
    scene,
    bounds: { x0: minX * k + ox, x1: maxX * k + ox, y0: minY * k + oy, y1: maxY * k + oy },
  };
}

function findObject(scene: Scene, d: Detection): Obj | undefined {
  // Detections name objects by their kind and place; after sorting, the first tower of the line or the
  // tallest eucalyptus nearest the line is the one meant.
  if (d.label.startsWith("tower")) return scene.objects[scene.lines[0]?.towers[1] ?? -1];
  if (d.label.startsWith("tree")) {
    const c = scene.corridor;
    if (!c) return undefined;
    return scene.objects
      .filter((o) => o.kind === "eucalyptus" && o.s >= 1.25)
      .sort((o, q) => o.x + o.y - (q.x + q.y))[0];
  }
  return undefined;
}

export { INK, COCOA, CARAMEL };
