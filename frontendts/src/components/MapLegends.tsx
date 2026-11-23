import { ChevronUp, Layers3 } from 'lucide-react';
import type { Map as MLMap } from 'maplibre-gl';
import { useEffect, useRef, useState } from 'react';
import { PANEL_FONT } from '@/components/CardAnswer';

// The map's key: one quiet card, a block for each layer on the map that declares one (layer metadata
// "mundi:legend"). A block says more than which colour is which. A layer on a scale shows the scale as one strip
// with this picture's own value marked on it; a layer that is one reading shows the figure and a gauge of where
// it sits in its range. A layer that only lists colours keeps a short list.
//
// Roger, 2026-11-23: the key (a white box headed "Legend" with two lists of thirteen swatches) looked immature;
// everything must be professional and minimal, with few things asking for the eye.

export interface LayerLegend {
  /** The whole key in words: what a screen reader reads, and the heading of a plain list. */
  title: string;
  items: Array<{ label: string; range?: string; color: string }>;
  /** A short name and the day or days the picture is for, when the layer gives them. */
  name?: string;
  when?: string;
  /** The items' colours as one strip from `low` to `high`, with words at its ends, numbers where its classes
   * change, and a mark where this picture's own value falls. */
  scale?: {
    colors: string[];
    low: number;
    high: number;
    low_word: string;
    high_word: string;
    ticks?: Array<{ at: number; label: string }>;
    mark?: { at: number; label: string; says: string };
  };
  /** One reading, with a few words beside it. */
  value?: string;
  value_note?: string;
  /** Where the readings (`from` to `to`) sit in a range (`low` to `high`). */
  gauge?: { low: number; high: number; from: number; to: number; low_label: string; high_label: string; caption: string; says: string };
  notes?: Array<{ label: string; color?: string }>;
}

const LEGEND_KEY = 'mundi:legend';

/** Legends declared by visible map layers (layer metadata "mundi:legend"), without duplicates. */
function visibleLegends(map: MLMap): LayerLegend[] {
  const seen = new Set<string>();
  const legends: LayerLegend[] = [];
  for (const layer of map.getStyle()?.layers ?? []) {
    const legend = (layer.metadata as Record<string, unknown> | undefined)?.[LEGEND_KEY] as LayerLegend | undefined;
    // a block needs something to show: colours to list, or a scale or a reading of its own
    if (!legend || !(legend.items?.length || legend.scale || legend.value)) continue;
    if (map.getLayoutProperty(layer.id, 'visibility') === 'none') continue;
    const key = JSON.stringify(legend);
    if (seen.has(key)) continue;
    seen.add(key);
    legends.push(legend);
  }
  return legends;
}

/** How far along a range a value lies, in percent, kept inside the range. */
const along = (value: number, low: number, high: number) => (high > low ? Math.min(1, Math.max(0, (value - low) / (high - low))) * 100 : 0);

function Scale({ legend }: { legend: LayerLegend & { scale: NonNullable<LayerLegend['scale']> } }) {
  const { colors, low, high, low_word, high_word, ticks = [], mark } = legend.scale;
  const at = mark ? along(mark.at, low, high) : 0;
  return (
    <>
      <div className={`relative ${mark ? 'mt-5' : 'mt-2'}`}>
        {mark && (
          <span
            className="absolute -top-4 -translate-x-1/2 text-[10.5px] font-semibold leading-none text-fg tabular-nums"
            // the figure stays inside the card when the mark is near an end
            style={{ left: `${Math.min(92, Math.max(8, at))}%` }}
            title={mark.says}
          >
            {mark.label}
          </span>
        )}
        <div className="flex h-2 overflow-hidden rounded-full">
          {colors.map((color, i) => (
            // keyed by place: a strip's places are its order, and two may share a colour
            <span key={i} className="flex-1" style={{ backgroundColor: color }} title={legend.items?.[i]?.label} />
          ))}
        </div>
        {mark && (
          <span
            aria-hidden="true"
            className="absolute -top-0.5 h-3 w-[2px] -translate-x-1/2 rounded-full bg-white shadow-[0_0_0_1.5px_var(--color-panel)]"
            style={{ left: `${at}%` }}
          />
        )}
      </div>
      <div className="relative mt-1 h-3.5 text-[10.5px] leading-none text-fg-muted" aria-hidden="true">
        <span className="absolute left-0">{low_word}</span>
        {ticks.map((tick) => (
          <span key={tick.at} className="absolute -translate-x-1/2 tabular-nums" style={{ left: `${along(tick.at, low, high)}%` }}>
            {tick.label}
          </span>
        ))}
        <span className="absolute right-0">{high_word}</span>
      </div>
      {mark && <span className="sr-only">{mark.says}</span>}
    </>
  );
}

function Gauge({ gauge, color }: { gauge: NonNullable<LayerLegend['gauge']>; color: string }) {
  const from = along(gauge.from, gauge.low, gauge.high);
  const to = along(gauge.to, gauge.low, gauge.high);
  // a reading with no spread is still a mark wide enough to see, and stays inside the track
  const width = Math.max(4, to - from);
  return (
    <div className="mt-2" title={gauge.says}>
      <div className="relative h-1.5 rounded-full bg-white/[0.12]">
        <span
          className="absolute inset-y-0 rounded-full"
          style={{ left: `${Math.min(from, 100 - width)}%`, width: `${width}%`, backgroundColor: color }}
        />
      </div>
      <div className="mt-1 flex justify-between gap-2 text-[10.5px] leading-none tabular-nums" aria-hidden="true">
        <span className="text-fg-muted">{gauge.low_label}</span>
        <span className="text-fg-subtle">{gauge.caption}</span>
        <span className="text-fg-muted">{gauge.high_label}</span>
      </div>
      <span className="sr-only">
        {gauge.says} It ran from {gauge.low_label} to {gauge.high_label} {gauge.caption}.
      </span>
    </div>
  );
}

/** One layer's block. Each leaves the same room at the end of its first line, where the card's hide button sits
 * beside the first block: the days then line up under one another. */
function Block({ legend }: { legend: LayerLegend }) {
  const rich = legend.scale || legend.value || legend.gauge;
  return (
    <section
      aria-label={legend.title}
      className="border-white/[0.07] not-first-of-type:mt-2.5 not-first-of-type:border-t not-first-of-type:pt-2.5"
    >
      <div className="flex items-baseline justify-between gap-2 pr-6">
        <span className="text-[12px] font-semibold text-fg">{legend.name ?? legend.title}</span>
        {legend.when && <span className="shrink-0 text-fg-muted tabular-nums">{legend.when}</span>}
      </div>
      {legend.scale && <Scale legend={{ ...legend, scale: legend.scale }} />}
      {legend.value && (
        <div className="mt-1.5 flex items-baseline gap-1.5">
          <span className="text-[18px] font-semibold leading-none text-fg tabular-nums">{legend.value}</span>
          {legend.value_note && <span className="text-fg-muted">{legend.value_note}</span>}
        </div>
      )}
      {legend.gauge && <Gauge gauge={legend.gauge} color={legend.items?.[0]?.color ?? 'var(--color-kivu)'} />}
      {!rich && (
        <ul className="m-0 mt-1.5 flex list-none flex-col gap-1 p-0">
          {(legend.items ?? []).map((item) => (
            <li key={`${item.label} ${item.color}`} className="flex items-center gap-2">
              <span className="size-2.5 shrink-0 rounded-[3px]" style={{ backgroundColor: item.color }} />
              <span>
                {item.label}
                {item.range ? ` (${item.range})` : ''}
              </span>
            </li>
          ))}
        </ul>
      )}
      {legend.notes?.map((note) => (
        <p
          key={`${note.label} ${note.color ?? ''}`}
          className="m-0 mt-1.5 flex items-center gap-1.5 text-[10.5px] leading-snug text-fg-subtle"
        >
          {note.color && <span className="size-2 shrink-0 rounded-[2px]" style={{ backgroundColor: note.color }} />}
          {note.label}
        </p>
      ))}
    </section>
  );
}

export function MapLegends({ map }: { map: MLMap | null }) {
  const [legends, setLegends] = useState<LayerLegend[]>([]);
  // on a phone the map is small: the key waits behind its button until it is asked for
  const [open, setOpen] = useState(() => typeof window === 'undefined' || window.innerWidth >= 1024);
  // Putting the key away or bringing it back swaps one button for another: the keyboard's place goes with it.
  const byReader = useRef(false);
  const toggle = (next: boolean) => {
    byReader.current = true;
    setOpen(next);
  };
  const takeFocus = (button: HTMLButtonElement | null) => {
    if (button && byReader.current) {
      byReader.current = false;
      button.focus();
    }
  };

  useEffect(() => {
    if (!map) return;
    const update = () => {
      if (!map.isStyleLoaded()) return;
      setLegends((prev) => {
        const next = visibleLegends(map);
        return JSON.stringify(prev) === JSON.stringify(next) ? prev : next;
      });
    };
    update();
    map.on('styledata', update);
    map.on('idle', update);
    return () => {
      map.off('styledata', update);
      map.off('idle', update);
    };
  }, [map]);

  if (legends.length === 0) return null;
  // Placed under the map's own column of buttons (zoom, north, outlines, hills, basemap: they end 214 px down) and in
  // line with it: 32 px higher, the last button lay over the card's corner and its hide button.

  if (!open) {
    return (
      <button
        type="button"
        aria-expanded={false}
        ref={takeFocus}
        onClick={() => toggle(true)}
        className="absolute top-56 right-2.5 z-[1] flex h-8 cursor-pointer items-center gap-1.5 rounded-full border border-white/10 bg-panel/90 px-3 text-[12px] font-semibold text-fg-soft shadow-[0_8px_24px_rgba(0,0,0,0.35)] backdrop-blur-md hover:text-fg"
        style={{ fontFamily: PANEL_FONT }}
      >
        <Layers3 className="size-3.5" aria-hidden="true" />
        Legend
      </button>
    );
  }

  return (
    <div
      role="group"
      aria-label="Legend"
      className="absolute top-56 right-2.5 z-[1] w-56 rounded-[14px] border border-white/10 bg-panel/90 p-3 text-[11.5px] text-fg-soft shadow-[0_8px_24px_rgba(0,0,0,0.35)] backdrop-blur-md"
      style={{ fontFamily: PANEL_FONT }}
    >
      <button
        type="button"
        aria-label="Hide the legend"
        title="Hide the legend"
        aria-expanded={true}
        ref={takeFocus}
        onClick={() => toggle(false)}
        className="absolute top-2 right-2 flex size-6 cursor-pointer items-center justify-center rounded-full text-fg-muted hover:bg-white/[0.08] hover:text-fg"
      >
        <ChevronUp className="size-3.5" aria-hidden="true" />
      </button>
      {legends.map((legend) => (
        // two layers may share a title (two photos seen the same day): what tells them apart is the whole key
        <Block key={JSON.stringify(legend)} legend={legend} />
      ))}
    </div>
  );
}
