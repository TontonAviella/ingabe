import { X, ZoomIn } from 'lucide-react';
import { PANEL_FONT } from '@/components/CardAnswer';
import { featureFieldRows, featureReading } from '@/lib/featureFields';

// What was tapped on the map, in one quiet card. A hexagon of the satellite view leads with its reading (the
// figure, what it means, the day it is for) and keeps the rest small under it; anything else is a short list of
// what it holds.
//
// Roger, 2026-11-23: the panel (a white card with a table headed "Attribute | Value", the layer's type beside its
// name) looked immature; everything must be professional and minimal.

export function FeaturePanel({
  layerName,
  properties,
  photoUrl,
  onZoom,
  onClose,
}: {
  /** The layer the tapped thing belongs to; none when the layer is no longer on the map. */
  layerName?: string;
  properties: Record<string, unknown> | null | undefined;
  /** A camera point of a photo inspection shows its photo. */
  photoUrl?: string | null;
  onZoom: () => void;
  onClose: () => void;
}) {
  const reading = properties ? featureReading(properties) : null;
  const rows = properties ? featureFieldRows(properties).filter((row) => !reading?.said.includes(row.key)) : [];
  const button =
    'flex size-7 shrink-0 cursor-pointer items-center justify-center rounded-full text-fg-muted hover:bg-white/[0.08] hover:text-fg';
  return (
    <section
      aria-label="What you tapped"
      className="absolute bottom-10 left-4 z-[1] flex max-h-[60vh] w-64 max-w-[calc(100%-2rem)] flex-col gap-2 overflow-auto rounded-[14px] border border-white/10 bg-panel/90 p-3 text-[12px] text-fg-soft shadow-[0_8px_24px_rgba(0,0,0,0.35)] backdrop-blur-md"
      style={{ fontFamily: PANEL_FONT }}
    >
      <div className="flex items-center justify-between gap-2">
        <span className="min-w-0 truncate text-[12px] font-semibold text-fg-muted" title={layerName}>
          {layerName ?? 'On the map'}
        </span>
        <span className="-my-1 -mr-1 flex">
          <button type="button" className={button} aria-label="Zoom to it" title="Zoom to it" onClick={onZoom}>
            <ZoomIn className="size-3.5" aria-hidden="true" />
          </button>
          <button type="button" className={button} aria-label="Close" title="Close" onClick={onClose}>
            <X className="size-3.5" aria-hidden="true" />
          </button>
        </span>
      </div>
      {photoUrl && (
        <a href={photoUrl} target="_blank" rel="noreferrer" title="Open the full photo">
          <img src={photoUrl} alt="The photo taken here" loading="lazy" className="w-full rounded-[10px]" />
        </a>
      )}
      {reading && (
        <div>
          <div className="flex items-baseline gap-1.5">
            <span className="text-[24px] font-semibold leading-none text-fg tabular-nums">{reading.value}</span>
            <span className="text-fg-muted">{reading.label}</span>
          </div>
          {(reading.means || reading.when) && (
            <p className="m-0 mt-1.5 leading-snug">
              {reading.means && <span className="text-fg">{reading.means}</span>}
              {reading.means && reading.when ? ', ' : ''}
              {reading.when}
            </p>
          )}
        </div>
      )}
      {rows.length > 0 && (
        <dl className={`m-0 grid grid-cols-[auto_minmax(0,1fr)] gap-x-3 gap-y-1 ${reading ? 'border-t border-white/[0.07] pt-2' : ''}`}>
          {rows.map((row) => (
            <div key={row.key} className="contents">
              {/* the column's own name, for whoever needs it: on the label, since the row itself has no box */}
              <dt className="text-fg-muted" title={row.key}>
                {row.label}
              </dt>
              <dd className="m-0 break-words text-right text-fg">{row.value}</dd>
            </div>
          ))}
        </dl>
      )}
    </section>
  );
}
