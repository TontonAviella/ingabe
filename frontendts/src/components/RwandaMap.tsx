import { Layer, Map as MapGL, type MapRef, NavigationControl, ScaleControl, Source } from '@vis.gl/react-maplibre';
import type { ExpressionSpecification, MapGeoJSONFeature } from 'maplibre-gl';
import { useRef, useState } from 'react';
import { AdminLevelLadder } from '@/components/AdminLevelLadder';
import { AdminUnitTooltip } from '@/components/AdminUnitTooltip';
import { type AdminLevel, useAdminOutlines, useDistrictNdviMap } from '@/hooks/useRwandaApi';
import { formatWeek, levelForZoom, viewBbox } from '@/lib/adminLevels';
import 'maplibre-gl/dist/maplibre-gl.css';

interface RwandaMapProps {
  selectedDistrict?: string;
}

const RWANDA_BOUNDS: [[number, number], [number, number]] = [
  [28.86, -2.84],
  [30.9, -1.05],
];
const BASEMAP_URL = 'https://basemaps.cartocdn.com/gl/positron-gl-style/style.json';
const NO_DATA_COLOR = '#cccccc';

// Colours, labels and legend come from the API (src/services/ndvi_classes.py),
// the same scale Sage uses when it describes NDVI.
const NDVI_FILL: ExpressionSpecification = ['coalesce', ['get', 'color'], NO_DATA_COLOR];

export function RwandaMap({ selectedDistrict }: RwandaMapProps) {
  const mapRef = useRef<MapRef>(null);
  const [hoveredDistrict, setHoveredDistrict] = useState<MapGeoJSONFeature | null>(null);
  const [hoveredUnit, setHoveredUnit] = useState<MapGeoJSONFeature | null>(null);
  const [cursor, setCursor] = useState<{ x: number; y: number } | null>(null);
  const [view, setView] = useState<{ level: AdminLevel; bbox: string | null }>({ level: 'district', bbox: null });
  const { data, isLoading, isError } = useDistrictNdviMap();
  const outlines = useAdminOutlines(view.level, view.bbox);
  // Collapsed by default on narrow screens, where the legend would cover the map.
  const [legendOpen, setLegendOpen] = useState(() => typeof window === 'undefined' || window.innerWidth >= 640);
  const [noteOpen, setNoteOpen] = useState(false);

  const latestWeek = formatWeek(data?.features.find((f) => f.properties.week_start)?.properties.week_start);
  const showOutlines = view.level !== 'district' && outlines.data?.level === view.level;

  const handleMoveEnd = () => {
    const map = mapRef.current?.getMap();
    if (!map) return;
    const level = levelForZoom(map.getZoom());
    setView({ level, bbox: level === 'district' ? null : viewBbox(map) });
  };

  const handleMouseMove = (event: maplibregl.MapMouseEvent) => {
    const map = mapRef.current?.getMap();
    if (!map) return;
    const district = map.queryRenderedFeatures(event.point, { layers: ['district-ndvi-fill'] })[0];
    const unit = showOutlines ? map.queryRenderedFeatures(event.point, { layers: ['admin-outline-fill'] })[0] : undefined;
    setHoveredDistrict((district as MapGeoJSONFeature) ?? null);
    setHoveredUnit((unit as MapGeoJSONFeature) ?? null);
    setCursor(district ? { x: event.point.x, y: event.point.y } : null);
  };

  return (
    <div className="relative w-full h-full">
      <MapGL
        ref={mapRef}
        initialViewState={{ bounds: RWANDA_BOUNDS, fitBoundsOptions: { padding: 16 } }}
        style={{ width: '100%', height: '100%' }}
        mapStyle={BASEMAP_URL}
        attributionControl={{ compact: true }}
        onLoad={handleMoveEnd}
        onMoveEnd={handleMoveEnd}
        onMouseMove={handleMouseMove}
        onMouseLeave={() => {
          setHoveredDistrict(null);
          setHoveredUnit(null);
          setCursor(null);
        }}
        interactiveLayerIds={['district-ndvi-fill']}
      >
        <NavigationControl position="top-right" />
        <ScaleControl position="bottom-left" />

        {data && (
          <Source id="district-ndvi" type="geojson" data={data}>
            <Layer id="district-ndvi-fill" type="fill" paint={{ 'fill-color': NDVI_FILL, 'fill-opacity': 0.72 }} />
            <Layer
              id="district-ndvi-line"
              type="line"
              paint={{ 'line-color': showOutlines ? '#374151' : '#ffffff', 'line-width': showOutlines ? 2 : 1 }}
            />
            <Layer
              id="district-ndvi-selected"
              type="line"
              filter={['==', ['downcase', ['get', 'district']], (selectedDistrict ?? '').toLowerCase()]}
              paint={{ 'line-color': '#111827', 'line-width': 3 }}
            />
          </Source>
        )}

        {showOutlines && outlines.data && (
          <Source id="admin-outlines" type="geojson" data={outlines.data}>
            <Layer id="admin-outline-fill" type="fill" paint={{ 'fill-color': '#000000', 'fill-opacity': 0 }} />
            <Layer id="admin-outline-line" type="line" paint={{ 'line-color': '#ffffff', 'line-width': 0.8 }} />
            <Layer
              id="admin-outline-hover"
              type="line"
              filter={['==', ['get', 'id'], hoveredUnit?.properties?.id ?? '']}
              paint={{ 'line-color': '#111827', 'line-width': 2 }}
            />
          </Source>
        )}
      </MapGL>

      {isLoading && (
        <div className="absolute top-4 left-4 bg-white dark:bg-gray-800 px-3 py-2 rounded-md shadow-md text-sm">
          Loading district vegetation…
        </div>
      )}
      {isError && (
        <div className="absolute top-4 left-4 bg-white dark:bg-gray-800 px-3 py-2 rounded-md shadow-md text-sm text-red-600">
          Could not load district vegetation data.
        </div>
      )}

      {hoveredDistrict && cursor && (
        <AdminUnitTooltip district={hoveredDistrict.properties} unit={hoveredUnit?.properties} x={cursor.x} y={cursor.y} />
      )}

      {/* Caption, level ladder and legend stack at the top left, clear of the attribution. */}
      <div className="absolute top-2 left-2 flex max-w-[calc(100%-4rem)] flex-col items-start gap-1">
        {data && !isLoading && (
          <button
            type="button"
            className="bg-white/95 dark:bg-gray-800/95 px-2 py-1 rounded-md shadow text-[11px] text-left"
            aria-expanded={noteOpen}
            onClick={() => setNoteOpen((open) => !open)}
            title={data.data_coverage.note}
          >
            <span className="font-semibold">One value per district</span>
            {' · Sentinel-2'}
            {latestWeek ? ` · week of ${latestWeek}` : ''} <span aria-hidden>ⓘ</span>
            {noteOpen && <span className="mt-1 block text-gray-600 dark:text-gray-400">{data.data_coverage.note}</span>}
          </button>
        )}
        {data && !isLoading && <AdminLevelLadder levels={data.levels} current={view.level} />}
        {showOutlines && outlines.data?.truncated && (
          <div className="bg-white/95 dark:bg-gray-800/95 px-2 py-1 rounded-md shadow text-[11px]">Zoom in to see every {view.level}.</div>
        )}
        <LegendBox
          title={data?.legend.title ?? 'Vegetation (NDVI)'}
          items={data?.legend.items ?? []}
          open={legendOpen}
          onToggle={() => setLegendOpen((open) => !open)}
        />
      </div>
    </div>
  );
}

function LegendBox({
  title,
  items,
  open,
  onToggle,
}: {
  title: string;
  items: Array<{ key: string; label: string; range: string; color: string }>;
  open: boolean;
  onToggle: () => void;
}) {
  return (
    <div className="bg-white dark:bg-gray-800 px-2 py-1.5 rounded-md shadow-lg text-[11px]">
      <button
        type="button"
        className="flex w-full items-center justify-between gap-2 font-semibold"
        aria-expanded={open}
        onClick={onToggle}
      >
        <span>{title}</span>
        <span aria-hidden>{open ? '▾' : '▸'}</span>
      </button>
      {open && (
        <div className="mt-1 space-y-0.5">
          {items.map((item) => (
            <div key={item.key} className="flex items-center gap-1.5">
              <div className="w-2.5 h-2.5 rounded-sm shrink-0" style={{ backgroundColor: item.color }} />
              <span>
                {item.range}: {item.label}
              </span>
            </div>
          ))}
          <div className="flex items-center gap-1.5">
            <div className="w-2.5 h-2.5 rounded-sm shrink-0" style={{ backgroundColor: NO_DATA_COLOR }} />
            <span>No data yet</span>
          </div>
        </div>
      )}
    </div>
  );
}
