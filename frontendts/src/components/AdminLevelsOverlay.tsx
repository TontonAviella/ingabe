import type { GeoJSONSource, IControl, MapGeoJSONFeature, MapMouseEvent, Map as MLMap } from 'maplibre-gl';
import { useEffect, useState } from 'react';
import { AdminLevelLadder } from '@/components/AdminLevelLadder';
import { AdminUnitTooltip } from '@/components/AdminUnitTooltip';
import { type AdminLevel, useAdminOutlines, useDistrictNdviMap } from '@/hooks/useRwandaApi';
import { levelForZoom, viewBbox } from '@/lib/adminLevels';

// Rwanda's admin outlines on the project map: districts, then sectors, cells
// and villages as you zoom in. Outlines only (no fill), so the user's own
// layers stay visible; hovering names the unit and gives its district's
// latest NDVI, saying when that value is shared by every finer unit.

const DISTRICT_SRC = 'rw-admin-districts';
const UNIT_SRC = 'rw-admin-units';
const LAYERS = {
  districtHit: 'rw-admin-district-hit',
  districtLine: 'rw-admin-district-line',
  unitHit: 'rw-admin-unit-hit',
  unitCasing: 'rw-admin-unit-casing',
  unitLine: 'rw-admin-unit-line',
  districtCasing: 'rw-admin-district-casing',
  unitHover: 'rw-admin-unit-hover',
};
const EMPTY: GeoJSON.FeatureCollection = { type: 'FeatureCollection', features: [] };
const STORAGE_KEY = 'mundi.adminOutlines';

function readEnabled(): boolean {
  try {
    return localStorage.getItem(STORAGE_KEY) !== 'off';
  } catch {
    return true;
  }
}

function writeEnabled(on: boolean) {
  try {
    localStorage.setItem(STORAGE_KEY, on ? 'on' : 'off');
  } catch {
    /* private window: the toggle still works for this session */
  }
}

/** Adds the sources and layers if a style reload removed them. */
function ensureLayers(map: MLMap) {
  if (!map.getSource(DISTRICT_SRC)) map.addSource(DISTRICT_SRC, { type: 'geojson', data: EMPTY });
  if (!map.getSource(UNIT_SRC)) map.addSource(UNIT_SRC, { type: 'geojson', data: EMPTY });
  const add = (spec: Parameters<MLMap['addLayer']>[0]) => {
    if (!map.getLayer(spec.id)) map.addLayer(spec);
  };
  // Transparent fills only exist so hover can find the unit under the cursor.
  add({ id: LAYERS.districtHit, type: 'fill', source: DISTRICT_SRC, paint: { 'fill-color': '#000', 'fill-opacity': 0 } });
  add({ id: LAYERS.unitHit, type: 'fill', source: UNIT_SRC, paint: { 'fill-color': '#000', 'fill-opacity': 0 } });
  // Dark casings keep the outlines readable on light basemaps too.
  add({
    id: LAYERS.unitCasing,
    type: 'line',
    source: UNIT_SRC,
    paint: { 'line-color': '#111827', 'line-width': 2.2, 'line-opacity': 0.45 },
  });
  add({ id: LAYERS.unitLine, type: 'line', source: UNIT_SRC, paint: { 'line-color': '#ffffff', 'line-width': 0.8, 'line-opacity': 0.8 } });
  add({
    id: LAYERS.districtCasing,
    type: 'line',
    source: DISTRICT_SRC,
    paint: { 'line-color': '#111827', 'line-width': 3.4, 'line-opacity': 0.55 },
  });
  add({
    id: LAYERS.districtLine,
    type: 'line',
    source: DISTRICT_SRC,
    paint: { 'line-color': '#facc15', 'line-width': 1.6, 'line-opacity': 0.9 },
  });
  add({
    id: LAYERS.unitHover,
    type: 'line',
    source: UNIT_SRC,
    filter: ['==', ['get', 'id'], ''],
    paint: { 'line-color': '#facc15', 'line-width': 2.5 },
  });
}

function removeLayers(map: MLMap) {
  for (const id of Object.values(LAYERS)) if (map.getLayer(id)) map.removeLayer(id);
  for (const id of [DISTRICT_SRC, UNIT_SRC]) if (map.getSource(id)) map.removeSource(id);
}

class AdminOutlinesControl implements IControl {
  private _button: HTMLButtonElement | undefined;
  constructor(
    private _on: boolean,
    private _onToggle: () => void,
  ) {}
  onAdd(): HTMLElement {
    const container = document.createElement('div');
    container.className = 'maplibregl-ctrl maplibregl-ctrl-group';
    const button = document.createElement('button');
    button.type = 'button';
    button.style.display = 'flex';
    button.style.alignItems = 'center';
    button.style.justifyContent = 'center';
    button.innerHTML = `<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="#333" stroke-width="2" stroke-linejoin="round" aria-hidden="true">
      <path d="M3 6l6-3 6 3 6-3v15l-6 3-6-3-6 3z"/><path d="M9 3v15M15 6v15"/></svg>`;
    button.addEventListener('click', () => this._onToggle());
    this._button = button;
    this.setOn(this._on);
    container.appendChild(button);
    return container;
  }
  onRemove(): void {
    this._button?.parentElement?.remove();
  }
  setOn(on: boolean) {
    this._on = on;
    if (!this._button) return;
    const label = on ? 'Hide district, sector, cell and village outlines' : 'Show district, sector, cell and village outlines';
    this._button.title = label;
    this._button.setAttribute('aria-label', label);
    this._button.setAttribute('aria-pressed', String(on));
    this._button.style.background = on ? '#fde68a' : 'transparent';
  }
}

export function AdminLevelsOverlay({ map, projectId }: { map: MLMap | null; projectId?: string }) {
  const [enabled, setEnabled] = useState(readEnabled);
  const [view, setView] = useState<{ level: AdminLevel; bbox: string | null }>({ level: 'district', bbox: null });
  const [hover, setHover] = useState<{ district: MapGeoJSONFeature; unit?: MapGeoJSONFeature; x: number; y: number } | null>(null);
  const { data: districts } = useDistrictNdviMap(projectId);
  const outlines = useAdminOutlines(view.level, enabled ? view.bbox : null);
  const units = enabled && view.level !== 'district' && outlines.data?.level === view.level ? outlines.data : null;

  // Toggle button beside the zoom and basemap controls.
  // biome-ignore lint/correctness/useExhaustiveDependencies: the control keeps its own on/off state after mount
  useEffect(() => {
    if (!map) return;
    const control = new AdminOutlinesControl(enabled, () =>
      setEnabled((on) => {
        writeEnabled(!on);
        control.setOn(!on);
        return !on;
      }),
    );
    map.addControl(control, 'top-right');
    return () => {
      try {
        map.removeControl(control);
      } catch {
        /* map already destroyed */
      }
    };
  }, [map]);

  // Level and view follow the camera.
  useEffect(() => {
    if (!map || !enabled) return;
    const onMove = () => {
      const level = levelForZoom(map.getZoom());
      setView({ level, bbox: level === 'district' ? null : viewBbox(map) });
    };
    onMove();
    map.on('moveend', onMove);
    return () => {
      map.off('moveend', onMove);
    };
  }, [map, enabled]);

  // Layers: added now and again after every style reload (Sage and the
  // basemap switcher call setStyle, which drops layers added directly).
  useEffect(() => {
    if (!map) return;
    if (!enabled) {
      try {
        removeLayers(map);
      } catch {
        /* style mid-reload: nothing of ours is on it */
      }
      setHover(null);
      return;
    }
    // isStyleLoaded() is false while tiles load, so try now and retry once the
    // map is idle if the style itself is not ready yet.
    const apply = () => {
      try {
        ensureLayers(map);
      } catch {
        map.once('idle', apply);
        return;
      }
      (map.getSource(DISTRICT_SRC) as GeoJSONSource).setData((districts as unknown as GeoJSON.FeatureCollection) ?? EMPTY);
      (map.getSource(UNIT_SRC) as GeoJSONSource).setData((units as unknown as GeoJSON.FeatureCollection) ?? EMPTY);
    };
    apply();
    map.on('style.load', apply);
    return () => {
      map.off('style.load', apply);
      map.off('idle', apply);
    };
  }, [map, enabled, districts, units]);

  // Hover card.
  useEffect(() => {
    if (!map || !enabled) return;
    const onMouseMove = (e: MapMouseEvent) => {
      if (!map.getLayer(LAYERS.districtHit)) return;
      const district = map.queryRenderedFeatures(e.point, { layers: [LAYERS.districtHit] })[0];
      const unit = map.getLayer(LAYERS.unitHit) ? map.queryRenderedFeatures(e.point, { layers: [LAYERS.unitHit] })[0] : undefined;
      if (map.getLayer(LAYERS.unitHover)) map.setFilter(LAYERS.unitHover, ['==', ['get', 'id'], unit?.properties?.id ?? '']);
      setHover(district ? { district, unit, x: e.point.x, y: e.point.y } : null);
    };
    const onLeave = () => setHover(null);
    map.on('mousemove', onMouseMove);
    map.getCanvas().addEventListener('mouseleave', onLeave);
    return () => {
      map.off('mousemove', onMouseMove);
      map.getCanvas().removeEventListener('mouseleave', onLeave);
    };
  }, [map, enabled]);

  if (!enabled || !districts) return null;
  return (
    <>
      <div className="absolute top-3 left-1/2 -translate-x-1/2 z-10 flex flex-col items-center gap-1 pointer-events-none">
        <AdminLevelLadder levels={districts.levels} current={view.level} />
        {view.level !== 'district' && !units && outlines.isFetching && (
          <div className="bg-white/95 dark:bg-gray-800/95 text-gray-900 dark:text-gray-100 px-2 py-1 rounded-md shadow text-[11px]">
            Loading {view.level} outlines…
          </div>
        )}
        {units?.truncated && (
          <div className="bg-white/95 dark:bg-gray-800/95 text-gray-900 dark:text-gray-100 px-2 py-1 rounded-md shadow text-[11px]">
            Zoom in to see every {view.level}.
          </div>
        )}
      </div>
      {hover && (
        <AdminUnitTooltip
          district={hover.district.properties}
          unit={hover.unit?.properties}
          x={hover.x}
          y={hover.y}
          vegetation={districts?.vegetation !== false}
        />
      )}
    </>
  );
}
