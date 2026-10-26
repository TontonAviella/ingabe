// Our one mark, on everything we make (Roger, 2026-10-26): "n." — the n of noza, drawn as one arch, and a caramel
// full stop. The same paths are in noza-web (components/Nav.tsx), the favicons and the sign-in page's logos
// (noza-web/public/brand/workos). It replaced Ingabe's earlier Inyambo-horns mark.

const CARAMEL = '#D9A066';
const NIGHT = '#0B0908';
const SAND = '#F3EDE6';

const ARCH = 'M19 44V27.5c0-3 2.4-5.5 5.5-5.5h0c3 0 5.5 2.5 5.5 5.5V44';

/** The n. on a 64 grid: the arch in `color`, the full stop in caramel. */
function NozaN({ color }: { color: string }) {
  return (
    <>
      <path d={ARCH} fill="none" stroke={color} strokeWidth="5" strokeLinecap="round" />
      <circle cx="43" cy="41" r="4.5" fill={CARAMEL} />
    </>
  );
}

/** The app icon: the n. in sand on a dark rounded square. */
export function IngabeIcon({ className = 'h-8 w-8' }: { className?: string }) {
  return (
    <svg viewBox="0 0 64 64" className={className} role="img" aria-label="Ingabe">
      <rect width="64" height="64" rx="14" fill={NIGHT} />
      <NozaN color={SAND} />
    </svg>
  );
}

/** The wordmark: "ingabe", set plainly; the mark beside it carries the colour. */
export function IngabeWordmark({ className = '' }: { className?: string }) {
  return <span className={`font-semibold tracking-[-0.03em] ${className}`}>ingabe</span>;
}
