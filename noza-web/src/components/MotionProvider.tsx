"use client";

import { createContext, useContext, useEffect, useState } from "react";

interface Motion {
  paused: boolean;
  reduced: boolean;
  toggle: () => void;
}

const MotionContext = createContext<Motion>({ paused: false, reduced: false, toggle: () => {} });

/** One switch for every moving drawing on the page; it also respects the reader's reduced-motion setting. */
export function MotionProvider({ children }: { children: React.ReactNode }) {
  const [paused, setPaused] = useState(false);
  const [reduced, setReduced] = useState(false);

  useEffect(() => {
    const q = window.matchMedia("(prefers-reduced-motion: reduce)");
    const on = () => setReduced(q.matches);
    on();
    q.addEventListener("change", on);
    return () => q.removeEventListener("change", on);
  }, []);

  return <MotionContext.Provider value={{ paused, reduced, toggle: () => setPaused((p) => !p) }}>{children}</MotionContext.Provider>;
}

export const useMotion = () => useContext(MotionContext);

export function PauseButton({ className = "" }: { className?: string }) {
  const { paused, reduced, toggle } = useMotion();
  if (reduced) return null;
  return (
    <button
      type="button"
      onClick={toggle}
      aria-pressed={paused}
      className={`inline-flex items-center gap-2 font-mono text-[11px] uppercase tracking-[0.14em] transition-opacity hover:opacity-70 ${className}`}
    >
      <span aria-hidden className="inline-flex h-3 w-3 items-center justify-center">
        {paused ? (
          <svg viewBox="0 0 10 10" className="h-2.5 w-2.5 fill-current">
            <path d="M2 1l7 4-7 4z" />
          </svg>
        ) : (
          <svg viewBox="0 0 10 10" className="h-2.5 w-2.5 fill-current">
            <path d="M2 1h2v8H2zM6 1h2v8H6z" />
          </svg>
        )}
      </span>
      {paused ? "Play drawings" : "Pause drawings"}
    </button>
  );
}
