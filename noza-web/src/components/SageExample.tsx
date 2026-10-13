"use client";

import { useEffect, useRef, useState } from "react";
import { useMotion } from "./MotionProvider";

const QUESTION = "Which plots should I visit first this week?";
const LEAD = "Start with the two plots where the crop is thin.";
const POINTS = [
  { k: "Drone photo", v: "open soil covers a real share of both plots, mostly along the lower edge." },
  { k: "Satellite rain", v: "the last ten days were drier than usual for this point in the season." },
  { k: "Not sure yet", v: "one plot could be maize or beans. A quick look on the ground settles it." },
];

/** An example of how Sage answers: the answer first, each point with where it came from. */
export function SageExample() {
  const ref = useRef<HTMLDivElement>(null);
  const { reduced } = useMotion();
  const [typed, setTyped] = useState(0);
  const [stage, setStage] = useState(0);

  useEffect(() => {
    if (reduced) {
      setTyped(QUESTION.length);
      setStage(5);
      return;
    }
    const el = ref.current;
    if (!el) return;
    let timers: ReturnType<typeof setTimeout>[] = [];
    const io = new IntersectionObserver(
      ([e]) => {
        if (!e.isIntersecting) return;
        io.disconnect();
        for (let i = 1; i <= QUESTION.length; i++) timers.push(setTimeout(() => setTyped(i), 300 + i * 32));
        const after = 300 + QUESTION.length * 32;
        [700, 1500, 2300, 3100, 3900].forEach((d, s) => timers.push(setTimeout(() => setStage(s + 1), after + d)));
      },
      { threshold: 0.4 },
    );
    io.observe(el);
    return () => {
      io.disconnect();
      timers.forEach(clearTimeout);
      timers = [];
    };
  }, [reduced]);

  return (
    <div ref={ref} className="relative">
      <div className="mb-3 font-mono text-[11px] uppercase tracking-label text-latte/70">Example answer</div>
      <div className="overflow-hidden rounded-[22px] bg-chocolate/80 ring-1 ring-white/10 backdrop-blur">
        <div className="flex justify-end p-5 pb-0">
          <div className="max-w-[85%] rounded-2xl rounded-br-md bg-sand px-4 py-2.5 text-[15px] text-bitter">
            {QUESTION.slice(0, typed)}
            {typed < QUESTION.length && <span className="cursor" />}
          </div>
        </div>
        <div className="p-5 sm:p-6">
          <div className="flex items-center gap-2.5">
            <span className="flex h-7 w-7 items-center justify-center rounded-full bg-caramel font-display text-[15px] text-night">S</span>
            <span className="text-[13px] font-medium text-sand">Sage</span>
            {stage > 0 && stage < 2 && <span className="font-mono text-[11px] text-latte">reading the photo and the rain…</span>}
          </div>
          <p className={`mt-4 font-display text-[26px] leading-tight text-sand transition-all duration-700 ${stage >= 2 ? "opacity-100" : "translate-y-1 opacity-0"}`}>
            {LEAD}
          </p>
          <ul className="mt-4 space-y-3">
            {POINTS.map((p, i) => (
              <li
                key={p.k}
                className={`flex gap-3 border-t border-white/10 pt-3 text-[14.5px] leading-relaxed text-sand/90 transition-all duration-700 ${
                  stage >= 3 + Math.min(i, 1) + (i === 2 ? 1 : 0) ? "opacity-100" : "translate-y-1 opacity-0"
                }`}
              >
                <span className="mt-1.5 h-1.5 w-1.5 shrink-0 rounded-full bg-caramel" />
                <span>
                  <span className="text-caramel">{p.k}:</span> {p.v}
                </span>
              </li>
            ))}
          </ul>
          <div className={`mt-5 flex flex-wrap gap-2 transition-opacity duration-700 ${stage >= 5 ? "opacity-100" : "opacity-0"}`}>
            {["Drone photo", "CHIRPS rainfall", "Field checks"].map((s) => (
              <span key={s} className="rounded-full border border-white/10 px-3 py-1 font-mono text-[10.5px] uppercase tracking-[0.12em] text-latte">
                {s}
              </span>
            ))}
          </div>
        </div>
      </div>
    </div>
  );
}
