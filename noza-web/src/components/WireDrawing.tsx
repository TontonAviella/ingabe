"use client";

import { useEffect, useRef } from "react";
import { model, toPath, type ModelName } from "@/lib/wire3d";
import { useMotion } from "./MotionProvider";

const FRAME: Record<ModelName, { scale: number; cy: number; start: number }> = {
  crops: { scale: 2.45, cy: 175, start: 0.6 },
  count: { scale: 2.65, cy: 168, start: 0.5 },
  gaps: { scale: 2.45, cy: 168, start: 0.4 },
  tower: { scale: 3.0, cy: 290, start: 0.3 },
  vegetation: { scale: 2.55, cy: 240, start: 0.25 },
  corridor: { scale: 2.45, cy: 225, start: -0.35 },
};

/** A line drawing that turns slowly; caramel marks what the drone found, dashes what it is not sure about. */
export function WireDrawing({ name, className = "" }: { name: ModelName; className?: string }) {
  const base = useRef<SVGPathElement>(null);
  const accent = useRef<SVGPathElement>(null);
  const dashed = useRef<SVGPathElement>(null);
  const caption = useRef<SVGTextElement>(null);
  const svg = useRef<SVGSVGElement>(null);
  const { paused, reduced } = useMotion();
  const state = useRef({ paused, reduced });
  state.current = { paused, reduced };

  useEffect(() => {
    const f = FRAME[name];
    let t = 4;
    let raf = 0;
    let last = performance.now();
    let visible = false;
    let acc = 0;
    const paint = () => {
      const m = model(name, state.current.reduced ? 99 : t);
      const angle = f.start + Math.sin(t * 0.16) * 0.55;
      base.current?.setAttribute("d", toPath(m.base, angle, f.scale, 200, f.cy));
      accent.current?.setAttribute("d", toPath(m.accent, angle, f.scale, 200, f.cy));
      dashed.current?.setAttribute("d", toPath(m.dashed, angle, f.scale, 200, f.cy));
      if (caption.current) caption.current.textContent = m.caption ?? "";
    };
    const tick = (now: number) => {
      const dt = Math.min(0.05, (now - last) / 1000);
      last = now;
      acc += dt;
      if (visible && !state.current.paused && !state.current.reduced && acc > 1 / 40) {
        t += acc;
        acc = 0;
        paint();
      }
      raf = requestAnimationFrame(tick);
    };
    const io = new IntersectionObserver(([e]) => (visible = e.isIntersecting), { threshold: 0.05 });
    if (svg.current) io.observe(svg.current);
    paint();
    raf = requestAnimationFrame(tick);
    return () => {
      cancelAnimationFrame(raf);
      io.disconnect();
    };
  }, [name]);

  return (
    <svg ref={svg} viewBox="0 0 400 320" className={className} aria-hidden>
      <path ref={base} fill="none" stroke="#B8A99B" strokeOpacity="0.55" strokeWidth="0.7" strokeLinecap="round" />
      <path ref={dashed} fill="none" stroke="#D9A066" strokeOpacity="0.85" strokeWidth="1" strokeDasharray="3 3" strokeLinecap="round" />
      <path ref={accent} fill="none" stroke="#D9A066" strokeWidth="1.1" strokeLinecap="round" />
      <text ref={caption} x="20" y="306" className="fill-caramel font-mono" fontSize="11" letterSpacing="1.5" />
    </svg>
  );
}
