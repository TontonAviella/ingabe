"use client";

import { useEffect, useRef } from "react";
import type { Preset } from "@/lib/miniworld";
import { WorldTile } from "./MiniWorld";

const WORLDS: { preset: Preset; seed: number; label: string; note: string }[] = [
  { preset: "hills", seed: 3, label: "Hillside farms", note: "Terraces, mixed plots, bananas" },
  { preset: "marsh", seed: 5, label: "Marshland rice", note: "Paddies and channels" },
  { preset: "tea", seed: 9, label: "Tea estates", note: "Rows on steep slopes" },
  { preset: "scheme", seed: 4, label: "Irrigation schemes", note: "Blocks, canals, seasons" },
  { preset: "corridor", seed: 8, label: "Transmission lines", note: "Towers, spans, right of way" },
  { preset: "telecom", seed: 2, label: "Telecom & monitoring towers", note: "Masts, antennas, sites" },
  { preset: "distribution", seed: 6, label: "Village power lines", note: "Poles and wires along roads" },
];

/** Scrolling down moves the worlds sideways along a gentle arc; hovering one shows the drone's reading. */
export function WorldsStrip({ header }: { header: React.ReactNode }) {
  const outer = useRef<HTMLDivElement>(null);
  const track = useRef<HTMLDivElement>(null);
  const curve = useRef<SVGPathElement>(null);
  const tiles = useRef<(HTMLDivElement | null)[]>([]);

  useEffect(() => {
    const wide = window.matchMedia("(min-width: 768px)");
    let raf = 0;
    const update = () => {
      raf = 0;
      const o = outer.current;
      const t = track.current;
      if (!o || !t) return;
      const vw = window.innerWidth;
      if (!wide.matches) {
        t.style.transform = "";
        tiles.current.forEach((el) => el && (el.style.transform = ""));
        curve.current?.setAttribute("d", "");
        return;
      }
      const r = o.getBoundingClientRect();
      const travel = o.offsetHeight - window.innerHeight;
      const p = Math.min(1, Math.max(0, -r.top / Math.max(1, travel)));
      const shift = (t.scrollWidth - vw + 96) * p;
      t.style.transform = `translate3d(${-shift}px,0,0)`;
      const pts: [number, number][] = [];
      tiles.current.forEach((el) => {
        if (!el) return;
        const box = el.getBoundingClientRect();
        const c = (box.left + box.width / 2 - vw / 2) / (vw / 2);
        const dy = 90 * c * c;
        el.style.transform = `translate3d(0,${dy}px,0) scale(${1 - 0.08 * Math.min(1, Math.abs(c))})`;
        const parent = t.getBoundingClientRect();
        const dot = el.querySelector("[data-dot]")?.getBoundingClientRect();
        if (dot) pts.push([dot.left - parent.left + dot.width / 2, dot.top - parent.top + dot.height / 2]);
      });
      if (curve.current && pts.length > 1) {
        let d = `M${pts[0][0]},${pts[0][1]}`;
        for (let k = 1; k < pts.length; k++) {
          const [x0, y0] = pts[k - 1];
          const [x1, y1] = pts[k];
          d += ` C${(x0 + x1) / 2},${y0} ${(x0 + x1) / 2},${y1} ${x1},${y1}`;
        }
        curve.current.setAttribute("d", d);
      }
    };
    const on = () => {
      if (!raf) raf = requestAnimationFrame(update);
    };
    update();
    window.addEventListener("scroll", on, { passive: true });
    window.addEventListener("resize", on);
    wide.addEventListener("change", on);
    return () => {
      window.removeEventListener("scroll", on);
      window.removeEventListener("resize", on);
      wide.removeEventListener("change", on);
      cancelAnimationFrame(raf);
    };
  }, []);

  return (
    <div ref={outer} className="relative md:h-[280vh]">
      <div className="md:sticky md:top-0 md:flex md:h-screen md:flex-col md:overflow-hidden">
        <div className="mx-auto w-full max-w-page px-5 pt-24 sm:px-8 md:pt-28 lg:px-12">{header}</div>
        <div className="overflow-x-auto md:flex md:flex-1 md:items-center md:overflow-visible">
        <div ref={track} className="relative flex w-max snap-x snap-mandatory gap-6 px-5 pb-10 pt-6 will-change-transform sm:px-8 md:gap-10 md:px-12 md:pb-16 md:pt-0">
          <svg className="pointer-events-none absolute inset-0 hidden h-full w-full overflow-visible md:block" aria-hidden>
            <path ref={curve} fill="none" stroke="#D9A066" strokeOpacity="0.55" strokeWidth="1" strokeDasharray="2 5" />
          </svg>
          {WORLDS.map((w, k) => (
            <div
              key={w.preset}
              ref={(el) => {
                tiles.current[k] = el;
              }}
              className="group relative w-[78vw] shrink-0 snap-center sm:w-[46vw] md:w-[34vw] lg:w-[29vw]"
            >
              <WorldTile preset={w.preset} seed={w.seed} className="aspect-[4/3.2] w-full" />
              <div className="mt-2 flex flex-col items-center text-center">
                <span data-dot className="relative z-10 mb-3 h-2.5 w-2.5 rounded-full border border-caramel bg-cream transition-colors group-hover:bg-caramel" />
                <span className="font-mono text-[11px] uppercase tracking-label text-bitter">{w.label}</span>
                <span className="mt-1 text-[13px] text-mocha">{w.note}</span>
              </div>
            </div>
          ))}
        </div>
        </div>
      </div>
    </div>
  );
}
