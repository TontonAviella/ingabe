import { Layer, Map as MapGL, type MapRef, NavigationControl, ScaleControl, Source } from '@vis.gl/react-maplibre';
import type { MapGeoJSONFeature } from 'maplibre-gl';
import { useMemo, useRef, useState } from 'react';
import { useH3Grid } from '@/hooks/useRwandaApi';
import 'maplibre-gl/dist/maplibre-gl.css';

interface RwandaMapProps {
  resolution?: number;
  bounds?: string;
  selectedDistrict?: string;
}

const RWANDA_CENTER: [number, number] = [29.87, -1.94];
const RWANDA_ZOOM = 8;
const BASEMAP_URL = 'https://basemaps.cartocdn.com/gl/positron-gl-style/style.json';

// Simplified Rwanda boundary (approximate polygon with ~10 coordinate pairs)
const RWANDA_BOUNDARY: GeoJSON.Feature<GeoJSON.Polygon> = {
  type: 'Feature',
  geometry: {
    type: 'Polygon',
    coordinates: [
      [
        [28.86, -1.04],
        [29.44, -1.04],
        [30.42, -1.13],
        [30.9, -1.69],
        [30.86, -2.31],
        [30.42, -2.84],
        [29.57, -2.74],
        [29.02, -2.55],
        [28.86, -2.22],
        [28.86, -1.04],
      ],
    ],
  },
  properties: {},
};

// NDVI color scale
function getNdviColor(ndvi?: number): string {
  if (ndvi === undefined || ndvi === null) return '#cccccc'; // Gray for no data
  if (ndvi < 0.2) return '#d73027'; // Red - bare soil
  if (ndvi < 0.4) return '#fc8d59'; // Orange - sparse
  if (ndvi < 0.6) return '#fee08b'; // Yellow - moderate
  if (ndvi < 0.8) return '#91cf60'; // Green - healthy
  return '#1a9850'; // Dark green - very healthy
}

export function RwandaMap({ resolution = 7, bounds, selectedDistrict: _selectedDistrict }: RwandaMapProps) {
  const mapRef = useRef<MapRef>(null);
  const [hoveredFeature, setHoveredFeature] = useState<MapGeoJSONFeature | null>(null);
  const [cursorPosition, setCursorPosition] = useState<{ x: number; y: number } | null>(null);

  // Fetch H3 grid data
  const { data: h3GridData, isLoading } = useH3Grid(resolution, bounds || '28.86,-2.84,30.90,-1.04');

  // Create GeoJSON with NDVI-based colors
  const h3GeoJSON = useMemo(() => {
    if (!h3GridData) return null;

    return {
      ...h3GridData,
      features: h3GridData.features.map((feature) => ({
        ...feature,
        properties: {
          ...feature.properties,
          color: getNdviColor(feature.properties.mean_ndvi),
        },
      })),
    };
  }, [h3GridData]);

  const handleMouseMove = (event: maplibregl.MapMouseEvent) => {
    const map = mapRef.current?.getMap();
    if (!map) return;

    const features = map.queryRenderedFeatures(event.point, {
      layers: ['h3-fill-layer'],
    });

    if (features.length > 0) {
      setHoveredFeature(features[0] as MapGeoJSONFeature);
      setCursorPosition({ x: event.point.x, y: event.point.y });
    } else {
      setHoveredFeature(null);
      setCursorPosition(null);
    }
  };

  const handleMouseLeave = () => {
    setHoveredFeature(null);
    setCursorPosition(null);
  };

  return (
    <div className="relative w-full h-full">
      <MapGL
        ref={mapRef}
        initialViewState={{
          longitude: RWANDA_CENTER[0],
          latitude: RWANDA_CENTER[1],
          zoom: RWANDA_ZOOM,
        }}
        mapStyle={BASEMAP_URL}
        style={{ width: '100%', height: '100%' }}
        onMouseMove={handleMouseMove}
        onMouseLeave={handleMouseLeave}
        attributionControl={false}
      >
        <NavigationControl position="top-right" />
        <ScaleControl position="bottom-left" />

        {/* Rwanda boundary outline */}
        <Source
          id="rwanda-boundary"
          type="geojson"
          data={{
            type: 'FeatureCollection',
            features: [RWANDA_BOUNDARY],
          }}
        >
          <Layer
            id="rwanda-boundary-line"
            type="line"
            paint={{
              'line-color': '#333333',
              'line-width': 2,
            }}
          />
        </Source>

        {/* H3 Grid with NDVI colors */}
        {h3GeoJSON && (
          <Source id="h3-grid" type="geojson" data={h3GeoJSON}>
            <Layer
              id="h3-fill-layer"
              type="fill"
              paint={{
                'fill-color': ['get', 'color'],
                'fill-opacity': 0.7,
              }}
            />
            <Layer
              id="h3-line-layer"
              type="line"
              paint={{
                'line-color': '#ffffff',
                'line-width': 0.5,
                'line-opacity': 0.3,
              }}
            />
          </Source>
        )}
      </MapGL>

      {/* Loading indicator */}
      {isLoading && (
        <div className="absolute top-4 left-4 bg-white dark:bg-gray-800 px-3 py-2 rounded-md shadow-md text-sm">Loading H3 grid...</div>
      )}

      {/* Hover popup */}
      {hoveredFeature && cursorPosition && (
        <div
          className="absolute bg-white dark:bg-gray-800 px-3 py-2 rounded-md shadow-lg text-xs pointer-events-none z-10"
          style={{
            left: cursorPosition.x + 10,
            top: cursorPosition.y + 10,
          }}
        >
          <div className="font-semibold mb-1">H3 Cell</div>
          <div>
            <span className="text-gray-600 dark:text-gray-400">Index:</span>{' '}
            <span className="font-mono">{hoveredFeature.properties?.h3_index}</span>
          </div>
          <div>
            <span className="text-gray-600 dark:text-gray-400">Resolution:</span> {resolution}
          </div>
          {hoveredFeature.properties?.mean_ndvi !== undefined && (
            <div>
              <span className="text-gray-600 dark:text-gray-400">NDVI:</span>{' '}
              <span className="font-semibold">{hoveredFeature.properties.mean_ndvi.toFixed(3)}</span>
            </div>
          )}
        </div>
      )}

      {/* NDVI Legend */}
      <div className="absolute bottom-4 right-4 bg-white dark:bg-gray-800 px-4 py-3 rounded-md shadow-lg text-xs">
        <div className="font-semibold mb-2">NDVI Scale</div>
        <div className="space-y-1">
          <div className="flex items-center gap-2">
            <div className="w-4 h-4 rounded" style={{ backgroundColor: '#d73027' }} />
            <span>&lt; 0.2 (Bare soil)</span>
          </div>
          <div className="flex items-center gap-2">
            <div className="w-4 h-4 rounded" style={{ backgroundColor: '#fc8d59' }} />
            <span>0.2-0.4 (Sparse)</span>
          </div>
          <div className="flex items-center gap-2">
            <div className="w-4 h-4 rounded" style={{ backgroundColor: '#fee08b' }} />
            <span>0.4-0.6 (Moderate)</span>
          </div>
          <div className="flex items-center gap-2">
            <div className="w-4 h-4 rounded" style={{ backgroundColor: '#91cf60' }} />
            <span>0.6-0.8 (Healthy)</span>
          </div>
          <div className="flex items-center gap-2">
            <div className="w-4 h-4 rounded" style={{ backgroundColor: '#1a9850' }} />
            <span>&gt; 0.8 (Very healthy)</span>
          </div>
        </div>
      </div>
    </div>
  );
}
