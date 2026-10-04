import type { Map as MLMap } from 'maplibre-gl';
import { useEffect, useState } from 'react';

export interface LayerLegend {
  title: string;
  items: Array<{ label: string; range?: string; color: string }>;
}

const LEGEND_KEY = 'mundi:legend';

/** Legends declared by visible map layers (layer metadata "mundi:legend"), without duplicates. */
function visibleLegends(map: MLMap): LayerLegend[] {
  const seen = new Set<string>();
  const legends: LayerLegend[] = [];
  for (const layer of map.getStyle()?.layers ?? []) {
    const legend = (layer.metadata as Record<string, unknown> | undefined)?.[LEGEND_KEY] as LayerLegend | undefined;
    if (!legend?.items?.length) continue;
    if (map.getLayoutProperty(layer.id, 'visibility') === 'none') continue;
    const key = JSON.stringify(legend);
    if (seen.has(key)) continue;
    seen.add(key);
    legends.push(legend);
  }
  return legends;
}

export function MapLegends({ map }: { map: MLMap | null }) {
  const [legends, setLegends] = useState<LayerLegend[]>([]);
  const [open, setOpen] = useState(true);

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

  return (
    <div className="absolute top-28 right-4 z-10 max-w-60 rounded-md bg-white/95 px-2 py-1.5 text-[11px] text-gray-900 shadow-lg dark:bg-gray-800/95 dark:text-gray-100">
      <button
        type="button"
        className="flex w-full items-center justify-between gap-2 font-semibold"
        aria-expanded={open}
        onClick={() => setOpen((v) => !v)}
      >
        <span>Legend</span>
        <span aria-hidden>{open ? '▾' : '▸'}</span>
      </button>
      {open &&
        legends.map((legend) => (
          <div key={legend.title} className="mt-1.5">
            <div className="font-medium">{legend.title}</div>
            <div className="mt-0.5 space-y-0.5">
              {legend.items.map((item) => (
                <div key={item.label} className="flex items-center gap-1.5">
                  <div className="h-2.5 w-2.5 shrink-0 rounded-sm" style={{ backgroundColor: item.color }} />
                  <span>
                    {item.label}
                    {item.range ? ` (${item.range})` : ''}
                  </span>
                </div>
              ))}
            </div>
          </div>
        ))}
    </div>
  );
}
