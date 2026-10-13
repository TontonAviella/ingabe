"use client";

import { useEffect, useRef } from "react";
import { buildScene, hAt, renderWorld, type Preset, type Rendered } from "@/lib/miniworld";
import { useMotion } from "./MotionProvider";

const CYCLE = 15; // seconds: hold, sweep, hold, return
const SWEEP_FROM = 1.2;
const SWEEP_TO = 9.6;
const BACK_FROM = 13.2;

const ease = (t: number) => (t < 0.5 ? 4 * t * t * t : 1 - (-2 * t + 2) ** 3 / 2);

/** The hero diorama: a scan line turns the land into what the drone reads, and boxes what it found. */
export function HeroWorld({ className = "" }: { className?: string }) {
  const canvas = useRef<HTMLCanvasElement>(null);
  const { paused, reduced } = useMotion();
  const state = useRef({ paused, reduced });
  state.current = { paused, reduced };

  useEffect(() => {
    const el = canvas.current;
    if (!el) return;
    // Three worlds take turns, like a reel: farms with a line and a mast, a line corridor, terraced hills.
    const scenes = [buildScene("hero", 11), buildScene("corridor", 8), buildScene("hills", 3)];
    let worlds: (Rendered | null)[] = scenes.map(() => null);
    let pending: ReturnType<typeof setTimeout> | undefined;
    let raf = 0;
    let visible = true;
    let clock = 0;
    let last = performance.now();
    const dpr = Math.min(2, window.devicePixelRatio || 1);

    const resize = () => {
      const r = el.getBoundingClientRect();
      if (r.width < 10) return;
      el.width = Math.round(r.width * dpr);
      el.height = Math.round(r.height * dpr);
      worlds = scenes.map(() => null);
      worlds[0] = renderWorld(scenes[0], r.width, r.height, dpr, 0.04);
      draw();
      // The other worlds are drawn a moment later, one at a time, so the page stays responsive.
      clearTimeout(pending);
      const next = (n: number) => {
        if (n >= scenes.length) return;
        pending = setTimeout(() => {
          worlds[n] = renderWorld(scenes[n], r.width, r.height, dpr, 0.04);
          next(n + 1);
        }, 400);
      };
      next(1);
    };

    const draw = () => {
      const ready = worlds.filter(Boolean).length;
      const idx = state.current.reduced ? 0 : Math.floor(clock / CYCLE) % Math.max(1, ready);
      const world = worlds[idx] ?? worlds[0];
      if (!world) return;
      const upcoming = worlds[(idx + 1) % Math.max(1, ready)] ?? world;
      const scene = world.scene;
      const ctx = el.getContext("2d")!;
      const { solid, wire, bounds, boxes, project } = world;
      const W = el.width;
      const H = el.height;
      ctx.clearRect(0, 0, W, H);
      const t = state.current.reduced ? 7 : clock % CYCLE;
      const sweepT = state.current.reduced ? 0.58 : t < SWEEP_FROM ? 0 : t > SWEEP_TO ? 1 : ease((t - SWEEP_FROM) / (SWEEP_TO - SWEEP_FROM));
      const back = t > BACK_FROM ? Math.min(1, (t - BACK_FROM) / (CYCLE - BACK_FROM)) : 0;
      const x0 = bounds.x0 - 4 * dpr;
      const x1 = bounds.x1 + 4 * dpr;
      const sx = x0 + (x1 - x0) * sweepT;

      // Read part (wire) on the left of the scan line, the land as it looks on the right.
      ctx.save();
      ctx.beginPath();
      ctx.rect(0, 0, sx, H);
      ctx.clip();
      ctx.drawImage(wire, 0, 0);
      ctx.restore();
      ctx.save();
      ctx.beginPath();
      ctx.rect(sx, 0, W - sx, H);
      ctx.clip();
      ctx.drawImage(solid, 0, 0);
      ctx.restore();
      if (back > 0) {
        // Cross-fade into the next world as it looks.
        ctx.globalAlpha = ease(back);
        ctx.drawImage(upcoming.solid, 0, 0);
        ctx.globalAlpha = 1;
      }

      // The scan line.
      if (sweepT > 0.01 && sweepT < 0.99 && back === 0) {
        // Brightest mid-sweep, fading in and out at the block's edges.
        ctx.globalAlpha = Math.min(1, Math.sin(sweepT * Math.PI) * 2.2);
        const g = ctx.createLinearGradient(sx - 70 * dpr, 0, sx, 0);
        g.addColorStop(0, "rgba(217,160,102,0)");
        g.addColorStop(1, "rgba(217,160,102,0.09)");
        ctx.fillStyle = g;
        ctx.fillRect(sx - 70 * dpr, bounds.y0, 70 * dpr, bounds.y1 - bounds.y0);
        const v = ctx.createLinearGradient(0, bounds.y0, 0, bounds.y1);
        v.addColorStop(0, "rgba(217,160,102,0)");
        v.addColorStop(0.3, "rgba(217,160,102,0.95)");
        v.addColorStop(0.75, "rgba(217,160,102,0.95)");
        v.addColorStop(1, "rgba(217,160,102,0)");
        ctx.fillStyle = v;
        ctx.fillRect(sx - 0.75 * dpr, bounds.y0, 1.5 * dpr, bounds.y1 - bounds.y0);
        ctx.globalAlpha = 1;
      }

      // What the drone found, once the line has passed it.
      const fade = 1 - (back > 0 ? ease(back) : 0);
      const placed: Chip[] = [];
      const size = Math.max(7.5, Math.min(9.5, W / dpr / 80));
      for (const b of boxes) {
        const passed = sx - (b.x + b.w * 0.5);
        if (passed <= 0 || fade <= 0) continue;
        const a = Math.min(1, passed / (40 * dpr)) * fade;
        drawBox(ctx, b, a, dpr, size, placed);
      }

      // The drone flies along the line, a little ahead of the scan.
      const c = scene.corridor;
      if (c && sweepT > 0 && sweepT < 1 && back === 0) {
        const pa = project([c.a[0], c.a[1], 0]);
        const pb = project([c.b[0], c.b[1], 0]);
        const tt = Math.max(0, Math.min(1, (sx + 26 * dpr - pa[0]) / (pb[0] - pa[0])));
        const x = c.a[0] + (c.b[0] - c.a[0]) * tt;
        const y = c.a[1] + (c.b[1] - c.a[1]) * tt;
        const g = hAt(scene, x, y);
        drawDrone(ctx, project, x, y, g, g + c.z + 0.6 + Math.sin(clock * 2.2) * 0.12, clock, dpr);
      }
    };

    const tick = (now: number) => {
      const dt = Math.min(0.05, (now - last) / 1000);
      last = now;
      if (visible && !state.current.paused && !state.current.reduced) {
        clock += dt;
        draw();
      }
      raf = requestAnimationFrame(tick);
    };

    const ro = new ResizeObserver(resize);
    ro.observe(el);
    const io = new IntersectionObserver(([e]) => (visible = e.isIntersecting), { threshold: 0.01 });
    io.observe(el);
    resize();
    raf = requestAnimationFrame(tick);
    return () => {
      cancelAnimationFrame(raf);
      clearTimeout(pending);
      ro.disconnect();
      io.disconnect();
    };
  }, []);

  return <canvas ref={canvas} className={className} aria-label="A drone reads a hillside of farm plots and a power line" role="img" />;
}

type Chip = { x: number; y: number; w: number; h: number };

function drawBox(ctx: CanvasRenderingContext2D, b: Rendered["boxes"][number], alpha: number, dpr: number, size: number, placed: Chip[]) {
  ctx.save();
  ctx.globalAlpha = alpha;
  ctx.strokeStyle = "#D9A066";
  ctx.lineWidth = 1.4 * dpr;
  if (b.unsure) ctx.setLineDash([4 * dpr, 3 * dpr]);
  if (b.poly) {
    ctx.beginPath();
    b.poly.forEach((p, k) => (k ? ctx.lineTo(p[0], p[1]) : ctx.moveTo(p[0], p[1])));
    ctx.closePath();
    ctx.fillStyle = b.unsure ? "rgba(217,160,102,0.08)" : "rgba(217,160,102,0.18)";
    ctx.fill();
    ctx.stroke();
  } else {
    const pad = 4 * dpr;
    ctx.strokeRect(b.x - pad, b.y - pad, b.w + 2 * pad, b.h + 2 * pad);
  }
  ctx.setLineDash([]);
  // Label chip above the box, moved up when it would cover another label.
  const text = b.label.toUpperCase();
  ctx.font = `500 ${size * dpr}px ui-monospace, SFMono-Regular, Menlo, monospace`;
  const tw = ctx.measureText(text).width;
  const ch = size * 1.8 * dpr;
  const cw = tw + size * 1.5 * dpr;
  const lx = b.x + b.w / 2 - cw / 2;
  let ly = b.y - ch - size * dpr;
  const hits = (y: number) => placed.some((p) => lx < p.x + p.w + 4 * dpr && lx + cw + 4 * dpr > p.x && y < p.y + p.h + 3 * dpr && y + ch + 3 * dpr > p.y);
  for (let k = 0; k < 6 && hits(ly); k++) ly -= ch + 4 * dpr;
  placed.push({ x: lx, y: ly, w: cw, h: ch });
  ctx.fillStyle = "rgba(26,19,16,0.92)";
  roundRect(ctx, lx, ly, cw, ch, 3 * dpr);
  ctx.fill();
  ctx.fillStyle = b.unsure ? "#E9D9C6" : "#D9A066";
  ctx.fillText(text, lx + size * 0.75 * dpr, ly + ch * 0.68);
  ctx.strokeStyle = "rgba(217,160,102,0.7)";
  ctx.lineWidth = 1 * dpr;
  ctx.beginPath();
  ctx.moveTo(b.x + b.w / 2, ly + ch);
  ctx.lineTo(b.x + b.w / 2, b.y - 4 * dpr);
  ctx.stroke();
  ctx.restore();
}

function roundRect(ctx: CanvasRenderingContext2D, x: number, y: number, w: number, h: number, r: number) {
  ctx.beginPath();
  ctx.moveTo(x + r, y);
  ctx.arcTo(x + w, y, x + w, y + h, r);
  ctx.arcTo(x + w, y + h, x, y + h, r);
  ctx.arcTo(x, y + h, x, y, r);
  ctx.arcTo(x, y, x + w, y, r);
  ctx.closePath();
}

function drawDrone(
  ctx: CanvasRenderingContext2D,
  P: (v: [number, number, number]) => [number, number],
  x: number,
  y: number,
  ground: number,
  z: number,
  clock: number,
  dpr: number,
) {
  const body = P([x, y, z]);
  const shadow = P([x, y, ground]);
  // The camera's footprint on the ground.
  ctx.fillStyle = "rgba(217,160,102,0.10)";
  ctx.beginPath();
  ctx.moveTo(body[0], body[1]);
  ctx.lineTo(shadow[0] - 34 * dpr, shadow[1] + 6 * dpr);
  ctx.lineTo(shadow[0] + 34 * dpr, shadow[1] + 6 * dpr);
  ctx.closePath();
  ctx.fill();
  ctx.fillStyle = "rgba(26,19,16,0.25)";
  ctx.beginPath();
  ctx.ellipse(shadow[0], shadow[1], 9 * dpr, 3.5 * dpr, 0, 0, Math.PI * 2);
  ctx.fill();
  // Arms and rotors.
  ctx.strokeStyle = "#1A1310";
  ctx.lineWidth = 1.6 * dpr;
  const arm = 0.55;
  const tips = [
    P([x + arm, y, z]),
    P([x - arm, y, z]),
    P([x, y + arm, z]),
    P([x, y - arm, z]),
  ];
  ctx.beginPath();
  ctx.moveTo(tips[0][0], tips[0][1]);
  ctx.lineTo(tips[1][0], tips[1][1]);
  ctx.moveTo(tips[2][0], tips[2][1]);
  ctx.lineTo(tips[3][0], tips[3][1]);
  ctx.stroke();
  for (const [k, t] of tips.entries()) {
    ctx.fillStyle = "rgba(26,19,16,0.18)";
    ctx.beginPath();
    ctx.ellipse(t[0], t[1], 6 * dpr, 2.4 * dpr, 0, 0, Math.PI * 2);
    ctx.fill();
    const a = clock * 40 + k;
    ctx.strokeStyle = "rgba(26,19,16,0.7)";
    ctx.lineWidth = 0.8 * dpr;
    ctx.beginPath();
    ctx.moveTo(t[0] - Math.cos(a) * 6 * dpr, t[1] - Math.sin(a) * 2.4 * dpr);
    ctx.lineTo(t[0] + Math.cos(a) * 6 * dpr, t[1] + Math.sin(a) * 2.4 * dpr);
    ctx.stroke();
  }
  ctx.fillStyle = "#1A1310";
  ctx.beginPath();
  ctx.ellipse(body[0], body[1], 4 * dpr, 2.6 * dpr, 0, 0, Math.PI * 2);
  ctx.fill();
  ctx.fillStyle = "#D9A066";
  ctx.beginPath();
  ctx.arc(body[0], body[1] + 2 * dpr, 1.3 * dpr, 0, Math.PI * 2);
  ctx.fill();
}

/** A small world for the "where we fly" strip: the land as it looks, the drone's reading on hover. */
export function WorldTile({ preset, seed, className = "" }: { preset: Preset; seed: number; className?: string }) {
  const solidRef = useRef<HTMLCanvasElement>(null);
  const wireRef = useRef<HTMLCanvasElement>(null);

  useEffect(() => {
    const a = solidRef.current;
    const b = wireRef.current;
    if (!a || !b) return;
    const scene = buildScene(preset, seed);
    const dpr = Math.min(2, window.devicePixelRatio || 1);
    const paint = () => {
      const r = a.getBoundingClientRect();
      if (r.width < 10) return;
      const w = renderWorld(scene, r.width, r.height, dpr, 0.05);
      for (const [c, src] of [
        [a, w.solid],
        [b, w.wire],
      ] as const) {
        c.width = src.width;
        c.height = src.height;
        c.getContext("2d")!.drawImage(src, 0, 0);
      }
    };
    const ro = new ResizeObserver(paint);
    ro.observe(a);
    return () => ro.disconnect();
  }, [preset, seed]);

  return (
    <div className={`relative ${className}`}>
      <canvas ref={solidRef} className="absolute inset-0 h-full w-full transition-opacity duration-700 group-hover:opacity-0" />
      <canvas ref={wireRef} className="absolute inset-0 h-full w-full opacity-0 transition-opacity duration-700 group-hover:opacity-100" />
    </div>
  );
}
