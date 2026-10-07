"use client";

import { useEffect, useState } from "react";
import { useMotion } from "./MotionProvider";

const PLOTS = [
  { name: "Plot A", first: "Maize", second: "Maize" },
  { name: "Plot B", first: "Maize", second: "Beans" },
  { name: "Plot C", first: "Cassava", second: "Cassava" },
];

/** Two independent looks at a plot: the crop is named only when they agree. */
export function TwoLooks() {
  const { paused, reduced } = useMotion();
  const [k, setK] = useState(0);
  const [step, setStep] = useState(3);

  useEffect(() => {
    if (paused || reduced) return;
    const id = setInterval(() => {
      setStep((s) => {
        if (s >= 7) {
          setK((x) => (x + 1) % PLOTS.length);
          return 0;
        }
        return s + 1;
      });
    }, 900);
    return () => clearInterval(id);
  }, [paused, reduced]);

  const p = PLOTS[k];
  const agree = p.first === p.second;
  const show = reduced ? 7 : step;

  return (
    <div className="rounded-[22px] bg-espresso p-6 text-sand ring-1 ring-white/5 sm:p-8">
      <div className="flex items-center justify-between font-mono text-[11px] uppercase tracking-label text-latte">
        <span>{p.name}</span>
        <span className="flex gap-1.5">
          {PLOTS.map((_, i) => (
            <span key={i} className={`h-1 w-5 rounded-full transition-colors ${i === k ? "bg-caramel" : "bg-white/10"}`} />
          ))}
        </span>
      </div>

      <div className="mt-6 grid grid-cols-2 gap-3">
        {[p.first, p.second].map((guess, i) => (
          <div key={i} className="rounded-2xl bg-chocolate p-4 ring-1 ring-white/5">
            <div className="font-mono text-[10px] uppercase tracking-label text-latte/80">Look {i + 1}</div>
            <div
              className={`mt-3 font-display text-[28px] leading-none transition-all duration-500 ${
                show > i ? "translate-y-0 opacity-100" : "translate-y-2 opacity-0"
              }`}
            >
              {guess}
            </div>
            <PlotSketch seed={i + k * 2} />
          </div>
        ))}
      </div>

      <div
        className={`mt-4 flex items-center justify-between rounded-2xl border p-4 transition-all duration-500 ${
          show >= 3 ? "opacity-100" : "opacity-0"
        } ${agree ? "border-caramel/60 bg-caramel/10" : "border-dashed border-caramel/50"}`}
      >
        <div>
          <div className="font-mono text-[10px] uppercase tracking-label text-latte">What we show</div>
          <div className="mt-1.5 text-[17px]">
            {agree ? (
              <>
                <span className="text-caramel">{p.first}</span>
                <span className="text-latte"> · both looks agree</span>
              </>
            ) : (
              <>
                <span className="text-caramel">Not sure</span>
                <span className="text-latte">
                  {" "}
                  · {p.first.toLowerCase()} or {p.second.toLowerCase()}?
                </span>
              </>
            )}
          </div>
        </div>
        <span
          className={`flex h-9 w-9 items-center justify-center rounded-full ${agree ? "bg-caramel text-night" : "border border-dashed border-caramel text-caramel"}`}
          aria-hidden
        >
          {agree ? "✓" : "?"}
        </span>
      </div>
      <p className={`mt-4 min-h-[2.6em] text-[13px] leading-relaxed text-latte transition-opacity duration-500 ${show >= 3 ? "opacity-100" : "opacity-0"}`}>
        {agree ? "Named on the map, with how sure we are." : "Marked for a quick check in the field. Your answer teaches the next look."}
      </p>
    </div>
  );
}

function PlotSketch({ seed }: { seed: number }) {
  const rows = Array.from({ length: 5 }, (_, r) => r);
  return (
    <svg viewBox="0 0 120 60" className="mt-4 h-14 w-full" aria-hidden>
      <path d="M4 52 L30 8 L116 8 L90 52 Z" fill="none" stroke="#4A3326" />
      {rows.map((r) =>
        Array.from({ length: 7 }, (_, c) => {
          const x = 18 + c * 12 - r * 5 + ((seed * 3 + c * r) % 3);
          const y = 14 + r * 9;
          return <circle key={`${r}-${c}`} cx={x + 6} cy={y} r={1.6} fill="#B8A99B" opacity={0.65} />;
        }),
      )}
    </svg>
  );
}
