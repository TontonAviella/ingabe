import type { Map as MLMap } from 'maplibre-gl';
import type { AdminLevel } from '@/hooks/useRwandaApi';

// Admin outlines follow the zoom: each level appears once its units are
// roughly 70-90 px across (sector ~58 km2, cell ~11 km2, village ~1.6 km2).
const LEVEL_MIN_ZOOM: Array<[AdminLevel, number]> = [
  ['village', 12.5],
  ['cell', 11],
  ['sector', 9.5],
  ['district', 0],
];

export const LEVEL_NAMES: Record<AdminLevel, string> = {
  district: 'District',
  sector: 'Sector',
  cell: 'Cell',
  village: 'Village',
};

export function levelForZoom(zoom: number): AdminLevel {
  return (LEVEL_MIN_ZOOM.find(([, min]) => zoom >= min) ?? ['district', 0])[0];
}

// Round the view outwards to a 0.05 degree grid so small pans reuse the same request.
export function viewBbox(map: MLMap): string {
  const b = map.getBounds();
  const out = (v: number, up: boolean) => ((up ? Math.ceil(v * 20) : Math.floor(v * 20)) / 20).toFixed(2);
  return [out(b.getWest(), false), out(b.getSouth(), false), out(b.getEast(), true), out(b.getNorth(), true)].join(',');
}

export function formatWeek(weekStart?: string | null): string | null {
  if (!weekStart) return null;
  const d = new Date(`${weekStart}T00:00:00Z`);
  return Number.isNaN(d.getTime())
    ? weekStart
    : d.toLocaleDateString('en-GB', { day: 'numeric', month: 'short', year: 'numeric', timeZone: 'UTC' });
}
