// Ingabe's mark: the horns of an Inyambo, Rwanda's long-horned cattle (a sign of richness and calm), with
// the caramel sun between them. Read the other way it is a sensor's focus, "( • )": the place we read.
// Two curved strokes and a dot, so a child can draw it. The same paths are in noza-web (components/Nav.tsx)
// and the favicons.

const CARAMEL = '#D9A066';
const NIGHT = '#0B0908';
const SAND = '#F3EDE6';

const HORNS = [
  'M26 51 C14 51.4 6 43.6 5.2 31 C4.5 19.6 8.2 9.6 16.6 2.6 C12 9.8 10.4 18.8 11 28.6 C11.7 38.6 17.4 46 26.8 46.6 Z',
  'M38 51 C50 51.4 58 43.6 58.8 31 C59.5 19.6 55.8 9.6 47.4 2.6 C52 9.8 53.6 18.8 53 28.6 C52.3 38.6 46.6 46 37.2 46.6 Z',
];

/** The horns and the sun on a 64 grid, in `color`. */
function Horns({ color }: { color: string }) {
  return (
    <>
      <circle cx="32" cy="27" r="5.6" fill={CARAMEL} />
      {HORNS.map((d) => (
        <path key={d} d={d} fill={color} />
      ))}
    </>
  );
}

/** The app icon: the horns in sand on a dark rounded square. */
export function IngabeIcon({ className = 'h-8 w-8' }: { className?: string }) {
  return (
    <svg viewBox="0 0 64 64" className={className} role="img" aria-label="Ingabe">
      <rect width="64" height="64" rx="15" fill={NIGHT} />
      <g transform="translate(8.3 12) scale(0.74)">
        <Horns color={SAND} />
      </g>
    </svg>
  );
}

/** The wordmark: "ingabe", set plainly; the horns beside it carry the colour. */
export function IngabeWordmark({ className = '' }: { className?: string }) {
  return <span className={`font-semibold tracking-[-0.03em] ${className}`}>ingabe</span>;
}
