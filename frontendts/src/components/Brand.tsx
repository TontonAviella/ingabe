// The Ingabe mark, in the same family as the Noza Labs wordmark: the name in a plain weight, with one
// caramel point (here, the dot of the i) for the place we read.

const CARAMEL = '#D9A066';

/** The app icon: a lowercase i whose dot is caramel, on a dark rounded square. */
export function IngabeIcon({ className = 'h-8 w-8' }: { className?: string }) {
  return (
    <svg viewBox="0 0 64 64" className={className} role="img" aria-label="Ingabe">
      <rect width="64" height="64" rx="15" fill="#0B0908" />
      <rect x="27.5" y="27" width="9" height="25" rx="4.5" fill="#F3EDE6" />
      <circle cx="32" cy="16.5" r="5.5" fill={CARAMEL} />
    </svg>
  );
}

/** The wordmark: "ingabe" with the dot of the i in caramel. A caramel copy of the i, cut off just below its
 * dot, sits on the real one, so the dot is the font's own wherever the font puts it. */
export function IngabeWordmark({ className = '' }: { className?: string }) {
  return (
    <span className={`inline-flex items-baseline font-semibold tracking-[-0.03em] ${className}`} aria-label="Ingabe">
      <span aria-hidden className="relative inline-block leading-none">
        i
        <span className="absolute left-0 top-0 h-[0.3em] overflow-hidden" style={{ color: CARAMEL }}>
          i
        </span>
      </span>
      <span aria-hidden>ngabe</span>
    </span>
  );
}
