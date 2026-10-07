"use client";

import { useEffect, useState } from "react";
import { INGABE_URL, SIGN_IN_URL } from "@/lib/site";

const LINKS = [
  { href: "#read", label: "What we read" },
  { href: "#where", label: "Where we fly" },
  { href: "#accuracy", label: "Accuracy" },
  { href: "#how", label: "How it works" },
];

/** The mark: the name, a small caramel point (the place we read), and "labs" in a lighter weight. */
export function Wordmark({ light = false }: { light?: boolean }) {
  return (
    <span className={`inline-flex items-baseline text-[19px] tracking-[-0.03em] ${light ? "text-sand" : "text-bitter"}`}>
      <span className="font-semibold">noza</span>
      <span aria-hidden className="mx-[3px] inline-block h-[5px] w-[5px] translate-y-[-1px] rounded-full bg-caramel" />
      <span className={`font-normal ${light ? "text-latte" : "text-mocha"}`}>labs</span>
    </span>
  );
}

export function Nav() {
  const [scrolled, setScrolled] = useState(false);
  const [open, setOpen] = useState(false);
  useEffect(() => {
    const on = () => setScrolled(window.scrollY > 12);
    on();
    window.addEventListener("scroll", on, { passive: true });
    return () => window.removeEventListener("scroll", on);
  }, []);

  return (
    <header
      className={`arrive fixed inset-x-0 top-0 z-50 transition-colors duration-500 ${
        scrolled || open ? "border-b border-bitter/[0.08] bg-cream/75 backdrop-blur-xl backdrop-saturate-150" : "border-b border-transparent"
      }`}
    >
      <div className="mx-auto flex h-16 max-w-page items-center justify-between px-5 sm:px-8 lg:px-12">
        <a href="#top" aria-label="Noza Labs, back to top">
          <Wordmark />
        </a>
        <nav className="hidden items-center gap-8 text-[13.5px] text-bitter/70 md:flex">
          {LINKS.map((l) => (
            <a key={l.href} href={l.href} className="transition-colors hover:text-bitter">
              {l.label}
            </a>
          ))}
        </nav>
        <div className="flex items-center gap-5">
          <a href={SIGN_IN_URL} className="hidden text-[13.5px] text-bitter/70 transition-colors hover:text-bitter sm:inline">
            Sign in
          </a>
          <a
            href={INGABE_URL}
            className="group hidden items-center gap-2.5 rounded-full bg-bitter py-2 pl-4 pr-3.5 text-[13.5px] font-medium text-cream transition-colors hover:bg-bark sm:inline-flex"
          >
            <IngabeIcon className="-ml-1.5 h-[22px] w-[22px]" />
            Open Ingabe
            <Arrow className="transition-transform duration-300 group-hover:-translate-y-0.5 group-hover:translate-x-0.5" diagonal />
          </a>
          <button
            type="button"
            className="inline-flex h-10 w-10 items-center justify-center rounded-full border border-bitter/15 md:hidden"
            aria-label={open ? "Close menu" : "Open menu"}
            aria-expanded={open}
            onClick={() => setOpen((o) => !o)}
          >
            <svg viewBox="0 0 20 20" className="h-4 w-4" aria-hidden>
              <path d={open ? "M5 5l10 10M15 5L5 15" : "M3 7h14M3 13h14"} stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
            </svg>
          </button>
        </div>
      </div>
      {open && (
        <nav className="border-t border-bitter/10 px-5 pb-6 pt-2 md:hidden">
          {LINKS.map((l) => (
            <a key={l.href} href={l.href} onClick={() => setOpen(false)} className="block border-b border-bitter/10 py-4 text-[17px]">
              {l.label}
            </a>
          ))}
          <a href={SIGN_IN_URL} className="block border-b border-bitter/10 py-4 text-[17px]">
            Sign in
          </a>
          <a href={INGABE_URL} className="mt-5 inline-flex items-center gap-3 rounded-full bg-bitter px-6 py-3 text-[15px] text-cream">
            Open Ingabe <Arrow diagonal />
          </a>
        </nav>
      )}
    </header>
  );
}

export function Arrow({ className = "", diagonal = false }: { className?: string; diagonal?: boolean }) {
  return (
    <svg viewBox="0 0 16 16" className={`h-3.5 w-3.5 ${className}`} aria-hidden>
      <path
        d={diagonal ? "M4.5 11.5l7-7M5.5 4.5h6v6" : "M2.5 8h11M9 3.5L13.5 8 9 12.5"}
        fill="none"
        stroke="currentColor"
        strokeWidth="1.5"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );
}

/** Ingabe's app icon, the same mark the app shows: a lowercase i with a caramel dot. */
export function IngabeIcon({ className = "h-6 w-6" }: { className?: string }) {
  return (
    <svg viewBox="0 0 64 64" className={className} aria-hidden>
      <rect width="64" height="64" rx="15" fill="#0B0908" stroke="#F3EDE6" strokeOpacity="0.18" strokeWidth="2" />
      <rect x="27.5" y="27" width="9" height="25" rx="4.5" fill="#F3EDE6" />
      <circle cx="32" cy="16.5" r="5.5" fill="#D9A066" />
    </svg>
  );
}
