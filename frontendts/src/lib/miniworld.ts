// Copied from noza-web/src/lib/miniworld.ts (the website's miniature worlds) so Ingabe shows the same pictures.
// Keep the two identical: change the website's copy first, then copy it here.
// Miniature worlds: small isometric dioramas of Rwandan land, drawn on a canvas to look like tilt-shift
// photographs (crop rows laid in perspective, low sun, cast shadows, shallow focus, grain). Each world is
// drawn twice: once as it looks ("solid") and once as the drone reads it ("wire"), so the page can sweep
// between them. Everything is generated from a seed; there are no images.

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
  seed: number;
}

export interface Detection {
  label: string;
  unsure?: boolean;
  plot?: number;
  obj?: number;
}

interface Line {
  towers: number[];
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
  fence?: Pt[];
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

const hsl = (h: number, s: number, l: number, a = 1) => `hsla(${h},${s}%,${l}%,${a})`;

// ---------- the land ----------

export const TOWER_H = 7.4;
export const MAST_H = 10.5;

function heightField(preset: Preset, W: number, D: number, r: () => number): number[][] {
  const p1 = r() * 6;
  const p2 = r() * 6;
  const h: number[][] = [];
  for (let i = 0; i <= W; i++) {
    h.push([]);
    for (let j = 0; j <= D; j++) {
      const x = i / W;
      const y = j / D;
      let z: number;
      if (preset === "marsh" || preset === "scheme") z = 0.08 * Math.sin(x * 3 + p1) * Math.cos(y * 2 + p2);
      else if (preset === "hills" || preset === "tea") {
        z = 2.0 * Math.exp(-((x - 0.3) ** 2 + (y - 0.35) ** 2) * 3.4) + 0.3 * Math.sin(x * 5 + p1);
        z = Math.round(z * 3.5) / 3.5; // terraces
      } else if (preset === "telecom") z = 1.9 * Math.exp(-((x - 0.5) ** 2 + (y - 0.45) ** 2) * 5);
      else if (preset === "hero") z = 0.4 * Math.sin(x * 4.2 + p1) * Math.cos(y * 3.1 + p2) + 1.4 * Math.exp(-((x - 0.86) ** 2 + (y - 0.12) ** 2) * 14);
      else z = 0.45 * Math.sin(x * 4.2 + p1) * Math.cos(y * 3.1 + p2) + 0.3 * Math.sin((x + y) * 2.4 + p2);
      h[i].push(z);
    }
  }
  return h;
}

function cropMix(preset: Preset): CellKind[] {
  switch (preset) {
    case "marsh":
      return ["rice", "rice", "rice", "rice", "fallow"];
    case "tea":
      return ["tea", "tea", "tea", "tea", "banana"];
    case "scheme":
      return ["maize", "beans", "maize", "bare", "beans", "rice"];
    case "telecom":
      return ["grass", "fallow", "grass", "cassava", "banana"];
    default:
      return ["maize", "cassava", "beans", "maize", "banana", "bare", "cassava", "fallow", "maize"];
  }
}

export function hAt(scene: Scene, x: number, y: number): number {
  const { heights, W, D } = scene;
  const cx = Math.max(0, Math.min(W - 0.0001, x));
  const cy = Math.max(0, Math.min(D - 0.0001, y));
  const i = Math.floor(cx);
  const j = Math.floor(cy);
  const fx = cx - i;
  const fy = cy - j;
  return heights[i][j] * (1 - fx) * (1 - fy) + heights[i + 1][j] * fx * (1 - fy) + heights[i][j + 1] * (1 - fx) * fy + heights[i + 1][j + 1] * fx * fy;
}

export function buildScene(preset: Preset, seed = 7): Scene {
  const r = rng(seed);
  const W = preset === "hero" ? 18 : 14;
  const D = W;
  const T = preset === "hero" ? 1.9 : 1.5;
  const heights = heightField(preset, W, D, r);
  const cells: Cell[][] = Array.from({ length: W }, () => Array.from({ length: D }, () => ({ kind: "grass" as CellKind, plot: -1, rows: 0 as 0 | 1 })));
  const objects: Obj[] = [];
  const lines: Line[] = [];
  const detections: Detection[] = [];
  const add = (o: Omit<Obj, "seed">) => {
    objects.push({ ...o, seed: Math.floor(r() * 1e9) });
    return objects.length - 1;
  };

  // A dirt road where people live.
  const road = preset !== "marsh" && preset !== "tea";
  const roadJ = Math.floor(D * (preset === "distribution" ? 0.5 : 0.82));
  if (road)
    for (let i = 0; i < W; i++) {
      const j = roadJ + Math.round(Math.sin(i * 0.35 + seed) * 0.6);
      if (j >= 0 && j < D) cells[i][j].kind = "road";
    }

  // A mast on the hill, inside a gravel yard.
  let mast: Pt | undefined;
  if (preset === "hero" || preset === "telecom") {
    mast = preset === "hero" ? [W * 0.86, D * 0.13] : [W * 0.5, D * 0.42];
    for (let i = Math.floor(mast[0]) - 1; i <= Math.floor(mast[0]) + 1; i++)
      for (let j = Math.floor(mast[1]) - 1; j <= Math.floor(mast[1]) + 1; j++) if (cells[i]?.[j]) cells[i][j] = { kind: "yard", plot: -2, rows: 0 };
  }

  // Plots: greedy rectangles.
  const plots: Plot[] = [];
  const mix = cropMix(preset);
  const big = preset === "scheme" || preset === "marsh";
  for (let i = 0; i < W; i++)
    for (let j = 0; j < D; j++) {
      if (cells[i][j].plot !== -1 || cells[i][j].kind === "road") continue;
      const w = big ? 3 + Math.floor(r() * 2) : 2 + Math.floor(r() * 2);
      const d = big ? 3 + Math.floor(r() * 3) : 2 + Math.floor(r() * 3);
      const kind = mix[Math.floor(r() * mix.length)];
      const rows: 0 | 1 = r() > 0.5 ? 1 : 0;
      const id = plots.length;
      let i1 = i;
      let j1 = j;
      for (let a = i; a < Math.min(W, i + w); a++)
        for (let b = j; b < Math.min(D, j + d); b++) {
          if (cells[a][b].plot !== -1 || cells[a][b].kind === "road") continue;
          cells[a][b] = { kind, plot: id, rows };
          i1 = Math.max(i1, a);
          j1 = Math.max(j1, b);
        }
      plots.push({ id, i0: i, j0: j, i1, j1, kind });
    }
  if (preset === "scheme" || preset === "marsh")
    for (let i = 0; i < W; i++) for (const j of [Math.floor(D / 3), Math.floor((2 * D) / 3)]) cells[i][j] = { kind: "water", plot: -3, rows: 0 };

  const free = (x: number, y: number, pad = 0) => {
    for (const [dx, dy] of [
      [0, 0],
      [pad, 0],
      [-pad, 0],
      [0, pad],
      [0, -pad],
    ]) {
      const c = cells[Math.floor(x + dx)]?.[Math.floor(y + dy)];
      if (!c || c.kind === "road" || c.kind === "water" || c.kind === "yard") return false;
    }
    return true;
  };

  // Power: a transmission line across the land, or poles along the road.
  let corridor: Scene["corridor"];
  if (preset === "hero" || preset === "corridor") {
    const a: Pt = preset === "hero" ? [0.3, D * 0.68] : [0.3, D * 0.62];
    const b: Pt = preset === "hero" ? [W - 0.3, D * 0.5] : [W - 0.3, D * 0.22];
    const len = Math.hypot(b[0] - a[0], b[1] - a[1]);
    const u: Pt = [(b[0] - a[0]) / len, (b[1] - a[1]) / len];
    const ids: number[] = [];
    for (const t of [0.1, 0.5, 0.9]) {
      ids.push(add({ kind: "tower", x: a[0] + (b[0] - a[0]) * t, y: a[1] + (b[1] - a[1]) * t, s: 1, rot: u }));
    }
    lines.push({ towers: ids, kind: "transmission" });
    corridor = { a, b, z: TOWER_H - 1.6 };
    // The strip under the line is kept as grass, with one tree grown close to the wires.
    for (let i = 0; i < W; i++)
      for (let j = 0; j < D; j++) {
        const t = ((i + 0.5 - a[0]) * u[0] + (j + 0.5 - a[1]) * u[1]) / len;
        const dist = Math.abs((i + 0.5 - a[0]) * -u[1] + (j + 0.5 - a[1]) * u[0]);
        if (t > 0 && t < 1 && dist < 0.9 && cells[i][j].kind !== "road") cells[i][j] = { kind: "grass", plot: -4, rows: 0 };
      }
    const t = 0.66;
    const tree = add({ kind: "eucalyptus", x: a[0] + (b[0] - a[0]) * t - u[1] * 1.05, y: a[1] + (b[1] - a[1]) * t + u[0] * 1.05, s: 1.5 });
    detections.push({ label: "tower 2 · insulators", obj: ids[1] });
    detections.push({ label: "tree close to line", obj: tree });
  }
  if (preset === "distribution") {
    const ids: number[] = [];
    for (let i = 1; i < W; i += 3) ids.push(add({ kind: "pole", x: i + 0.5, y: roadJ - 0.55, s: 1 }));
    lines.push({ towers: ids, kind: "distribution" });
  }
  let fence: Pt[] | undefined;
  if (mast) {
    const m = add({ kind: "mast", x: mast[0], y: mast[1], s: 1 });
    add({ kind: "shelter", x: mast[0] + 0.8, y: mast[1] + 0.75, s: 0.85 });
    add({ kind: "shelter", x: mast[0] - 0.75, y: mast[1] + 0.9, s: 0.7 });
    const [mx, my] = [Math.floor(mast[0]) - 1 + 0.08, Math.floor(mast[1]) - 1 + 0.08];
    fence = [
      [mx, my],
      [mx + 2.84, my],
      [mx + 2.84, my + 2.84],
      [mx, my + 2.84],
    ];
    detections.push({ label: "mast · antennas", obj: m });
  }

  const nearLine = (x: number, y: number) => {
    if (!corridor) return false;
    const { a, b } = corridor;
    const dx = b[0] - a[0];
    const dy = b[1] - a[1];
    const t = ((x - a[0]) * dx + (y - a[1]) * dy) / (dx * dx + dy * dy);
    return Math.hypot(x - (a[0] + dx * t), y - (a[1] + dy * t)) < 1.5;
  };
  const nearMast = (x: number, y: number) => !!mast && Math.hypot(x - mast[0], y - mast[1]) < 2.2;

  // Houses along the road, trees and bananas around them.
  if (road) {
    const houses = preset === "hero" ? 8 : preset === "distribution" ? 7 : 4;
    for (let k = 0, tries = 0; k < houses && tries < 80; tries++) {
      const x = 1 + r() * (W - 2.5);
      const y = roadJ + (r() > 0.5 ? 1.2 : -0.9) + r() * 0.3;
      if (!free(x, y, 0.4) || nearLine(x, y) || nearMast(x, y)) continue;
      if (objects.some((o) => o.kind === "house" && Math.hypot(o.x - x, o.y - y) < 1.5)) continue;
      add({ kind: "house", x, y, s: 0.85 + r() * 0.3, roof: r() > 0.45 ? "#a9a6a0" : "#9b5638" });
      k++;
    }
  }
  const trees = preset === "marsh" || preset === "scheme" ? 12 : preset === "tea" ? 14 : 40;
  for (let k = 0; k < trees; k++) {
    const x = 0.4 + r() * (W - 0.8);
    const y = 0.4 + r() * (D - 0.8);
    if (!free(x, y) || nearLine(x, y) || nearMast(x, y)) continue;
    const c = cells[Math.floor(x)][Math.floor(y)];
    if (["maize", "beans", "rice", "tea"].includes(c.kind) && r() > 0.25) continue;
    add({ kind: c.kind === "banana" ? "banana" : r() > 0.5 ? "eucalyptus" : "tree", x, y, s: 0.75 + r() * 0.45 });
  }
  for (const p of plots) {
    if (p.kind !== "banana") continue;
    for (let a = p.i0; a <= p.i1; a++)
      for (let b = p.j0; b <= p.j1; b++)
        if (cells[a][b].plot === p.id)
          for (const [fx, fy] of [
            [0.3, 0.3],
            [0.75, 0.7],
          ])
            if (r() > 0.3) add({ kind: "banana", x: a + fx + r() * 0.1, y: b + fy, s: 0.8 + r() * 0.3 });
  }

  // What the drone reads on the plots: two looks that agree name the crop, otherwise "not sure".
  if (preset === "hero" || preset === "hills" || preset === "scheme") {
    const sized = plots
      .filter((p) => ["maize", "cassava", "beans"].includes(p.kind) && (p.i1 - p.i0 + 1) * (p.j1 - p.j0 + 1) >= 4)
      .filter((p) => !nearLine((p.i0 + p.i1 + 1) / 2, (p.j0 + p.j1 + 1) / 2) && !nearMast((p.i0 + p.i1 + 1) / 2, (p.j0 + p.j1 + 1) / 2))
      .sort((p, q) => p.i0 - p.j0 - (q.i0 - q.j0));
    const picks = preset === "hero" ? [0.15, 0.6] : [0.2, 0.5, 0.85];
    picks.forEach((f, k) => {
      const p = sized[Math.floor(sized.length * f)];
      if (!p) return;
      if (k === 1) detections.push({ label: `not sure · ${p.kind === "beans" ? "beans or maize" : "maize or beans"}?`, plot: p.id, unsure: true });
      else detections.push({ label: p.kind, plot: p.id });
    });
  }

  return { W, D, T, heights, cells, plots, objects, lines, detections, corridor, fence };
}

// ---------- textures: one seamless tile per crop, a grid cell wide ----------

const TILE = 96;
const tiles = new Map<string, HTMLCanvasElement>();

function tile(kind: CellKind, rows: 0 | 1): HTMLCanvasElement {
  const key = `${kind}${rows}`;
  const hit = tiles.get(key);
  if (hit) return hit;
  const c = document.createElement("canvas");
  c.width = TILE;
  c.height = TILE;
  const g = c.getContext("2d")!;
  const r = rng(kind.length * 977 + kind.charCodeAt(0) * 13 + rows * 31);
  const T = TILE;
  // Draw at (x, y) and again across every edge it touches, so the tile repeats without seams.
  const wrap = (x: number, y: number, rad: number, f: (x: number, y: number) => void) => {
    for (const dx of [-T, 0, T]) for (const dy of [-T, 0, T]) if (x + dx > -rad && x + dx < T + rad && y + dy > -rad && y + dy < T + rad) f(x + dx, y + dy);
  };
  const soil = (l: number) => {
    g.fillStyle = hsl(26, 32, l);
    g.fillRect(0, 0, T, T);
    for (let k = 0; k < 900; k++) {
      g.fillStyle = hsl(24 + r() * 10, 25 + r() * 15, l - 8 + r() * 16, 0.5);
      g.fillRect(r() * T, r() * T, 1 + r() * 1.5, 1 + r() * 1.5);
    }
  };
  const leaf = (x: number, y: number, len: number, wid: number, ang: number, col: string) => {
    g.save();
    g.translate(x, y);
    g.rotate(ang);
    g.fillStyle = col;
    g.beginPath();
    g.ellipse(len / 2, 0, len / 2, wid, 0, 0, Math.PI * 2);
    g.fill();
    g.restore();
  };
  if (rows) {
    g.translate(T, 0);
    g.rotate(Math.PI / 2);
  }
  switch (kind) {
    case "maize":
      soil(34);
      for (let row = 0; row < 6; row++)
        for (let k = 0; k < 11; k++) {
          const x = (k + 0.5) * (T / 11) + (r() - 0.5) * 3;
          const y = (row + 0.5) * (T / 6) + (r() - 0.5) * 2;
          const leaves = Array.from({ length: 7 }, () => [7 + r() * 6, r() * Math.PI * 2, 70 + r() * 16, 46 + r() * 14, 30 + r() * 18]);
          wrap(x, y, 14, (px, py) => {
            for (const [len, ang, hh, ss, ll] of leaves) leaf(px, py, len, 1.7, ang, hsl(hh, ss, ll));
            g.fillStyle = hsl(70, 45, 46);
            g.beginPath();
            g.arc(px, py, 1.6, 0, Math.PI * 2);
            g.fill();
          });
        }
      break;
    case "cassava":
      soil(32);
      for (let k = 0; k < 25; k++) {
        const x = ((k % 5) + 0.5) * (T / 5) + (r() - 0.5) * 6;
        const y = (Math.floor(k / 5) + 0.5) * (T / 5) + (r() - 0.5) * 6;
        const leaves = Array.from({ length: 16 }, () => [6 + r() * 7, r() * Math.PI * 2, 92 + r() * 20, 36 + r() * 14, 22 + r() * 16]);
        wrap(x, y, 20, (px, py) => {
          for (const [len, ang, hh, ss, ll] of leaves) leaf(px, py, len, 2.6, ang, hsl(hh, ss, ll));
        });
      }
      break;
    case "beans":
      soil(30);
      for (let row = 0; row < 9; row++)
        for (let k = 0; k < 26; k++) {
          const x = (k + r()) * (T / 26);
          const y = (row + 0.5) * (T / 9) + (r() - 0.5) * 3;
          const col = hsl(76 + r() * 16, 48 + r() * 12, 36 + r() * 18);
          const rad = 2.2 + r() * 1.6;
          wrap(x, y, 6, (px, py) => {
            g.fillStyle = col;
            g.beginPath();
            g.arc(px, py, rad, 0, Math.PI * 2);
            g.fill();
          });
        }
      break;
    case "banana":
      g.fillStyle = hsl(80, 30, 26);
      g.fillRect(0, 0, T, T);
      for (let k = 0; k < 700; k++) {
        g.fillStyle = hsl(70 + r() * 30, 30, 20 + r() * 16, 0.6);
        g.fillRect(r() * T, r() * T, 2, 2);
      }
      break;
    case "bare":
      soil(41);
      g.strokeStyle = hsl(24, 30, 30, 0.45);
      g.lineWidth = 1.2;
      for (let row = 0; row < 8; row++) {
        g.beginPath();
        g.moveTo(0, (row + 0.5) * (T / 8));
        g.lineTo(T, (row + 0.5) * (T / 8));
        g.stroke();
      }
      break;
    case "fallow":
      g.fillStyle = hsl(40, 32, 52);
      g.fillRect(0, 0, T, T);
      for (let k = 0; k < 520; k++) {
        const x = r() * T;
        const y = r() * T;
        g.strokeStyle = hsl(38 + r() * 30, 25 + r() * 20, 38 + r() * 24, 0.8);
        g.lineWidth = 1;
        g.beginPath();
        g.moveTo(x, y);
        g.lineTo(x + (r() - 0.5) * 5, y - 2 - r() * 4);
        g.stroke();
      }
      break;
    case "grass":
    case "yard":
      g.fillStyle = kind === "yard" ? hsl(33, 10, 68) : hsl(78, 30, 38);
      g.fillRect(0, 0, T, T);
      for (let k = 0; k < 1100; k++) {
        g.fillStyle = kind === "yard" ? hsl(30, 8, 50 + r() * 30, 0.7) : hsl(68 + r() * 26, 26 + r() * 20, 28 + r() * 20, 0.75);
        g.fillRect(r() * T, r() * T, 1.5, kind === "yard" ? 1.5 : 2.5);
      }
      break;
    case "road":
      g.fillStyle = hsl(34, 30, 66);
      g.fillRect(0, 0, T, T);
      for (let k = 0; k < 700; k++) {
        g.fillStyle = hsl(30, 22, 52 + r() * 22, 0.6);
        g.fillRect(r() * T, r() * T, 1.5, 1.5);
      }
      g.fillStyle = hsl(30, 25, 55, 0.45);
      g.fillRect(0, T * 0.32, T, 5);
      g.fillRect(0, T * 0.62, T, 5);
      break;
    case "water":
      g.fillStyle = hsl(186, 14, 44);
      g.fillRect(0, 0, T, T);
      for (let k = 0; k < 40; k++) {
        g.strokeStyle = hsl(186, 20, 64 + r() * 14, 0.35);
        g.lineWidth = 1;
        const x = r() * T;
        const y = r() * T;
        g.beginPath();
        g.moveTo(x, y);
        g.lineTo(x + 8 + r() * 10, y);
        g.stroke();
      }
      break;
    case "rice":
      g.fillStyle = hsl(150, 12, 36);
      g.fillRect(0, 0, T, T);
      for (let row = 0; row < 10; row++)
        for (let k = 0; k < 24; k++) {
          const x = (k + 0.5) * (T / 24);
          const y = (row + 0.5) * (T / 10);
          g.strokeStyle = hsl(68 + r() * 16, 52 + r() * 14, 38 + r() * 18);
          g.lineWidth = 1.3;
          g.beginPath();
          g.moveTo(x, y + 3);
          g.lineTo(x + (r() - 0.5) * 3, y - 3);
          g.stroke();
        }
      break;
    case "tea":
      g.fillStyle = hsl(30, 25, 18);
      g.fillRect(0, 0, T, T);
      for (let row = 0; row < 4; row++)
        for (let k = 0; k < 20; k++) {
          const x = (k + r()) * (T / 20);
          const y = (row + 0.5) * (T / 4);
          const rad = 8 + r() * 2;
          wrap(x, y, 12, (px, py) => {
            const grd = g.createRadialGradient(px - 2, py - 3, 1, px, py, rad + 2);
            grd.addColorStop(0, hsl(92, 42, 44));
            grd.addColorStop(1, hsl(105, 40, 20));
            g.fillStyle = grd;
            g.beginPath();
            g.arc(px, py, rad, 0, Math.PI * 2);
            g.fill();
          });
        }
      break;
  }
  tiles.set(key, c);
  return c;
}

let grainTile: HTMLCanvasElement | null = null;
function grain(): HTMLCanvasElement {
  if (grainTile) return grainTile;
  const c = document.createElement("canvas");
  c.width = 160;
  c.height = 160;
  const g = c.getContext("2d")!;
  const img = g.createImageData(160, 160);
  const r = rng(5);
  for (let k = 0; k < img.data.length; k += 4) {
    const v = 128 + (r() - 0.5) * 90;
    img.data[k] = v;
    img.data[k + 1] = v;
    img.data[k + 2] = v;
    img.data[k + 3] = 255;
  }
  g.putImageData(img, 0, 0);
  grainTile = c;
  return c;
}

// ---------- projection ----------

const ZS = 0.8; // screen units per unit of height (true isometric is ~0.82)
const SUN: Pt = [0.62, 0.18]; // ground shift of a shadow per unit of height: a low sun from the back left

function iso([x, y, z]: Vec3): Pt {
  return [(x - y) * 0.866, (x + y) * 0.5 - z * ZS];
}

function poly(ctx: CanvasRenderingContext2D, pts: Pt[]) {
  ctx.beginPath();
  ctx.moveTo(pts[0][0], pts[0][1]);
  for (let k = 1; k < pts.length; k++) ctx.lineTo(pts[k][0], pts[k][1]);
  ctx.closePath();
}

function strokeSegs(ctx: CanvasRenderingContext2D, segs: [Vec3, Vec3][], P: P, dx = 0, dy = 0) {
  ctx.beginPath();
  for (const [a, b] of segs) {
    const pa = P(a);
    const pb = P(b);
    ctx.moveTo(pa[0] + dx, pa[1] + dy);
    ctx.lineTo(pb[0] + dx, pb[1] + dy);
  }
  ctx.stroke();
}

// ---------- structures as 3D segments ----------

type Segs = [Vec3, Vec3][];
type P = (v: Vec3) => Pt;

function towerSegments(o: Obj, base: number): { steel: Segs; insulators: Segs } {
  const u = o.rot ?? [1, 0];
  const p: Pt = [-u[1], u[0]];
  const at = (a: number, b: number, z: number): Vec3 => [o.x + u[0] * a + p[0] * b, o.y + u[1] * a + p[1] * b, base + z];
  const H = TOWER_H;
  const waist = H * 0.68;
  const half = (z: number) => (z < waist ? 0.62 - (0.46 * z) / waist : 0.16);
  const levels = [0, 0.9, 1.7, 2.45, 3.15, 3.8, 4.4, waist, H * 0.8, H * 0.9, H];
  const corners: Pt[] = [
    [1, 1],
    [1, -1],
    [-1, -1],
    [-1, 1],
  ];
  const steel: Segs = [];
  for (let l = 0; l < levels.length - 1; l++) {
    const [z0, z1] = [levels[l], levels[l + 1]];
    const [h0, h1] = [half(z0), half(z1)];
    for (let c = 0; c < 4; c++) {
      const [a, b] = corners[c];
      const [a2, b2] = corners[(c + 1) % 4];
      steel.push([at(a * h0, b * h0, z0), at(a * h1, b * h1, z1)]);
      steel.push([at(a * h0, b * h0, z0), at(a2 * h1, b2 * h1, z1)]);
      steel.push([at(a2 * h0, b2 * h0, z0), at(a * h1, b * h1, z1)]);
      steel.push([at(a * h1, b * h1, z1), at(a2 * h1, b2 * h1, z1)]);
    }
  }
  const insulators: Segs = [];
  for (const [z, w] of [
    [waist, 1.75],
    [H * 0.86, 1.3],
  ] as const) {
    const h = half(z);
    for (const s of [-1, 1]) {
      steel.push([at(h, s * h, z), at(0, s * w, z)]);
      steel.push([at(-h, s * h, z), at(0, s * w, z)]);
      steel.push([at(h, s * h, z - 0.45), at(0, s * w, z)]);
      steel.push([at(-h, s * h, z - 0.45), at(0, s * w, z)]);
      insulators.push([at(0, s * w, z), at(0, s * w, z - 0.62)]);
    }
  }
  for (const [a, b] of [
    [0.16, 0.16],
    [-0.16, -0.16],
    [0.16, -0.16],
  ])
    steel.push([at(a, b, H), at(0, 0, H + 0.55)]);
  return { steel, insulators };
}

function attachPoints(o: Obj, base: number): Vec3[] {
  const u = o.rot ?? [1, 0];
  const p: Pt = [-u[1], u[0]];
  const pts: Vec3[] = [];
  for (const [z, w] of [
    [TOWER_H * 0.68 - 0.62, 1.75],
    [TOWER_H * 0.86 - 0.62, 1.3],
  ] as const)
    for (const s of [-w, w]) pts.push([o.x + p[0] * s, o.y + p[1] * s, base + z]);
  return pts;
}

function mastSegments(o: Obj, base: number): Segs {
  const s: Segs = [];
  const H = MAST_H;
  const half = (z: number) => 0.42 * (1 - z / (H * 1.2));
  const corner = (k: number, z: number): Vec3 => {
    const ang = (k / 3) * Math.PI * 2 + 0.4;
    return [o.x + Math.cos(ang) * half(z), o.y + Math.sin(ang) * half(z), base + z];
  };
  const step = 0.7;
  for (let z = 0; z < H - 0.01; z += step)
    for (let k = 0; k < 3; k++) {
      s.push([corner(k, z), corner(k, z + step)]);
      s.push([corner(k, z), corner((k + 1) % 3, z + step)]);
      s.push([corner(k, z + step), corner((k + 1) % 3, z + step)]);
    }
  s.push([
    [o.x, o.y, base + H],
    [o.x, o.y, base + H + 1.3],
  ]);
  return s;
}

function wires(scene: Scene, line: Line, base: (o: Obj) => number): [Vec3, Vec3, number][] {
  const spans: [Vec3, Vec3, number][] = [];
  const towers = line.towers.map((k) => scene.objects[k]);
  if (line.kind === "transmission") {
    for (let k = 0; k < towers.length - 1; k++) {
      const pa = attachPoints(towers[k], base(towers[k]));
      const pb = attachPoints(towers[k + 1], base(towers[k + 1]));
      for (let w = 0; w < pa.length; w++) spans.push([pa[w], pb[w], 0.9]);
    }
  } else {
    for (let k = 0; k < towers.length - 1; k++)
      for (const off of [-0.3, 0, 0.3]) {
        const [ta, tb] = [towers[k], towers[k + 1]];
        spans.push([[ta.x, ta.y + off, base(ta) + 2.3], [tb.x, tb.y + off, base(tb) + 2.3], 0.25]);
      }
  }
  return spans;
}

function sagAt(a: Vec3, b: Vec3, depth: number, t: number): Vec3 {
  return [a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t, a[2] + (b[2] - a[2]) * t - depth * 4 * t * (1 - t)];
}

function houseSize(o: Obj) {
  const shelter = o.kind === "shelter";
  return {
    w: (shelter ? 0.42 : 0.6) * o.s,
    d: (shelter ? 0.3 : 0.42) * o.s,
    hgt: (shelter ? 0.42 : 0.55) * o.s,
    ridge: shelter ? 0.04 : 0.34 * o.s,
  };
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

function topOutline(scene: Scene, P: P): Pt[] {
  const { W, D } = scene;
  const h = (i: number, j: number) => scene.heights[i][j];
  const pts: Pt[] = [];
  for (let i = 0; i <= W; i++) pts.push(P([i, 0, h(i, 0)]));
  for (let j = 1; j <= D; j++) pts.push(P([W, j, h(W, j)]));
  for (let i = W - 1; i >= 0; i--) pts.push(P([i, D, h(i, D)]));
  for (let j = D - 1; j > 0; j--) pts.push(P([0, j, h(0, j)]));
  return pts;
}

// ---------- drawing: the land as it looks ----------

function drawSolid(ctx: CanvasRenderingContext2D, scene: Scene, P: P, k: number, px: number) {
  const { W, D, T, cells, plots, objects } = scene;
  const h = (i: number, j: number) => scene.heights[i][j];
  const base = (o: Obj) => hAt(scene, o.x, o.y);
  const S = 0.866 * k;

  // Soft contact shadow under the block (drawn off-canvas and cast back, so every browser blurs it).
  ctx.save();
  ctx.shadowColor = "rgba(70,46,30,0.38)";
  ctx.shadowBlur = 34 * px;
  ctx.shadowOffsetX = 20000;
  ctx.shadowOffsetY = 14 * px;
  ctx.translate(-20000, 0);
  poly(ctx, [P([0.4, 0.8, -T]), P([W + 0.4, 0.4, -T]), P([W + 0.6, D + 0.6, -T]), P([0.4, D + 0.4, -T])]);
  ctx.fill();
  ctx.restore();

  // The two visible sides of the block: layered soil.
  const side = (pts: Vec3[], dark: number) => {
    const s = pts.map(P);
    poly(ctx, s);
    const y0 = Math.min(...s.map((q) => q[1]));
    const y1 = Math.max(...s.map((q) => q[1]));
    const g = ctx.createLinearGradient(0, y0, 0, y1);
    const c = (r: number, gg: number, b: number) => `rgb(${Math.round(r * dark)},${Math.round(gg * dark)},${Math.round(b * dark)})`;
    g.addColorStop(0, c(88, 62, 44));
    g.addColorStop(0.2, c(118, 82, 55));
    g.addColorStop(1, c(72, 50, 36));
    ctx.fillStyle = g;
    ctx.fill();
  };
  const rightSide: Vec3[] = [];
  for (let j = 0; j <= D; j++) rightSide.push([W, j, h(W, j)]);
  rightSide.push([W, D, -T], [W, 0, -T]);
  side(rightSide, 0.76);
  const frontSide: Vec3[] = [];
  for (let i = 0; i <= W; i++) frontSide.push([i, D, h(i, D)]);
  frontSide.push([W, D, -T], [0, D, -T]);
  side(frontSide, 1.0);
  {
    // Stones, roots and strata in the soil.
    const r = rng(W * 13);
    ctx.save();
    poly(ctx, [P([0, D, h(0, D)]), P([W, D, h(W, D)]), P([W, 0, h(W, 0)]), P([W, 0, -T]), P([W, D, -T]), P([0, D, -T])]);
    ctx.clip();
    for (let n = 0; n < 480; n++) {
      const right = r() > 0.5;
      const t = r();
      const z = -T + r() * (T + 0.3);
      const q = P(right ? [W, t * D, z] : [t * W, D, z]);
      ctx.fillStyle = r() > 0.5 ? `rgba(40,28,20,${0.15 + r() * 0.3})` : `rgba(176,146,114,${0.15 + r() * 0.3})`;
      ctx.beginPath();
      ctx.ellipse(q[0], q[1], (0.6 + r() * 2.2) * px, (0.5 + r() * 1.4) * px, 0, 0, Math.PI * 2);
      ctx.fill();
    }
    ctx.strokeStyle = "rgba(40,26,18,0.22)";
    ctx.lineWidth = 0.8 * px;
    for (const f of [0.32, 0.64]) {
      const z = -T * f;
      const a = P([0, D, z]);
      const b = P([W, D, z]);
      const c = P([W, 0, z]);
      ctx.beginPath();
      ctx.moveTo(a[0], a[1]);
      ctx.lineTo(b[0], b[1]);
      ctx.lineTo(c[0], c[1]);
      ctx.stroke();
    }
    ctx.restore();
  }

  // The land: each cell filled with its crop texture laid in perspective. Textures sit on one plane so rows
  // run on across cells, and cells are grown by a hair so no seam shows between them.
  const U = TILE / 2; // a texture tile covers two cells, so the pattern repeats less
  const o0 = P([0, 0, 0]);
  const grow = (pts: Pt[], by: number): Pt[] => {
    const cx = pts.reduce((a, q) => a + q[0], 0) / pts.length;
    const cy = pts.reduce((a, q) => a + q[1], 0) / pts.length;
    return pts.map(([x, y]) => {
      const d = Math.hypot(x - cx, y - cy) || 1;
      return [x + ((x - cx) / d) * by, y + ((y - cy) / d) * by];
    });
  };
  const light = document.createElement("canvas");
  light.width = ctx.canvas.width;
  light.height = ctx.canvas.height;
  const lg = light.getContext("2d")!;
  for (let s = 0; s <= W + D - 2; s++)
    for (let i = Math.max(0, s - (D - 1)); i <= Math.min(W - 1, s); i++) {
      const j = s - i;
      const c = cells[i][j];
      const pts = grow(
        (
          [
            [i, j, h(i, j)],
            [i + 1, j, h(i + 1, j)],
            [i + 1, j + 1, h(i + 1, j + 1)],
            [i, j + 1, h(i, j + 1)],
          ] as Vec3[]
        ).map(P),
        0.9 * px,
      );
      const zc = (h(i, j) + h(i + 1, j) + h(i + 1, j + 1) + h(i, j + 1)) / 4;
      ctx.save();
      poly(ctx, pts);
      ctx.clip();
      ctx.setTransform(S / U, (0.5 * k) / U, -S / U, (0.5 * k) / U, o0[0], o0[1] - zc * ZS * k * 0.35);
      ctx.fillStyle = ctx.createPattern(tile(c.kind, c.rows), "repeat")!;
      ctx.fillRect(i * U - U, j * U - U, 3 * U, 3 * U);
      ctx.restore();
      // Sun on slopes, painted opaque on its own layer (grey = flat), then laid over once.
      const dzdx = (h(i + 1, j) + h(i + 1, j + 1) - h(i, j) - h(i, j + 1)) / 2;
      const dzdy = (h(i, j + 1) + h(i + 1, j + 1) - h(i, j) - h(i + 1, j)) / 2;
      const lit = Math.max(-1, Math.min(1, -dzdx * 1.1 - dzdy * 0.35));
      const v = Math.round(128 + lit * 90);
      poly(lg, pts);
      lg.fillStyle = `rgb(${v},${v},${v})`;
      lg.fill();
    }
  ctx.save();
  ctx.globalCompositeOperation = "overlay";
  ctx.globalAlpha = 0.55;
  ctx.drawImage(light, 0, 0);
  ctx.restore();

  // Each plot its own shade, as real fields never match.
  {
    const r = rng(scene.W * 71 + plots.length);
    for (const p of plots) {
      poly(ctx, plotOutline(scene, p).map(P));
      const v = r();
      ctx.fillStyle = v > 0.5 ? `rgba(255,240,190,${(v - 0.5) * 0.22})` : `rgba(30,22,10,${(0.5 - v) * 0.28})`;
      ctx.fill();
    }
  }

  // Bunds between plots: a thin grass edge with a soft shadow.
  for (const p of plots) {
    const pts = plotOutline(scene, p).map(P);
    ctx.strokeStyle = "rgba(40,26,14,0.26)";
    ctx.lineWidth = 2 * px;
    ctx.beginPath();
    pts.forEach((q, n) => (n ? ctx.lineTo(q[0], q[1] + 0.8 * px) : ctx.moveTo(q[0], q[1] + 0.8 * px)));
    ctx.closePath();
    ctx.stroke();
    ctx.strokeStyle = "rgba(150,152,90,0.7)";
    ctx.lineWidth = 1.1 * px;
    ctx.beginPath();
    pts.forEach((q, n) => (n ? ctx.lineTo(q[0], q[1]) : ctx.moveTo(q[0], q[1])));
    ctx.closePath();
    ctx.stroke();
  }

  // A lip of grass along the front edges of the block.
  ctx.strokeStyle = "rgba(82,98,48,0.95)";
  ctx.lineWidth = 2.2 * px;
  ctx.beginPath();
  for (let i = 0; i <= W; i++) {
    const q = P([i, D, h(i, D)]);
    if (i) ctx.lineTo(q[0], q[1]);
    else ctx.moveTo(q[0], q[1]);
  }
  for (let j = D; j >= 0; j--) {
    const q = P([W, j, h(W, j)]);
    ctx.lineTo(q[0], q[1]);
  }
  ctx.stroke();

  // Cast shadows on their own layer, then laid on the land.
  const sh = document.createElement("canvas");
  sh.width = ctx.canvas.width;
  sh.height = ctx.canvas.height;
  const sg = sh.getContext("2d")!;
  sg.lineCap = "round";
  sg.fillStyle = "#000";
  sg.strokeStyle = "#000";
  const ground = (v: Vec3, g: number): Vec3 => {
    const up = v[2] - g;
    return [v[0] + up * SUN[0], v[1] + up * SUN[1], g];
  };
  for (const o of objects) {
    const g = base(o);
    if (o.kind === "tower" || o.kind === "mast") {
      sg.lineWidth = (o.kind === "mast" ? 2 : 1.5) * px;
      const segs = o.kind === "tower" ? towerSegments(o, g).steel : mastSegments(o, g);
      strokeSegs(
        sg,
        segs.map(([a, b]) => [ground(a, g), ground(b, g)]),
        P,
      );
    } else if (o.kind === "pole") {
      sg.lineWidth = 2 * px;
      strokeSegs(sg, [[[o.x, o.y, g], ground([o.x, o.y, g + 2.45], g)]], P);
    } else if (o.kind === "house" || o.kind === "shelter") {
      const { w, d, hgt, ridge } = houseSize(o);
      const pts: Vec3[] = [
        [o.x - w, o.y - d, g],
        [o.x + w, o.y - d, g],
        [o.x + w, o.y + d, g],
        [o.x - w, o.y + d, g],
        ground([o.x - w, o.y - d, g + hgt], g),
        ground([o.x + w, o.y - d, g + hgt], g),
        ground([o.x + w, o.y + d, g + hgt], g),
        ground([o.x - w, o.y + d, g + hgt], g),
        ground([o.x + w, o.y, g + hgt + ridge], g),
        ground([o.x - w, o.y, g + hgt + ridge], g),
      ];
      hull(sg, pts.map(P));
      sg.fill();
    } else {
      const tall = o.kind === "eucalyptus" ? 2.3 * o.s : o.kind === "banana" ? 0.9 * o.s : 1.35 * o.s;
      const rad = (o.kind === "eucalyptus" ? 0.4 : o.kind === "banana" ? 0.45 : 0.55) * o.s;
      const c = P(ground([o.x, o.y, g + tall * 0.72], g));
      const a = P([o.x, o.y, g]);
      sg.lineWidth = 1.2 * px;
      sg.beginPath();
      sg.moveTo(a[0], a[1]);
      sg.lineTo(c[0], c[1]);
      sg.stroke();
      sg.beginPath();
      sg.ellipse(c[0], c[1], rad * S * 1.05, rad * k * 0.6, 0, 0, Math.PI * 2);
      sg.fill();
    }
  }
  sg.lineWidth = 0.8 * px;
  for (const line of scene.lines)
    for (const [a, b, depth] of wires(scene, line, base)) {
      sg.beginPath();
      for (let n = 0; n <= 24; n++) {
        const v = sagAt(a, b, depth, n / 24);
        const q = P(ground(v, hAt(scene, v[0], v[1])));
        if (n) sg.lineTo(q[0], q[1]);
        else sg.moveTo(q[0], q[1]);
      }
      sg.stroke();
    }
  ctx.save();
  poly(ctx, topOutline(scene, P));
  ctx.clip();
  ctx.globalAlpha = 0.34;
  ctx.drawImage(sh, 0, 0);
  ctx.restore();

  if (scene.fence) drawFence(ctx, scene, P, px, "solid");

  // Things standing on the land, back to front, with the wires last.
  const order = [...objects].sort((o, q) => o.x + o.y - (q.x + q.y));
  for (const o of order) drawSolidObject(ctx, o, base(o), P, k, px);
  ctx.strokeStyle = "rgba(34,30,28,0.8)";
  ctx.lineWidth = 0.75 * px;
  for (const line of scene.lines)
    for (const [a, b, depth] of wires(scene, line, base)) {
      ctx.beginPath();
      for (let n = 0; n <= 24; n++) {
        const q = P(sagAt(a, b, depth, n / 24));
        if (n) ctx.lineTo(q[0], q[1]);
        else ctx.moveTo(q[0], q[1]);
      }
      ctx.stroke();
    }
}

function hull(ctx: CanvasRenderingContext2D, pts: Pt[]) {
  const s = [...pts].sort((a, b) => a[0] - b[0] || a[1] - b[1]);
  const cross = (o: Pt, a: Pt, b: Pt) => (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0]);
  const half = (list: Pt[]) => {
    const out: Pt[] = [];
    for (const p of list) {
      while (out.length >= 2 && cross(out[out.length - 2], out[out.length - 1], p) <= 0) out.pop();
      out.push(p);
    }
    return out;
  };
  const lower = half(s);
  const upper = half([...s].reverse());
  poly(ctx, [...lower.slice(0, -1), ...upper.slice(0, -1)]);
}

function drawFence(ctx: CanvasRenderingContext2D, scene: Scene, P: P, px: number, mode: "solid" | "wire") {
  const f = scene.fence!;
  ctx.strokeStyle = mode === "solid" ? "rgba(80,80,78,0.8)" : "rgba(74,51,38,0.5)";
  ctx.lineWidth = 0.6 * px;
  for (let e = 0; e < 4; e++) {
    const [a, b] = [f[e], f[(e + 1) % 4]];
    const n = Math.ceil(Math.hypot(b[0] - a[0], b[1] - a[1]) / 0.4);
    const at = (k: number, lift: number) => {
      const x = a[0] + ((b[0] - a[0]) * k) / n;
      const y = a[1] + ((b[1] - a[1]) * k) / n;
      return P([x, y, hAt(scene, x, y) + lift]);
    };
    for (let k = 0; k <= n; k++) {
      const [p0, p1] = [at(k, 0), at(k, 0.4)];
      ctx.beginPath();
      ctx.moveTo(p0[0], p0[1]);
      ctx.lineTo(p1[0], p1[1]);
      ctx.stroke();
    }
    for (const lift of [0.2, 0.4]) {
      ctx.beginPath();
      for (let k = 0; k <= n; k++) {
        const q = at(k, lift);
        if (k) ctx.lineTo(q[0], q[1]);
        else ctx.moveTo(q[0], q[1]);
      }
      ctx.stroke();
    }
  }
}

function sphere(ctx: CanvasRenderingContext2D, x: number, y: number, rad: number, hue: number, sat: number, light: number) {
  const g = ctx.createRadialGradient(x - rad * 0.4, y - rad * 0.45, rad * 0.1, x, y, rad * 1.05);
  g.addColorStop(0, hsl(hue - 6, sat, light + 18));
  g.addColorStop(0.55, hsl(hue, sat, light));
  g.addColorStop(1, hsl(hue + 8, sat + 4, Math.max(6, light - 14)));
  ctx.fillStyle = g;
  ctx.beginPath();
  ctx.arc(x, y, rad, 0, Math.PI * 2);
  ctx.fill();
}

function face(ctx: CanvasRenderingContext2D, pts: Pt[], fill: string | CanvasGradient) {
  poly(ctx, pts);
  ctx.fillStyle = fill;
  ctx.fill();
}

function drawSolidObject(ctx: CanvasRenderingContext2D, o: Obj, z: number, P: P, k: number, px: number) {
  const r = rng(o.seed);
  const unit = 0.866 * k;
  if (o.kind === "banana") {
    const g0 = P([o.x, o.y, z]);
    const top = P([o.x, o.y, z + 0.75 * o.s]);
    ctx.strokeStyle = hsl(60, 25, 32);
    ctx.lineWidth = 2 * px;
    ctx.beginPath();
    ctx.moveTo(g0[0], g0[1]);
    ctx.lineTo(top[0], top[1]);
    ctx.stroke();
    for (let l = 0; l < 8; l++) {
      const a = (l / 8) * Math.PI * 2 + r();
      const len = (0.55 + r() * 0.25) * o.s * unit;
      const tip: Pt = [top[0] + Math.cos(a) * len, top[1] + Math.sin(a) * len * 0.55 + len * 0.25];
      const g = ctx.createLinearGradient(top[0], top[1], tip[0], tip[1]);
      g.addColorStop(0, hsl(84, 42, 42));
      g.addColorStop(1, hsl(76, 40, 26 + r() * 10));
      const mid: Pt = [(top[0] + tip[0]) / 2, (top[1] + tip[1]) / 2 - len * 0.18];
      const nx = -Math.sin(a) * len * 0.16;
      const ny = Math.cos(a) * len * 0.1;
      ctx.fillStyle = g;
      ctx.beginPath();
      ctx.moveTo(top[0], top[1]);
      ctx.quadraticCurveTo(mid[0] + nx, mid[1] + ny, tip[0], tip[1]);
      ctx.quadraticCurveTo(mid[0] - nx, mid[1] - ny, top[0], top[1]);
      ctx.fill();
    }
    return;
  }
  if (o.kind === "tree" || o.kind === "eucalyptus") {
    const euc = o.kind === "eucalyptus";
    const g0 = P([o.x, o.y, z]);
    const tall = (euc ? 2.3 : 1.35) * o.s;
    const rad = (euc ? 0.36 : 0.52) * o.s * unit;
    const trunkTop = P([o.x, o.y, z + tall * (euc ? 0.5 : 0.38)]);
    ctx.strokeStyle = euc ? hsl(30, 12, 64) : hsl(25, 30, 24);
    ctx.lineWidth = (euc ? 1.7 : 2.1) * px;
    ctx.beginPath();
    ctx.moveTo(g0[0], g0[1]);
    ctx.lineTo(trunkTop[0], trunkTop[1]);
    ctx.stroke();
    const c = P([o.x, o.y, z + tall * 0.72]);
    const [hue, sat, light] = euc ? [96, 17, 35] : [88, 34, 27];
    const blobs: [number, number, number][] = [];
    for (let b = 0; b < (euc ? 8 : 10); b++) {
      const a = r() * Math.PI * 2;
      const d = r() * rad * 0.6;
      blobs.push([c[0] + Math.cos(a) * d, c[1] + Math.sin(a) * d * (euc ? 2.1 : 0.85), rad * (0.48 + r() * 0.35)]);
    }
    blobs.sort((p, q) => p[1] - q[1]);
    for (const [x, y, rr] of blobs) sphere(ctx, x, y, rr, hue + r() * 8, sat, light + r() * 6);
    for (let n = 0; n < 30; n++) {
      ctx.fillStyle = r() > 0.5 ? hsl(hue - 10, sat + 10, light + 26, 0.5) : hsl(hue + 10, sat, light - 10, 0.5);
      ctx.beginPath();
      ctx.arc(c[0] + (r() - 0.5) * rad * 1.5, c[1] + (r() - 0.5) * rad * (euc ? 2.2 : 1.2), (0.7 + r()) * px, 0, Math.PI * 2);
      ctx.fill();
    }
    return;
  }
  if (o.kind === "house" || o.kind === "shelter") {
    const { w, d, hgt, ridge } = houseSize(o);
    const shelter = o.kind === "shelter";
    const [x0, x1, y0, y1] = [o.x - w, o.x + w, o.y - d, o.y + d];
    face(
      ctx,
      (
        [
          [x0, y1, z],
          [x1, y1, z],
          [x1, y1, z + hgt],
          [x0, y1, z + hgt],
        ] as Vec3[]
      ).map(P),
      shelter ? "#e6e3dc" : "#e4d4ba",
    );
    face(
      ctx,
      (
        [
          [x1, y0, z],
          [x1, y1, z],
          [x1, y1, z + hgt],
          [x1, o.y, z + hgt + ridge],
          [x1, y0, z + hgt],
        ] as Vec3[]
      ).map(P),
      shelter ? "#b4b0a8" : "#b39c7e",
    );
    // Windows and a door on the sunny wall.
    const opening = (u: number, bottom: number, top: number, col: string) => {
      const xa = x0 + (x1 - x0) * u - 0.07;
      const xb = xa + 0.14;
      face(
        ctx,
        (
          [
            [xa, y1, z + hgt * bottom],
            [xb, y1, z + hgt * bottom],
            [xb, y1, z + hgt * top],
            [xa, y1, z + hgt * top],
          ] as Vec3[]
        ).map(P),
        col,
      );
    };
    if (shelter) opening(0.5, 0, 0.72, "#8d8b86");
    else {
      opening(0.5, 0, 0.7, "#4a3326");
      opening(0.2, 0.42, 0.74, "#3a434b");
      opening(0.8, 0.42, 0.74, "#3a434b");
    }
    // The roof slope toward us, corrugated, with a bright ridge.
    const roofPts = (
      [
        [x0 - 0.06, y1 + 0.08, z + hgt - 0.03],
        [x1 + 0.06, y1 + 0.08, z + hgt - 0.03],
        [x1 + 0.06, o.y, z + hgt + ridge],
        [x0 - 0.06, o.y, z + hgt + ridge],
      ] as Vec3[]
    ).map(P);
    const roof = o.roof ?? (shelter ? "#d9d7d1" : "#a9a6a0");
    const g = ctx.createLinearGradient(roofPts[3][0], roofPts[3][1], roofPts[0][0], roofPts[0][1]);
    g.addColorStop(0, shade(roof, 1.08));
    g.addColorStop(1, shade(roof, 0.84));
    face(ctx, roofPts, g);
    ctx.strokeStyle = "rgba(0,0,0,0.12)";
    ctx.lineWidth = 0.5 * px;
    for (let n = 1; n < 10; n++) {
      const t = n / 10;
      const x = x0 - 0.06 + (x1 - x0 + 0.12) * t;
      const a = P([x, y1 + 0.08, z + hgt - 0.03]);
      const b = P([x, o.y, z + hgt + ridge]);
      ctx.beginPath();
      ctx.moveTo(a[0], a[1]);
      ctx.lineTo(b[0], b[1]);
      ctx.stroke();
    }
    ctx.strokeStyle = "rgba(255,255,255,0.4)";
    ctx.beginPath();
    ctx.moveTo(roofPts[3][0], roofPts[3][1]);
    ctx.lineTo(roofPts[2][0], roofPts[2][1]);
    ctx.stroke();
    return;
  }
  if (o.kind === "tower") {
    const { steel, insulators } = towerSegments(o, z);
    // Galvanised steel: a dark line with a sunlit edge.
    ctx.strokeStyle = "#565b60";
    ctx.lineWidth = 1.3 * px;
    strokeSegs(ctx, steel, P);
    ctx.strokeStyle = "rgba(238,240,238,0.85)";
    ctx.lineWidth = 0.55 * px;
    strokeSegs(ctx, steel, P, -0.45 * px, -0.3 * px);
    // Insulator strings: stacks of glass discs.
    for (const [a, b] of insulators)
      for (let n = 0; n <= 5; n++) {
        const q = P([a[0], a[1], a[2] + ((b[2] - a[2]) * n) / 5]);
        ctx.fillStyle = "#3b464e";
        ctx.beginPath();
        ctx.ellipse(q[0], q[1], 2 * px, 0.85 * px, 0, 0, Math.PI * 2);
        ctx.fill();
        ctx.fillStyle = "rgba(196,220,230,0.65)";
        ctx.beginPath();
        ctx.ellipse(q[0] - 0.6 * px, q[1] - 0.3 * px, 0.8 * px, 0.35 * px, 0, 0, Math.PI * 2);
        ctx.fill();
      }
    // Concrete footings.
    const u = o.rot ?? [1, 0];
    for (const [a, b] of [
      [1, 1],
      [1, -1],
      [-1, -1],
      [-1, 1],
    ]) {
      const q = P([o.x + (u[0] * a - u[1] * b) * 0.62, o.y + (u[1] * a + u[0] * b) * 0.62, z]);
      ctx.fillStyle = "#cfc9be";
      ctx.beginPath();
      ctx.ellipse(q[0], q[1], 2.5 * px, 1.25 * px, 0, 0, Math.PI * 2);
      ctx.fill();
    }
    return;
  }
  if (o.kind === "mast") {
    ctx.lineWidth = 1.5 * px;
    for (const [a, b] of mastSegments(o, z)) {
      const band = Math.floor((a[2] - z) / 1.4) % 2 === 0;
      ctx.strokeStyle = band ? "#b8432c" : "#f2eee7";
      const pa = P(a);
      const pb = P(b);
      ctx.beginPath();
      ctx.moveTo(pa[0], pa[1]);
      ctx.lineTo(pb[0], pb[1]);
      ctx.stroke();
    }
    // Antenna panels near the top, and two dishes.
    for (let n = 0; n < 3; n++) {
      const ang = (n / 3) * Math.PI * 2 + 0.4;
      const ax = o.x + Math.cos(ang) * 0.44;
      const ay = o.y + Math.sin(ang) * 0.44;
      const a = P([ax, ay, z + MAST_H - 1.7]);
      const b = P([ax, ay, z + MAST_H - 0.5]);
      ctx.strokeStyle = "#efece5";
      ctx.lineWidth = 3.4 * px;
      ctx.beginPath();
      ctx.moveTo(a[0], a[1]);
      ctx.lineTo(b[0], b[1]);
      ctx.stroke();
      ctx.strokeStyle = "rgba(0,0,0,0.22)";
      ctx.lineWidth = 0.7 * px;
      ctx.beginPath();
      ctx.moveTo(a[0] + 1.6 * px, a[1]);
      ctx.lineTo(b[0] + 1.6 * px, b[1]);
      ctx.stroke();
    }
    for (const [dz, off, size] of [
      [MAST_H * 0.62, 0.55, 0.62],
      [MAST_H * 0.47, -0.5, 0.46],
    ] as const) {
      const q = P([o.x + off, o.y + Math.abs(off), z + dz]);
      const g = ctx.createRadialGradient(q[0] - 2 * px, q[1] - 2 * px, 1, q[0], q[1], size * unit);
      g.addColorStop(0, "#ffffff");
      g.addColorStop(1, "#bab5ab");
      ctx.fillStyle = g;
      ctx.beginPath();
      ctx.ellipse(q[0], q[1], size * unit * 0.62, size * unit * 0.8, -0.35, 0, Math.PI * 2);
      ctx.fill();
      ctx.strokeStyle = "rgba(0,0,0,0.18)";
      ctx.lineWidth = 0.6 * px;
      ctx.stroke();
    }
    const top = P([o.x, o.y, z + MAST_H + 1.3]);
    ctx.fillStyle = "#e0523a";
    ctx.beginPath();
    ctx.arc(top[0], top[1], 1.7 * px, 0, Math.PI * 2);
    ctx.fill();
    return;
  }
  if (o.kind === "pole") {
    const g0 = P([o.x, o.y, z]);
    const top = P([o.x, o.y, z + 2.45]);
    ctx.strokeStyle = "#8f8a82";
    ctx.lineWidth = 2.2 * px;
    ctx.beginPath();
    ctx.moveTo(g0[0], g0[1]);
    ctx.lineTo(top[0], top[1]);
    ctx.stroke();
    ctx.strokeStyle = "rgba(255,255,255,0.5)";
    ctx.lineWidth = 0.7 * px;
    ctx.beginPath();
    ctx.moveTo(g0[0] - 0.6 * px, g0[1]);
    ctx.lineTo(top[0] - 0.6 * px, top[1]);
    ctx.stroke();
    const a = P([o.x, o.y - 0.4, z + 2.3]);
    const b = P([o.x, o.y + 0.4, z + 2.3]);
    ctx.strokeStyle = "#6d6862";
    ctx.lineWidth = 1.4 * px;
    ctx.beginPath();
    ctx.moveTo(a[0], a[1]);
    ctx.lineTo(b[0], b[1]);
    ctx.stroke();
  }
}

function shade(hex: string, k: number): string {
  const n = Number.parseInt(hex.slice(1), 16);
  const c = (v: number) => Math.min(255, Math.max(0, Math.round(v * k)));
  return `rgb(${c((n >> 16) & 255)},${c((n >> 8) & 255)},${c(n & 255)})`;
}

// ---------- drawing: the land as the drone reads it ----------

function drawWire(ctx: CanvasRenderingContext2D, scene: Scene, P: P, k: number, px: number) {
  const { W, D, T, cells, plots, objects } = scene;
  const h = (i: number, j: number) => scene.heights[i][j];
  const base = (o: Obj) => hAt(scene, o.x, o.y);
  const paper = "rgba(250,247,242,0.94)";
  const ink = (a: number) => `rgba(74,51,38,${a})`;

  for (let n = 0; n < W; n++)
    for (const f of [
      [
        [W, n, h(W, n)],
        [W, n + 1, h(W, n + 1)],
        [W, n + 1, -T],
        [W, n, -T],
      ],
      [
        [n, D, h(n, D)],
        [n + 1, D, h(n + 1, D)],
        [n + 1, D, -T],
        [n, D, -T],
      ],
    ] as Vec3[][]) {
      poly(ctx, f.map(P));
      ctx.fillStyle = paper;
      ctx.fill();
      ctx.strokeStyle = ink(0.2);
      ctx.lineWidth = 0.6 * px;
      ctx.stroke();
    }

  for (let s = 0; s <= W + D - 2; s++)
    for (let i = Math.max(0, s - (D - 1)); i <= Math.min(W - 1, s); i++) {
      const j = s - i;
      const c = cells[i][j];
      const pts = (
        [
          [i, j, h(i, j)],
          [i + 1, j, h(i + 1, j)],
          [i + 1, j + 1, h(i + 1, j + 1)],
          [i, j + 1, h(i, j + 1)],
        ] as Vec3[]
      ).map(P);
      poly(ctx, pts);
      ctx.fillStyle = c.kind === "road" ? "rgba(236,228,216,0.96)" : c.kind === "water" ? "rgba(226,232,232,0.96)" : paper;
      ctx.fill();
      ctx.strokeStyle = ink(0.08);
      ctx.lineWidth = 0.5 * px;
      ctx.stroke();
      if (["maize", "cassava", "beans", "rice", "tea", "banana"].includes(c.kind)) {
        // A mark for each plant the drone counts.
        ctx.fillStyle = ink(0.34);
        const n = c.kind === "cassava" || c.kind === "banana" ? 3 : 4;
        for (let a = 0; a < n; a++)
          for (let b = 0; b < n; b++) {
            const x = i + (a + 0.5) / n;
            const y = j + (b + 0.5) / n;
            const q = P([x, y, hAt(scene, x, y)]);
            ctx.beginPath();
            ctx.arc(q[0], q[1], 0.7 * px, 0, Math.PI * 2);
            ctx.fill();
          }
      }
    }
  ctx.strokeStyle = ink(0.55);
  ctx.lineWidth = 0.9 * px;
  for (const p of plots) {
    poly(ctx, plotOutline(scene, p).map(P));
    ctx.stroke();
  }
  if (scene.fence) drawFence(ctx, scene, P, px, "wire");

  const order = [...objects].sort((o, q) => o.x + o.y - (q.x + q.y));
  const unit = 0.866 * k;
  for (const o of order) {
    const z = base(o);
    ctx.strokeStyle = ink(0.62);
    ctx.lineWidth = 0.7 * px;
    if (o.kind === "tower" || o.kind === "mast") {
      ctx.strokeStyle = "rgba(26,19,16,0.72)";
      if (o.kind === "tower") {
        const { steel, insulators } = towerSegments(o, z);
        strokeSegs(ctx, steel, P);
        ctx.lineWidth = 1.6 * px;
        strokeSegs(ctx, insulators, P);
      } else strokeSegs(ctx, mastSegments(o, z), P);
    } else if (o.kind === "house" || o.kind === "shelter") {
      const { w, d, hgt, ridge } = houseSize(o);
      const faces: Vec3[][] = [
        [
          [o.x - w, o.y + d, z],
          [o.x + w, o.y + d, z],
          [o.x + w, o.y + d, z + hgt],
          [o.x - w, o.y + d, z + hgt],
        ],
        [
          [o.x + w, o.y - d, z],
          [o.x + w, o.y + d, z],
          [o.x + w, o.y + d, z + hgt],
          [o.x + w, o.y, z + hgt + ridge],
          [o.x + w, o.y - d, z + hgt],
        ],
        [
          [o.x - w, o.y + d, z + hgt],
          [o.x + w, o.y + d, z + hgt],
          [o.x + w, o.y, z + hgt + ridge],
          [o.x - w, o.y, z + hgt + ridge],
        ],
      ];
      for (const f of faces) {
        poly(ctx, f.map(P));
        ctx.fillStyle = paper;
        ctx.fill();
        ctx.stroke();
      }
    } else if (o.kind === "pole") strokeSegs(ctx, [[[o.x, o.y, z], [o.x, o.y, z + 2.45]]], P);
    else {
      const euc = o.kind === "eucalyptus";
      const tall = (euc ? 2.3 : o.kind === "banana" ? 0.75 : 1.35) * o.s;
      const top = P([o.x, o.y, z + tall * 0.72]);
      const g = P([o.x, o.y, z]);
      const rad = (euc ? 0.36 : 0.5) * o.s * unit;
      ctx.beginPath();
      ctx.moveTo(g[0], g[1]);
      ctx.lineTo(top[0], top[1]);
      ctx.stroke();
      ctx.beginPath();
      ctx.ellipse(top[0], top[1], rad, rad * (euc ? 1.6 : 0.9), 0, 0, Math.PI * 2);
      ctx.fillStyle = paper;
      ctx.fill();
      ctx.stroke();
      ctx.beginPath();
      ctx.ellipse(g[0], g[1], rad * 0.8, rad * 0.4, 0, 0, Math.PI * 2);
      ctx.stroke();
    }
  }
  ctx.strokeStyle = "rgba(26,19,16,0.62)";
  ctx.lineWidth = 0.7 * px;
  for (const l of scene.lines)
    for (const [a, b, depth] of wires(scene, l, base)) {
      ctx.beginPath();
      for (let n = 0; n <= 24; n++) {
        const q = P(sagAt(a, b, depth, n / 24));
        if (n) ctx.lineTo(q[0], q[1]);
        else ctx.moveTo(q[0], q[1]);
      }
      ctx.stroke();
    }
}

// ---------- camera: tilt-shift focus, warm grade, grain ----------

function finish(c: HTMLCanvasElement, focusY: number, spread: number) {
  const ctx = c.getContext("2d")!;
  const W = c.width;
  const H = c.height;
  // Keep the world's silhouette so the grade and grain stay inside it.
  const shape = document.createElement("canvas");
  shape.width = W;
  shape.height = H;
  shape.getContext("2d")!.drawImage(c, 0, 0);
  // Shallow focus: a soft copy (scaled down and back up, which works in every browser) shows through
  // above and below the band in focus.
  const small = document.createElement("canvas");
  small.width = Math.max(1, Math.round(W / 5));
  small.height = Math.max(1, Math.round(H / 5));
  const sm = small.getContext("2d")!;
  sm.imageSmoothingQuality = "high";
  sm.drawImage(c, 0, 0, small.width, small.height);
  const soft = document.createElement("canvas");
  soft.width = W;
  soft.height = H;
  const so = soft.getContext("2d")!;
  so.imageSmoothingQuality = "high";
  so.drawImage(small, 0, 0, W, H);
  so.globalCompositeOperation = "destination-in";
  const f0 = Math.max(0.05, focusY - spread);
  const f1 = Math.min(0.95, focusY + spread);
  const g = so.createLinearGradient(0, 0, 0, H);
  g.addColorStop(0, "rgba(0,0,0,0.95)");
  g.addColorStop(Math.max(0, f0 - 0.2), "rgba(0,0,0,0.75)");
  g.addColorStop(f0, "rgba(0,0,0,0)");
  g.addColorStop(f1, "rgba(0,0,0,0)");
  g.addColorStop(Math.min(1, f1 + 0.2), "rgba(0,0,0,0.8)");
  g.addColorStop(1, "rgba(0,0,0,0.95)");
  so.fillStyle = g;
  so.fillRect(0, 0, W, H);
  ctx.drawImage(soft, 0, 0);
  // A warm late-afternoon grade and film grain.
  ctx.save();
  ctx.globalCompositeOperation = "soft-light";
  const warm = ctx.createLinearGradient(0, 0, W, H);
  warm.addColorStop(0, "rgba(255,212,156,0.6)");
  warm.addColorStop(1, "rgba(150,110,80,0.22)");
  ctx.globalAlpha = 0.45;
  ctx.fillStyle = warm;
  ctx.fillRect(0, 0, W, H);
  ctx.globalCompositeOperation = "overlay";
  ctx.globalAlpha = 0.1;
  ctx.fillStyle = ctx.createPattern(grain(), "repeat")!;
  ctx.fillRect(0, 0, W, H);
  ctx.globalCompositeOperation = "destination-in";
  ctx.globalAlpha = 1;
  ctx.drawImage(shape, 0, 0);
  ctx.restore();
}

// ---------- public: render a world into two canvases ----------

export function renderWorld(scene: Scene, cssW: number, cssH: number, dpr: number, padding = 0.06): Rendered {
  const W = Math.round(cssW * dpr);
  const H = Math.round(cssH * dpr);
  const corners: Vec3[] = [];
  for (const [x, y] of [
    [0, 0],
    [scene.W, 0],
    [scene.W, scene.D],
    [0, scene.D],
  ] as const)
    corners.push([x, y, -scene.T - 0.3], [x, y, hAt(scene, x, y) + 0.6]);
  for (const o of scene.objects) {
    const tall = o.kind === "tower" ? TOWER_H + 0.8 : o.kind === "mast" ? MAST_H + 1.6 : o.kind === "eucalyptus" ? 2.6 : 1.2;
    corners.push([o.x, o.y, hAt(scene, o.x, o.y) + tall]);
  }
  if (scene.corridor) {
    const { a, b, z } = scene.corridor;
    for (const t of [0.2, 0.5, 0.8]) {
      const x = a[0] + (b[0] - a[0]) * t;
      const y = a[1] + (b[1] - a[1]) * t;
      corners.push([x, y, hAt(scene, x, y) + z + 2.2]);
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
  const px = dpr * Math.max(0.6, Math.min(1.35, k / (34 * dpr)));

  const solid = document.createElement("canvas");
  solid.width = W;
  solid.height = H;
  const sctx = solid.getContext("2d")!;
  sctx.lineJoin = "round";
  sctx.lineCap = "round";
  drawSolid(sctx, scene, project, k, px);
  const center = project([scene.W / 2, scene.D / 2, 0])[1] / H;
  finish(solid, center, 0.14);

  const wire = document.createElement("canvas");
  wire.width = W;
  wire.height = H;
  const wctx = wire.getContext("2d")!;
  wctx.lineJoin = "round";
  wctx.lineCap = "round";
  drawWire(wctx, scene, project, k, px);

  const boxes: Rendered["boxes"] = [];
  for (const d of scene.detections) {
    let pts: Pt[] = [];
    let polyPts: Pt[] | undefined;
    if (d.plot !== undefined) {
      polyPts = plotOutline(scene, scene.plots[d.plot]).map(project);
      pts = polyPts;
    } else if (d.obj !== undefined) {
      const o = scene.objects[d.obj];
      const z = hAt(scene, o.x, o.y);
      if (o.kind === "tower") {
        const u = o.rot ?? [1, 0];
        const zz = z + TOWER_H * 0.68;
        for (const s of [-1.75, 1.75]) pts.push(project([o.x - u[1] * s, o.y + u[0] * s, zz - 0.8]), project([o.x - u[1] * s, o.y + u[0] * s, zz + 0.3]));
      } else if (o.kind === "mast") pts = [project([o.x - 0.6, o.y, z + MAST_H - 1.9]), project([o.x + 0.6, o.y + 0.6, z + MAST_H - 0.2])];
      else {
        const tall = 2.4 * o.s;
        pts = [project([o.x - 0.45, o.y, z]), project([o.x + 0.45, o.y, z + tall]), project([o.x, o.y - 0.45, z + tall]), project([o.x, o.y + 0.45, z])];
      }
    }
    if (!pts.length) continue;
    const xs = pts.map((p) => p[0]);
    const ys = pts.map((p) => p[1]);
    const x = Math.min(...xs);
    const y = Math.min(...ys);
    boxes.push({ x, y, w: Math.max(...xs) - x, h: Math.max(...ys) - y, label: d.label, unsure: !!d.unsure, poly: polyPts });
  }
  boxes.sort((a, b) => a.x - b.x);
  return { solid, wire, project, boxes, scene, bounds: { x0: minX * k + ox, x1: maxX * k + ox, y0: minY * k + oy, y1: maxY * k + oy } };
}
