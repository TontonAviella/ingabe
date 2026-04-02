import { apiFetch, getCachedToken, getJwt } from '@mundi/ee';
import { useQuery } from '@tanstack/react-query';
import legendSymbol, { type RenderElement } from 'legend-symbol-ts';
import { injectOverridesIntoStyle, useLayerPaintOverrides } from '../hooks/useLayerPaintOverrides';
import { StyleBridge } from '../lib/StyleBridge';
import { BasemapControl } from './BasemapControl';

function renderTree(tree: RenderElement | null): JSX.Element | null {
  if (!tree) return null;
  return React.createElement(tree.element, tree.attributes, tree.children?.map(renderTree));
}

// deck.gl & friends are imported dynamically to prevent module-level version
// detection throws (deck.gl throws if it detects multiple versions at eval time,
// which kills the entire JS bundle before React can mount).
type DeckGLModules = {
  COORDINATE_SYSTEM: typeof import('@deck.gl/core').COORDINATE_SYSTEM;
  PointCloudLayer: typeof import('@deck.gl/layers').PointCloudLayer;
  GeoJsonLayer: typeof import('@deck.gl/layers').GeoJsonLayer;
  MapboxOverlay: typeof import('@deck.gl/mapbox').MapboxOverlay;
  LASLoader: typeof import('@loaders.gl/las').LASLoader;
  Matrix4: typeof import('@math.gl/core').Matrix4;
};
let _deckModules: DeckGLModules | null = null;
async function getDeckModules(): Promise<DeckGLModules> {
  if (_deckModules) return _deckModules;
  const [core, layers, mapbox, las, math] = await Promise.all([
    import('@deck.gl/core'),
    import('@deck.gl/layers'),
    import('@deck.gl/mapbox'),
    import('@loaders.gl/las'),
    import('@math.gl/core'),
  ]);
  _deckModules = {
    COORDINATE_SYSTEM: core.COORDINATE_SYSTEM,
    PointCloudLayer: layers.PointCloudLayer,
    GeoJsonLayer: layers.GeoJsonLayer,
    MapboxOverlay: mapbox.MapboxOverlay,
    LASLoader: las.LASLoader,
    Matrix4: math.Matrix4,
  };
  return _deckModules;
}

/**
 * NDVI-based color ramp: Red (#d73027) → Orange (#fc8d59) → Yellow (#fee08b) → Green (#1a9850)
 * Returns [r, g, b, alpha] for a given NDVI value (0..1 range).
 */
function ndviColor(ndvi: number): [number, number, number, number] {
  const stops: [number, [number, number, number]][] = [
    [0.0, [215, 48, 39]], // #d73027
    [0.25, [252, 141, 89]], // #fc8d59
    [0.5, [254, 224, 139]], // #fee08b
    [0.75, [26, 152, 80]], // #1a9850
    [1.0, [0, 104, 55]], // #006837
  ];
  const clamped = Math.max(0, Math.min(1, ndvi));
  for (let i = 0; i < stops.length - 1; i++) {
    const [t0, c0] = stops[i];
    const [t1, c1] = stops[i + 1];
    if (clamped >= t0 && clamped <= t1) {
      const t = (clamped - t0) / (t1 - t0);
      return [
        Math.round(c0[0] + t * (c1[0] - c0[0])),
        Math.round(c0[1] + t * (c1[1] - c0[1])),
        Math.round(c0[2] + t * (c1[2] - c0[2])),
        220,
      ];
    }
  }
  return [215, 48, 39, 220];
}

/**
 * Create a deck.gl GeoJsonLayer with 3D extruded polygons for agri indices.
 * NDVI drives both fill colour (red→green) and extrusion height.
 */
async function createAgriIndicesLayer(layerId: string, geojsonUrl: string) {
  const { GeoJsonLayer } = await getDeckModules();
  const response = await apiFetch(geojsonUrl);
  if (!response.ok) throw new Error(`Failed to fetch GeoJSON: ${response.status}`);
  const data = await response.json();

  // Compute min/max NDVI for normalisation
  let minNdvi = Infinity;
  let maxNdvi = -Infinity;
  for (const f of data.features ?? []) {
    const v = f.properties?.ndvi;
    if (typeof v === 'number') {
      if (v < minNdvi) minNdvi = v;
      if (v > maxNdvi) maxNdvi = v;
    }
  }
  if (!isFinite(minNdvi)) minNdvi = 0;
  if (!isFinite(maxNdvi)) maxNdvi = 1;
  const range = maxNdvi - minNdvi || 1;
  const maxHeight = 5000; // metres — visible at country zoom

  return new GeoJsonLayer({
    id: `deckgl-agri-${layerId}`,
    data,
    extruded: true,
    wireframe: true,
    opacity: 0.85,
    getElevation: (f: any) => {
      const v = f.properties?.ndvi ?? 0;
      return ((v - minNdvi) / range) * maxHeight;
    },
    getFillColor: (f: any) => {
      const v = f.properties?.ndvi ?? 0;
      return ndviColor((v - minNdvi) / range);
    },
    getLineColor: [34, 34, 34, 200],
    getLineWidth: 80,
    lineWidthMinPixels: 1,
    pickable: true,
    autoHighlight: true,
    highlightColor: [255, 255, 255, 80],
  });
}

import { bbox } from '@turf/turf';
import { Activity, Brain, Database, Maximize2, Minimize2, MousePointerClick, Send, X, ZoomIn } from 'lucide-react';
import {
  AJAXError,
  type LayerSpecification,
  type MapGeoJSONFeature,
  type MapOptions,
  Map as MLMap,
  NavigationControl,
  ScaleControl,
  type SourceSpecification,
} from 'maplibre-gl';
import type { ChatCompletionUserMessageParam } from 'openai/resources/chat/completions';
import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { Download } from 'react-bootstrap-icons';
import ReactMarkdown from 'react-markdown';
import { ReadyState } from 'react-use-websocket';
import remarkGfm from 'remark-gfm';
import { toast } from 'sonner';
import AttributeTable from '@/components/AttributeTable';
import { BufferPieOverlay, type PieChartData } from '@/components/BufferPieOverlay';
import LayerList from '@/components/LayerList';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Input } from '@/components/ui/input';
import { Tooltip, TooltipContent, TooltipTrigger } from '@/components/ui/tooltip';
import VersionVisualization from '@/components/VersionVisualization';
import type { ErrorEntry, UploadingFile } from '../lib/frontend-types';
import type {
  Conversation,
  EphemeralAction,
  MapData,
  MapLayer,
  MapProject,
  MapTreeResponse,
  MessageSendRequest,
  SanitizedMessage,
} from '../lib/types';

const EMPTY_POINT_CLOUD_LAYERS: MapLayer[] = [];

// Import styles in the parent component
const KUE_MESSAGE_STYLE = `
  text-sm
  [&_table]:w-full [&_table]:border-collapse [&_table]:text-left
  [&_thead]:border-b-1 [&_thead]:border-gray-600
  [&_thead_th]:font-semibold
  [&_tbody_tr]:border-b [&_tbody_tr]:border-gray-200 last:[&_tbody_tr]:border-b-0
  [&_td]:align-top
  [&_a]:text-blue-200 [&_a]:underline
  [&_img]:h-auto [&_img]:block [&_img]:mx-auto
  [&_img]:my-2 [&_img]:w-[320px] [&_img]:border
  [&_img]:border-[#aaa] [&_img]:rounded-md
`;

// SWAP_XY is created lazily via getDeckModules() since Matrix4 is dynamically imported.

interface MapLibreMapProps {
  mapId: string;
  width?: string;
  height?: string;
  className?: string;
  project: MapProject;
  mapData?: MapData | null;
  mapTree: MapTreeResponse | null;
  conversationId: number | null;
  conversations: Conversation[];
  conversationsEnabled: boolean;
  setConversationId: (conversationId: number | null) => void;
  readyState: number;
  openDropzone?: () => void;
  invalidateProjectData: () => void;
  uploadingFiles?: UploadingFile[];
  hiddenLayerIDs: string[];
  toggleLayerVisibility: (layerId: string) => void;
  mapRef: React.RefObject<MLMap | null>;
  activeActions: EphemeralAction[];
  setActiveActions: React.Dispatch<React.SetStateAction<EphemeralAction[]>>;
  zoomHistory: Array<{ bounds: [number, number, number, number] }>;
  zoomHistoryIndex: number;
  setZoomHistoryIndex: React.Dispatch<React.SetStateAction<number>>;
  addError: (message: string, shouldOverrideMessages?: boolean, sourceId?: string) => void;
  dismissError: (errorId: string) => void;
  errors: ErrorEntry[];
  invalidateMapData: () => void;
}

// Known basemap source IDs — these are the only sources that should be
// removed/replaced when switching basemaps (overlay sources stay untouched).
// Defined at module level so the Set isn't recreated on every render.
const BASEMAP_SOURCE_IDS = new Set([
  'openstreetmap',
  'esri-satellite',
  'esri-topo',
  'carto-dark',
  'carto-voyager',
  'sentinel2-live',
  'ndvi-map',
  'basemap-underlay',
  // OpenFreeMap vector style uses these source IDs:
  'ne2_shaded',
  'openmaptiles',
]);

export default function MapLibreMap({
  mapId,
  width = '100%',
  height = '500px',
  className = '',
  project,
  mapData,
  mapTree,
  conversationId,
  conversations,
  conversationsEnabled,
  setConversationId,
  readyState,
  openDropzone,
  uploadingFiles,
  hiddenLayerIDs,
  toggleLayerVisibility,
  mapRef,
  activeActions,
  setActiveActions,
  zoomHistory,
  zoomHistoryIndex,
  setZoomHistoryIndex,
  addError,
  dismissError,
  errors,
  invalidateProjectData,
  invalidateMapData,
}: MapLibreMapProps) {
  const mapContainerRef = useRef<HTMLDivElement>(null);
  const localMapRef = useRef<MLMap | null>(null);
  const basemapControlRef = useRef<BasemapControl | null>(null);
  const styleBridgeRef = useRef<StyleBridge | null>(null);
  const deckOverlayRef = useRef<any>(null);
  // Incremented each time the map is destroyed/recreated so dependent effects
  // (controls, basemap, click handler) re-attach to the new map instance.
  const [mapInstanceId, setMapInstanceId] = useState(0);
  // hasZoomed is tracked via hasZoomedRef (line ~1087) to avoid triggering setStyle re-runs
  const [layerSymbols, setLayerSymbols] = useState<{
    [layerId: string]: JSX.Element;
  }>({});
  const [loadingSourceIds, setLoadingSourceIds] = useState<Set<string>>(new Set());
  const [assistantExpanded, setAssistantExpanded] = useState(false);
  const [isMapReady, setIsMapReady] = useState(false);
  const [pieOverlays, setPieOverlays] = useState<Map<string, PieChartData>>(new Map());
  const [sceneInfo, setSceneInfo] = useState<{
    scene_date: string | null;
    cloud_cover: number | null;
    scenes_available: number;
  } | null>(null);
  const [isSentinel2Active, setIsSentinel2Active] = useState(false);
  const [mosaicMode, setMosaicMode] = useState<'leastCC' | 'mostRecent'>('leastCC');

  const {
    overrides: paintOverrides,
    overridesRef: paintOverridesRef,
    setLayerOpacity,
    setLayerColor,
  } = useLayerPaintOverrides({
    map: localMapRef.current,
    mapId,
    isMapReady,
  });

  const { data: basemapsData } = useQuery({
    queryKey: ['basemaps', 'available'],
    queryFn: async () => {
      const response = await apiFetch('/api/basemaps/available');
      if (!response.ok) {
        throw new Error('Failed to fetch basemaps');
      }
      return (await response.json()) as { styles: string[]; display_names?: Record<string, string> };
    },
  });
  const availableBasemaps = basemapsData?.styles ?? [];
  const basemapDisplayNames = basemapsData?.display_names ?? {};

  // Track per-source loading state: listeners are attached after map load

  const loadingLayerIDs = useMemo(() => {
    if (!mapData?.layers) return [] as string[];
    return mapData.layers.map((l) => l.id).filter((id) => loadingSourceIds.has(id));
  }, [mapData?.layers, loadingSourceIds]);

  const { data: demoConfigData } = useQuery({
    queryKey: ['projects', 'config', 'demo-postgis-available'],
    queryFn: async () => {
      const response = await apiFetch('/api/projects/config/demo-postgis-available');
      if (!response.ok) {
        throw new Error('Failed to fetch demo config');
      }
      return (await response.json()) as { available: boolean; description: string };
    },
  });
  const demoConfig = demoConfigData ?? { available: false, description: '' };

  const pointCloudLayers = useMemo(() => {
    const filtered = mapData?.layers?.filter((layer) => layer.type === 'point_cloud') ?? EMPTY_POINT_CLOUD_LAYERS;
    return filtered.length === 0 ? EMPTY_POINT_CLOUD_LAYERS : filtered;
  }, [mapData?.layers]);

  // Layers flagged for deck.gl 3D extrusion (agri indices choropleth)
  const deckgl3dLayers = useMemo(() => {
    return mapData?.layers?.filter((layer) => (layer.metadata as any)?.deckgl_3d === true) ?? [];
  }, [mapData?.layers]);

  const createPointCloudLayer = useCallback(async (pclayer: MapLayer) => {
    const { COORDINATE_SYSTEM, PointCloudLayer, LASLoader, Matrix4 } = await getDeckModules();
    const SWAP_XY = new Matrix4().set(0, 1, 0, 0, 1, 0, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1);

    // some projection-foo to compensate for web mercator (gross!) and
    // latitude-longitude disagreements (SWAP_XY)
    const { lon, lat } = pclayer.metadata?.pointcloud_anchor as { lon: number; lat: number };
    if (!lon || !lat) {
      console.error('no anchor', pclayer);
      return;
    }
    const R = 6378137;
    const d2r = Math.PI / 180;
    const cosA = Math.cos(lat * d2r);

    const mPerDegLon = R * d2r * cosA;
    const mPerDegLat = R * d2r;
    const translate = new Matrix4().translate([-lon, -lat, 0]);
    const scale = new Matrix4().scale([mPerDegLon, mPerDegLat, 1]);
    const modelMatrix = scale.multiplyRight(translate).multiplyRight(SWAP_XY);

    const layer = new PointCloudLayer({
      id: `point-cloud-layer-${pclayer.id}`,
      data: `/api/layer/${pclayer.id}.laz`,
      loaders: [LASLoader],
      loadOptions: {
        las: {
          fp64: true,
        },
      },
      modelMatrix: modelMatrix,
      coordinateSystem: COORDINATE_SYSTEM.METER_OFFSETS,
      coordinateOrigin: [lon, lat, 0],
      getColor: (_d, dinfo) => {
        const mesh = (dinfo.data as any).loaderData;

        if (!mesh.maxs || !mesh.mins) {
          return [100, 100, 255, 255];
        }

        // TODO: improve this. its a fast percentile approximation
        // but life can always be better. pastures are greener
        const pointData = dinfo.data as any;
        const currentZ = pointData.attributes.POSITION.value[dinfo.index * 3 + 2];

        if (!mesh.percentileCache) {
          const numPoints = pointData.attributes.POSITION.value.length / 3;
          const sampleSize = Math.min(5000, numPoints);
          const zValues = [];

          for (let i = 0; i < sampleSize; i++) {
            const idx = Math.floor((i / sampleSize) * numPoints) * 3 + 2;
            zValues.push(pointData.attributes.POSITION.value[idx]);
          }

          zValues.sort((a, b) => a - b);
          mesh.percentileCache = {
            p5: zValues[Math.floor(sampleSize * 0.05)],
            p95: zValues[Math.floor(sampleSize * 0.95)],
          };
        }

        const { p5, p95 } = mesh.percentileCache;
        const range = p95 - p5;

        if (range === 0) {
          return [100, 100, 255, 255];
        }

        const clampedZ = Math.max(p5, Math.min(p95, currentZ));
        const normalizedZ = (clampedZ - p5) / range;

        // TODO: interpolate between two pretty colors
        const r = Math.round(normalizedZ * 255);
        const g = Math.round(normalizedZ * 255);
        const b = Math.round((1 - normalizedZ) * 255);
        return [r, g, b, 255];
      },
      pointSize: 1,
      onError: (error: any) => {
        console.error('Point cloud loading error: ' + error.message);
      },
    });
    return layer;
  }, []);

  const [showAttributeTable, setShowAttributeTable] = useState(false);
  const [selectedLayer, setSelectedLayer] = useState<MapLayer | null>(null);

  const [isCancelling, setIsCancelling] = useState(false);

  // Function to handle basemap changes — merges the new basemap into the
  // current style and applies it with a single setStyle() call.
  // This is more robust than imperatively adding/removing layers because
  // setStyle() handles all the internal bookkeeping atomically.
  const handleBasemapChange = useCallback(
    async (newBasemap: string) => {
      const map = localMapRef.current;
      if (!map) return;

      // Parse map ID from URL
      const pathParts = window.location.pathname.split('/');
      const urlMapId = pathParts.length > 3 ? pathParts[3] : mapId;

      try {
        // 1. Fetch the new basemap style from the lightweight endpoint
        const response = await apiFetch(`/api/basemaps/${newBasemap}/style.json`);
        if (!response.ok) {
          console.error('Failed to fetch basemap style:', await response.text());
          return;
        }
        const newBasemapStyle = await response.json();

        // 2. Build a merged style: new basemap sources/layers + existing overlay sources/layers
        const currentStyle = map.getStyle();
        if (!currentStyle) return;

        // Separate overlay sources/layers from basemap ones
        const overlaySources: Record<string, SourceSpecification> = {};
        const overlayLayers: LayerSpecification[] = [];

        if (currentStyle.sources) {
          for (const [id, src] of Object.entries(currentStyle.sources)) {
            if (!BASEMAP_SOURCE_IDS.has(id)) {
              overlaySources[id] = src as SourceSpecification;
            }
          }
        }
        if (currentStyle.layers) {
          for (const layer of currentStyle.layers) {
            const src = 'source' in layer ? layer.source : undefined;
            if (typeof src === 'string' && BASEMAP_SOURCE_IDS.has(src)) continue;
            if (layer.id === 'basemap-underlay-layer') continue;
            overlayLayers.push(layer as LayerSpecification);
          }
        }

        // 3. Compose merged style
        // Only add Esri underlay for TRUE-COLOR satellite (not NDVI — its green/red
        // output looks nothing like satellite imagery, so an Esri underlay is misleading)
        const needsUnderlay = newBasemap === 'sentinel2_live';
        const mergedSources: Record<string, SourceSpecification> = {};
        const mergedLayers: LayerSpecification[] = [];

        // For Sentinel-2 Live, add Esri underlay first (bottom-most)
        if (needsUnderlay) {
          mergedSources['basemap-underlay'] = {
            type: 'raster',
            tiles: ['https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}'],
            tileSize: 256,
            maxzoom: 18,
          } as SourceSpecification;
          mergedLayers.push({
            id: 'basemap-underlay-layer',
            type: 'raster',
            source: 'basemap-underlay',
            layout: { visibility: 'visible' },
            paint: {},
          } as LayerSpecification);
        }

        // New basemap sources and layers
        for (const [id, src] of Object.entries(newBasemapStyle.sources || {})) {
          mergedSources[id] = src as SourceSpecification;
        }
        for (const layer of newBasemapStyle.layers || []) {
          const pushed = layer as LayerSpecification;
          // When Esri underlay is present, fade Sentinel-2 at high zoom so the
          // sharp Esri imagery shows through beyond Sentinel-2's native 10m resolution.
          if (needsUnderlay && pushed.type === 'raster' && 'source' in pushed && pushed.source === 'sentinel2-live') {
            (pushed as any).paint = {
              ...(pushed as any).paint,
              'raster-opacity': ['interpolate', ['linear'], ['zoom'], 14, 1, 17, 0.25],
            };
          }
          mergedLayers.push(pushed);
        }

        // Overlay sources and layers (on top)
        for (const [id, src] of Object.entries(overlaySources)) {
          mergedSources[id] = src;
        }
        for (const layer of overlayLayers) {
          mergedLayers.push(layer);
        }

        // Inject paint overrides so they survive the setStyle diff
        const mergedStyle = {
          ...currentStyle,
          sources: mergedSources,
          layers: mergedLayers,
          // Use glyphs/sprite from new basemap if available, fall back to current
          glyphs: newBasemapStyle.glyphs || currentStyle.glyphs,
          sprite: newBasemapStyle.sprite || currentStyle.sprite,
          metadata: {
            ...(currentStyle.metadata || {}),
            current_basemap: newBasemap,
          },
        };
        injectOverridesIntoStyle(mergedStyle, paintOverridesRef.current);

        // Preserve projection
        const currentProjection = map.getProjection();

        // 4. Apply with setStyle — MapLibre diffs and updates atomically
        map.setStyle(mergedStyle);

        // Re-apply non-mercator projection
        if (currentProjection?.type && currentProjection.type !== 'mercator') {
          map.once('style.load', () => {
            map.setProjection(currentProjection);
          });
        }

        // 5. Track Sentinel-2 Live for scene info overlay
        setIsSentinel2Active(newBasemap === 'sentinel2_live' || newBasemap === 'ndvi_map');
        if (newBasemap !== 'sentinel2_live' && newBasemap !== 'ndvi_map') setSceneInfo(null);

        // 6. Persist basemap choice to DB (fire-and-forget, don't block UI)
        apiFetch(`/api/maps/${urlMapId}`, {
          method: 'PATCH',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ basemap: newBasemap }),
        }).catch((err) => console.error('Failed to persist basemap:', err));
      } catch (error) {
        console.error('Error switching basemap:', error);
        toast.error('Failed to switch basemap. Please try again.');
      }
    },
    [mapId],
  );

  // Function to get the appropriate icon for an action
  const getActionIcon = (action: string) => {
    if (action.includes('thinking')) {
      return <Brain className="animate-pulse w-4 h-4 mr-2" />;
    } else if (action.includes('Downloading data from OpenStreetMap')) {
      return <Download className="animate-pulse w-4 h-4 mr-2" />;
    } else if (action.includes('SQL')) {
      return <Database className="animate-pulse w-4 h-4 mr-2" />;
    } else if (action.includes('Sending message')) {
      return <Send className="animate-pulse w-4 h-4 mr-2" />;
    } else {
      return <Activity className="w-4 h-4 mr-2 animate-pulse" />;
    }
  };

  // State for changelog entries
  // State for changelog entries from map data
  const [__changelog, setChangelog] = useState<
    Array<{
      summary: string;
      timestamp: string;
      mapState: string;
    }>
  >([]);

  // Process changelog data when mapData changes
  useEffect(() => {
    if (mapData?.changelog) {
      const formattedChangelog = mapData.changelog.map((entry) => ({
        summary: entry.message,
        timestamp: new Date(entry.last_edited).toLocaleTimeString([], {
          hour: '2-digit',
          minute: '2-digit',
        }),
        mapState: entry.map_state,
      }));
      setChangelog(formattedChangelog);
    }
  }, [mapData]);

  useEffect(() => {
    if (isCancelling) {
      const cancelActions = async () => {
        await apiFetch(`/api/maps/${mapId}/messages/cancel`, {
          method: 'POST',
          headers: {
            'Content-Type': 'application/json',
          },
          body: JSON.stringify({}),
        });

        toast.success('Actions cancelled');
        setIsCancelling(false);
      };

      cancelActions();
    }
  }, [isCancelling, mapId]);

  const [selectedFeature, setSelectedFeature] = useState<MapGeoJSONFeature | null>(null);

  const selectFeature = useCallback(
    (feat: MapGeoJSONFeature | null) => {
      if (!mapRef.current) return;
      const newMap = mapRef.current;

      setSelectedFeature((prev: MapGeoJSONFeature | null) => {
        if (prev) {
          newMap.setFeatureState({ source: prev.source, sourceLayer: prev.sourceLayer, id: prev.id }, { selected: false });
        }

        if (feat) {
          newMap.setFeatureState({ source: feat.source, sourceLayer: feat.sourceLayer, id: feat.id }, { selected: true });
        }

        return feat;
      });
    },
    [mapRef],
  );

  const UPDATE_KUE_POINTER_MSEC = 40;
  const KUE_CURVE_DURATION_MS = 2000;

  // State for Kue's animated positions (indexed by action_id)
  const [kuePositions, setKuePositions] = useState<Record<string, { lng: number; lat: number }>>({});
  const [kueTargetPoints, setKueTargetPoints] = useState<Record<string, Array<{ lng: number; lat: number }>>>({});

  // Generate random points within layer bounds
  const generateRandomPointsInBounds = useCallback((bounds: number[], count: number = 3) => {
    const [minLng, minLat, maxLng, maxLat] = bounds;
    const points = [];

    for (let i = 0; i < count; i++) {
      points.push({
        lng: minLng + Math.random() * (maxLng - minLng),
        lat: minLat + Math.random() * (maxLat - minLat),
      });
    }

    return points;
  }, []);

  // Quadratic Bezier curve interpolation from p0 to p2 through p1
  const bezierInterpolate = useCallback(
    (p0: { lng: number; lat: number }, p1: { lng: number; lat: number }, p2: { lng: number; lat: number }, t: number) => {
      const invT = 1 - t;
      return {
        lng: invT * invT * p0.lng + 2 * invT * t * p1.lng + t * t * p2.lng,
        lat: invT * invT * p0.lat + 2 * invT * t * p1.lat + t * t * p2.lat,
      };
    },
    [],
  );

  // Update Kue's target points when active actions change
  useEffect(() => {
    const activeLayerActions = activeActions.filter((action) => action.status === 'active' && action.layer_id);

    // Get current action IDs
    const currentActionIds = new Set(activeLayerActions.map((action) => action.action_id));

    // Remove state for actions that are no longer active
    setKuePositions((prev) => {
      const filtered = Object.fromEntries(Object.entries(prev).filter(([actionId]) => currentActionIds.has(actionId)));
      return filtered;
    });
    setKueTargetPoints((prev) => {
      const filtered = Object.fromEntries(Object.entries(prev).filter(([actionId]) => currentActionIds.has(actionId)));
      return filtered;
    });

    // Add state for new actions
    if (mapData?.layers) {
      activeLayerActions.forEach((action) => {
        const layer = mapData.layers.find((l) => l.id === action.layer_id);
        if (layer?.bounds && layer.bounds.length >= 4) {
          const actionId = action.action_id;

          // Only initialize if not already present
          setKueTargetPoints((prev) => {
            if (prev[actionId]) return prev;
            const newTargetPoints = generateRandomPointsInBounds(layer.bounds!);
            return { ...prev, [actionId]: newTargetPoints };
          });

          setKuePositions((prev) => {
            if (prev[actionId]) return prev;
            const newTargetPoints = generateRandomPointsInBounds(layer.bounds!);
            return { ...prev, [actionId]: newTargetPoints[0] };
          });
        }
      });
    }
  }, [activeActions, mapData, generateRandomPointsInBounds]);

  // Animate Kue's positions based on timestamp
  useEffect(() => {
    const activeActionIds = Object.keys(kueTargetPoints);
    if (activeActionIds.length === 0) return;

    const interval = setInterval(() => {
      const now = Date.now();

      activeActionIds.forEach((actionId) => {
        const targetPoints = kueTargetPoints[actionId];

        if (targetPoints && targetPoints.length >= 2) {
          // Calculate progress based on timestamp modulo curve duration
          const progress = (now % KUE_CURVE_DURATION_MS) / KUE_CURVE_DURATION_MS;

          // Check if we've started a new curve cycle
          const currentCycle = Math.floor(now / KUE_CURVE_DURATION_MS);
          const lastCycle = Math.floor((now - UPDATE_KUE_POINTER_MSEC) / KUE_CURVE_DURATION_MS);

          if (currentCycle !== lastCycle) {
            // Generate new random points for the new curve
            const layer = mapData?.layers?.find((l) => activeActions.find((a) => a.action_id === actionId)?.layer_id === l.id);
            if (layer?.bounds) {
              const newTargetPoints = generateRandomPointsInBounds(layer.bounds);
              setKueTargetPoints((prev) => ({
                ...prev,
                [actionId]: newTargetPoints,
              }));
              return; // Skip position update this frame to use new points next frame
            }
          }

          const startPoint = targetPoints[0];
          const middlePoint = targetPoints[1];
          const endPoint = targetPoints[2];

          const interpolatedPosition = bezierInterpolate(startPoint, middlePoint, endPoint, progress);

          setKuePositions((prev) => ({
            ...prev,
            [actionId]: interpolatedPosition,
          }));
        }
      });
    }, UPDATE_KUE_POINTER_MSEC);

    return () => clearInterval(interval);
  }, [kueTargetPoints, activeActions, mapData, bezierInterpolate, generateRandomPointsInBounds]);

  // Generate GeoJSON from pointer positions
  const pointsGeoJSON = useMemo(() => {
    const features: GeoJSON.Feature[] = [];

    // Add Kue's animated positions
    Object.entries(kuePositions).forEach(([actionId, position]) => {
      features.push({
        type: 'Feature' as const,
        geometry: {
          type: 'Point' as const,
          coordinates: [position.lng, position.lat],
        },
        properties: { user: 'Sage', abbrev: 'Sage', color: '#ff69b4', actionId },
      });
    });

    return {
      type: 'FeatureCollection' as const,
      features,
    };
  }, [kuePositions]);

  const loadLegendSymbols = useCallback(
    (map: MLMap) => {
      const style = map.getStyle();

      // Check if style and style.layers exist before proceeding
      if (!style || !style.layers) return;

      mapData?.layers.forEach((layer) => {
        const layerId = layer.id;

        const mapLayer = style.layers.find((styleLayer) => 'source' in styleLayer && (styleLayer as any).source === layerId);

        if (mapLayer) {
          const tree: RenderElement | null = legendSymbol({
            sprite: style.sprite,
            zoom: map.getZoom(),
            layer: mapLayer as any,
          });
          // long lasting bug
          if (tree?.attributes?.style?.backgroundImage === 'url(null)') {
            tree.attributes.style.backgroundImage = 'none';
            tree.attributes.style.width = '16px';
            tree.attributes.style.height = '16px';
            tree.attributes.style.opacity = '1.0';
          }

          const symbolElement = renderTree(tree);
          if (symbolElement) {
            setLayerSymbols((prev) => ({
              ...prev,
              [layerId]: symbolElement as JSX.Element,
            }));
          }
        }
      });
    },
    [mapData],
  );

  // effect runs when map initializes AND when new point clouds are added
  useEffect(() => {
    if (!mapContainerRef.current) return;

    // need to nuke in order to re-draw, TODO this can be improved
    if (localMapRef.current) {
      localMapRef.current.remove();
      localMapRef.current = null;
    }
    if (mapRef.current) {
      (mapRef as any).current = null;
    }

    try {
      // Initialize the map with a basic style first
      const mapOptions: MapOptions = {
        container: mapContainerRef.current,
        style: {
          version: 8,
          sources: {},
          layers: [],
        }, // Start with empty style so map loads
        attributionControl: {
          compact: false,
        },
        transformRequest: (url: string) => {
          // Inject Clerk Bearer token into tile/API requests to the same origin
          if (url.startsWith('/api/') || url.startsWith(window.location.origin + '/api/')) {
            const token = getCachedToken();
            if (token) {
              return { url, headers: { Authorization: `Bearer ${token}` } };
            }
          }
          return { url };
        },
      };

      const newMap = new MLMap(mapOptions);
      localMapRef.current = newMap;
      if (mapRef.current !== undefined) {
        (mapRef as any).current = newMap;
      }

      // Add navigation controls immediately — they're pure-DOM controls
      // that don't need the style to be loaded.
      newMap.addControl(new NavigationControl(), 'top-right');
      newMap.addControl(new ScaleControl(), 'bottom-left');

      // Signal dependent effects (controls, basemap, click handler) to re-attach
      setMapInstanceId((id) => id + 1);

      // Define cursor image loading function
      const loadCursorImage = () => {
        const cursorImage = new Image();
        cursorImage.onload = () => {
          if (newMap.isStyleLoaded()) {
            if (newMap.hasImage('remote-cursor')) {
              newMap.removeImage('remote-cursor');
            }
            newMap.addImage('remote-cursor', cursorImage);
          }
        };
        cursorImage.src =
          'data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAADIAAAAyCAYAAAAeP4ixAAAACXBIWXMAAAsTAAALEwEAmpwYAAAAAXNSR0IArs4c6QAAAARnQU1BAACxjwv8YQUAAAIRSERBVHgB7dnNsdowFAXgQ5INO9OBt9m5BKUDOsAl0AHuIO4AUgF0YKgAOrCpwLDL7kbnPUHAETEY8yy98TejsWf8fnQsc3UBoNfr9WrkZoTwnMxmM8EnCCP0GcLIyXw+P4WJ4CG5tFwuZTQalfAwjFRtt1sJgoCrM4FHxCbPcwnDkGGm8ITcchFmBg/I//gURur4EkbuUZalRFHEMD/hKLkXw4zHY4aZw0HyqMlkwjBbPQI4RJowLQ3DhHCENOVafybPcCmMPMuVMNIGFzpnaUvXnbO0qcvOWdrWVecsr9BFfyav0iTMAM3xf+JZh8PhPIqieDvu93vsdjusVqu75/gNH2iz2SBN0/OEzTjoS6dRmOPeHHf4APIodsH8PT0U3jfB1prHL3gR3nXzaJzp8oo4jnmq8Pfud672xcpRlWUZV4SbnzOtvDXExcYW65Fx4lVKqdN1J1hDmFZjbH4m5qRvrEoGR1xNbrFY2PolPj4lA95YFQUHXIXA7Q42mU6nTq/K24SSJKl7TxFwpVh6q8zurdCCr2guGQwG0EEKff4D7+XU5rf2fTgcRvpxurpwPB6xXq9DffoLHXrkGyvFSlbFVTIV7p6/4QxrKTaPZgqPKFsp5qqYaufUZ111StuqsKrpawk8Yi3FbGngWNtS559SzBUym2MOz6R8gfNjIBOAm2IMD3H3591nAIVez21/ACUSSP4DF2G8AAAAAElFTkSuQmCC';
      };

      newMap.on('load', () => {
        setIsMapReady(true);
        // deck.gl overlay — dynamically imported and isolated so it can't prevent other setup
        (async () => {
          try {
            const { MapboxOverlay } = await getDeckModules();

            // Point cloud layers
            const overlaidPCLayers = await Promise.all(pointCloudLayers.map((layer) => createPointCloudLayer(layer)));

            // 3D extruded agri indices layers
            const overlaidAgriLayers = await Promise.all(
              deckgl3dLayers.map((layer) =>
                createAgriIndicesLayer(layer.id, `/api/layer/${layer.id}.geojson`).catch((err) => {
                  console.error(`Error creating agri 3D layer for ${layer.id}:`, err);
                  return null;
                }),
              ),
            );

            const allDeckLayers = [...overlaidPCLayers, ...overlaidAgriLayers].filter(Boolean);
            const deckOverlay = new MapboxOverlay({
              interleaved: true,
              layers: allDeckLayers,
            });
            deckOverlayRef.current = deckOverlay;
            newMap.addControl(deckOverlay);
          } catch (deckErr) {
            console.error('Error initializing deck.gl overlay:', deckErr);
          }
        })();

        // Load cursor image initially
        loadCursorImage();

        // Attach source data loading listeners
        const clearLoading = (id: string) => {
          setLoadingSourceIds((prev) => {
            if (!prev.has(id)) return prev;
            const next = new Set(prev);
            next.delete(id);
            return next;
          });
        };
        const addLoading = (id: string) => {
          setLoadingSourceIds((prev) => {
            if (prev.has(id)) return prev;
            const next = new Set(prev);
            next.add(id);
            return next;
          });
        };

        const onSourceDataLoading = (e: any) => {
          const id = (e && (e.sourceId || (e.source && e.source.id))) as string | undefined;
          if (id) addLoading(id);
        };
        const onSourceData = (e: any) => {
          const id = (e && (e.sourceId || (e.source && e.source.id))) as string | undefined;
          if (!id) return;
          if (e?.sourceDataType === 'idle' || e?.isSourceLoaded === true) {
            clearLoading(id);
          }
        };
        const onStyleData = () => setLoadingSourceIds(new Set());

        newMap.on('sourcedataloading', onSourceDataLoading);
        newMap.on('sourcedata', onSourceData);
        newMap.on('styledata', onStyleData);
      });

      newMap.on('error', (e) => {
        // Ignore errors from the pointer-positions source — the remote-cursor
        // image is loaded asynchronously after map.on('load') and MapLibre
        // briefly complains about the missing sprite entry before it's added.
        if ((e as any).sourceId === 'pointer-positions') return;

        // Suppress spurious worker errors from maplibre-gl's blob worker.
        // In dev mode the message is "__publicField is not defined" (full esbuild helper name);
        // in production builds the variable is minified to 1-3 chars (e.g. "de is not defined").
        // Also suppress deck.gl multi-version warnings surfaced as map error events.
        if (e.error?.message && /^(__\w+|[\w$]{1,3}) is not defined$/.test(e.error.message)) return;
        if (e.error?.message?.includes('multiple versions detected')) return;

        // Suppress satellite tile errors — cloud cover gaps and validation errors
        // are expected and shouldn't show as user-facing error banners.
        if (e.error?.url?.includes('/api/satellite/')) return;

        if (e.error instanceof AJAXError) {
          // 401 on tile requests = expired Clerk token. Refresh and reload tiles
          // instead of showing a confusing "Token expired" error to the user.
          if (e.error.status === 401) {
            (async () => {
              const freshToken = await getJwt();
              if (freshToken) {
                // Token refreshed successfully, reload map sources to retry tiles
                const m = localMapRef.current;
                if (m) {
                  const style = m.getStyle();
                  if (style?.sources) {
                    for (const [id, src] of Object.entries(style.sources)) {
                      if ('tiles' in (src as any)) {
                        // Force MapLibre to re-request tiles with the new cached token
                        const source = m.getSource(id);
                        if (source && 'setTiles' in source) {
                          (source as any).setTiles((src as any).tiles);
                        }
                      }
                    }
                  }
                }
              } else {
                // Clerk session is fully dead, user needs to re-login
                addError('Session expired. Please refresh the page to sign in again.', true);
              }
            })();
            return; // Don't show "Token expired" error for tile requests
          }
          // Non-auth 4xx errors: show the user the message
          if (e.error.status >= 400 && e.error.status < 500 && e.error.body instanceof Blob) {
            // Read the body of the error
            (async () => {
              const bodyStr = await e.error.body.text();
              try {
                const bodyObj = JSON.parse(bodyStr);

                if ('detail' in bodyObj) {
                  const detail = bodyObj.detail;
                  addError(typeof detail === 'string' ? detail : JSON.stringify(detail), true);
                } else if ('message' in bodyObj && bodyObj['message'] === 'try refresh token') {
                  addError('Session expired, please refresh the page', true);
                } else {
                  addError(bodyStr, true);
                }
              } catch {
                addError(bodyStr, true);
              }
            })();
          } else if (e.error.status == 502 && e.error.message.indexOf('.mvt') !== -1) {
            // This just means database is slow
            const sourceId = 'sourceId' in e && typeof e.sourceId === 'string' ? e.sourceId : undefined;
            addError('PostGIS query took 60+ seconds, database might be overloaded', true, sourceId);
          } else if (e.error.status == 500 && e.error.message.indexOf('.mvt') !== -1) {
            // Potentially an error with the query
            const sourceId = 'sourceId' in e && typeof e.sourceId === 'string' ? e.sourceId : undefined;
            addError('PostGIS query errored while executing, either re-create a new query or contact support', true, sourceId);
          } else {
            // Unknown type of error?
            addError('Error loading map data: ' + e.error.message, true);
          }
        } else {
          // Non-AJAXError path: MapLibre often emits plain Error for tile requests.
          const sourceId = 'sourceId' in e && typeof e.sourceId === 'string' ? e.sourceId : undefined;
          // Suppress satellite/underlay tile errors (cloud gaps, network timeouts, etc.)
          if (sourceId === 'sentinel2-live' || sourceId === 'ndvi-map' || sourceId === 'basemap-underlay') return;
          const msg = (e as any)?.error?.message as string | undefined;
          if (typeof msg === 'string') {
            const match = msg.match(/Bad response code:\s*(\d+)/);
            const code = match ? parseInt(match[1], 10) : null;
            if (code === 423) {
              addError('Vector tiles are still generating. Please refresh in a moment. This will take 2-3 minutes.', true, sourceId);
              return;
            }
            if (code === 502) {
              // 502 for tile/pmtiles requests means the file isn't in storage yet —
              // not actionable by the user, so log quietly instead of toasting.
              console.warn('Tile source returned 502 (file may be missing from storage)', sourceId);
              return;
            }
          }
          addError('Error loading map data: ' + (msg ?? 'Unknown error'), true, sourceId);
        }
      });

      newMap.on('style.load', () => {
        loadCursorImage();
      });

      // Clean up on unmount
      return () => {
        setIsMapReady(false);
        newMap.remove();
        localMapRef.current = null;
        if (mapRef.current !== undefined) {
          (mapRef as any).current = null;
        }
      };
    } catch (err) {
      console.error('Error initializing map:', err);
      addError('Failed to initialize map: ' + (err instanceof Error ? err.message : String(err)), true);
    }
  }, [addError, pointCloudLayers, createPointCloudLayer, mapRef]); // listen to point cloud layers

  // biome-ignore lint/correctness/useExhaustiveDependencies: mapInstanceId is an intentional trigger-only dep
  useEffect(() => {
    const map = mapRef.current;
    if (!map) return;

    const onClick = (e: any) => {
      // Use a small bounding box around the click point so that line-only
      // layers (polygons rendered as outlines without a fill) are still
      // clickable when the user clicks inside the polygon area.
      const tolerance = 5;
      const bbox: [maplibregl.PointLike, maplibregl.PointLike] = [
        [e.point.x - tolerance, e.point.y - tolerance],
        [e.point.x + tolerance, e.point.y + tolerance],
      ];
      const features = map.queryRenderedFeatures(bbox);
      // Find the first feature that belongs to one of our layers (L-prefixed IDs)
      const appFeature = features.find(
        (f) => typeof f.source === 'string' && f.source.startsWith('L') && f.source.length === 12,
      );
      if (appFeature) {
        selectFeature(appFeature);
      } else {
        selectFeature(null);
      }
    };

    map.on('click', onClick);
    return () => {
      map.off('click', onClick);
    };
  }, [mapRef, selectFeature, mapInstanceId]);

  // StyleBridge: replays imperative sources/layers after setStyle().
  // mapInstanceId ensures this re-runs when the map is destroyed/recreated.
  // biome-ignore lint/correctness/useExhaustiveDependencies: mapInstanceId is an intentional trigger-only dep
  useEffect(() => {
    const map = mapRef.current;
    if (!map || !mapId) return;

    let bridge: StyleBridge | null = null;
    let cancelled = false;

    const setup = () => {
      if (cancelled) return;
      bridge = new StyleBridge(map);
      styleBridgeRef.current = bridge;
    };

    if (map.isStyleLoaded()) {
      setup();
    } else {
      map.once('style.load', setup);
    }

    return () => {
      cancelled = true;
      map.off('style.load', setup);
      if (bridge) bridge.destroy();
      styleBridgeRef.current = null;
    };
  }, [mapRef, mapId, mapInstanceId]);

  const styleUpdateCounter = useMemo(() => {
    return activeActions.filter((a) => a.updates.style_json).length;
  }, [activeActions]);

  // Use useQuery to fetch the style.json
  const { data: styleData } = useQuery({
    queryKey: ['mapStyle', mapId, styleUpdateCounter],
    queryFn: async () => {
      const url = new URL(`/api/maps/${mapId}/style.json`, window.location.origin);
      url.searchParams.set('only_show_inline_sources', 'true');
      const response = await apiFetch(url.toString());
      if (!response.ok) {
        throw new Error(`Failed to fetch style: ${response.statusText}`);
      }
      const style = await response.json();
      // Resolve relative tile URLs to absolute (MapLibre requires absolute URLs)
      const origin = window.location.origin;
      if (style.sources) {
        for (const src of Object.values(style.sources) as Record<string, unknown>[]) {
          if (Array.isArray(src.tiles)) {
            src.tiles = src.tiles.map((t: string) => (t.startsWith('/') ? `${origin}${t}` : t));
          }
        }
      }
      return style;
    },
    enabled: !!mapId, // Only run query when mapId is available
  });

  // Get current basemap from style metadata or default to first available
  const currentBasemap = useMemo(() => {
    if (styleData?.metadata?.current_basemap) {
      return styleData.metadata.current_basemap;
    }
    return availableBasemaps[0] || '';
  }, [styleData, availableBasemaps]);

  // Add basemap control when map and basemaps are available.
  // mapInstanceId ensures re-creation after the map is destroyed/recreated.
  // biome-ignore lint/correctness/useExhaustiveDependencies: mapInstanceId is an intentional trigger-only dep
  useEffect(() => {
    const map = localMapRef.current;
    if (!map || availableBasemaps.length === 0) return;

    // Use current basemap from style or default to first available
    const initialBasemap = currentBasemap || availableBasemaps[0];
    // Create control with a no-op callback initially to avoid dependency issues
    const basemapControl = new BasemapControl(availableBasemaps, initialBasemap, basemapDisplayNames, () => undefined);
    basemapControlRef.current = basemapControl;
    map.addControl(basemapControl, 'top-right');
    // Immediately update with the real callback
    basemapControl.updateCallback(handleBasemapChange);

    return () => {
      basemapControlRef.current = null;
      try {
        map.removeControl(basemapControl);
      } catch (_) {
        /* already removed */
      }
    };
  }, [availableBasemaps, currentBasemap, basemapDisplayNames, handleBasemapChange, mapInstanceId]);

  // Track whether initial zoom has been performed — using a ref avoids
  // re-triggering the setStyle effect when this flag changes.
  const hasZoomedRef = useRef(false);

  // Apply the map style when styleData changes.
  // IMPORTANT: We inject paint overrides (choropleth, color, opacity) into the
  // style JSON *before* calling setStyle() so they survive the MapLibre diff.
  // Previously, overrides set via setPaintProperty were wiped on every setStyle()
  // call, and the styledata replay had a race condition with the diff engine.
  // biome-ignore lint/correctness/useExhaustiveDependencies: paintOverridesRef is a stable ref read inside the effect
  useEffect(() => {
    const map = localMapRef.current;
    if (!map || !styleData) return;

    try {
      // Preserve globe projection across setStyle — setStyle resets to mercator
      // if the style spec has no projection field, losing any globe toggle the user set.
      const currentProjection = map.getProjection();

      // Deep-clone the style so we don't mutate the TanStack Query cache,
      // then inject any active paint overrides (choropleth expressions, colors,
      // opacity) directly into the layer paint properties.
      const style = JSON.parse(JSON.stringify(styleData));
      injectOverridesIntoStyle(style, paintOverridesRef.current);

      // For Sentinel-2 TRUE-COLOR basemap, inject a fast Esri underlay so the
      // user sees imagery instantly while slow satellite tiles load.
      // NDVI is excluded — its green/red output is nothing like satellite imagery.
      const hasSatelliteSource = style.sources && 'sentinel2-live' in style.sources;
      if (hasSatelliteSource && !style.sources['basemap-underlay']) {
        style.sources['basemap-underlay'] = {
          type: 'raster',
          tiles: ['https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}'],
          tileSize: 256,
          maxzoom: 18,
        };
        // Insert underlay layer at position 0 (behind everything)
        const underlayLayer = {
          id: 'basemap-underlay-layer',
          type: 'raster' as const,
          source: 'basemap-underlay',
          layout: { visibility: 'visible' as const },
          paint: {},
        };
        if (style.layers) {
          style.layers.unshift(underlayLayer);
        } else {
          style.layers = [underlayLayer];
        }
        // Fade Sentinel-2 tiles at high zoom so sharp Esri underlay shows through
        // beyond Sentinel-2's native 10m/pixel resolution (maxzoom 14).
        if (style.layers) {
          for (const layer of style.layers) {
            if (layer.type === 'raster' && 'source' in layer && layer.source === 'sentinel2-live') {
              layer.paint = {
                ...layer.paint,
                'raster-opacity': ['interpolate', ['linear'], ['zoom'], 14, 1, 17, 0.25],
              };
            }
          }
        }
      }

      // Inject invisible fill layers for polygon sources that only have line
      // layers. Without a fill layer, queryRenderedFeatures only returns hits
      // on the thin outline pixels, making it nearly impossible to click a
      // feature inside the polygon.
      if (style.layers && style.sources) {
        const sourcesWithFill = new Set<string>();
        const sourcesWithLine = new Map<string, { sourceLayer?: string; idx: number }>();
        for (let i = 0; i < style.layers.length; i++) {
          const l = style.layers[i];
          if (!l.source || typeof l.source !== 'string' || !l.source.startsWith('L')) continue;
          if (l.type === 'fill') sourcesWithFill.add(l.source);
          if (l.type === 'line' && !sourcesWithFill.has(l.source)) {
            sourcesWithLine.set(l.source, { sourceLayer: l['source-layer'], idx: i });
          }
        }
        // For sources that have line but no fill, insert an invisible fill before the line
        for (const [src, info] of sourcesWithLine) {
          if (sourcesWithFill.has(src)) continue;
          const invisibleFill: Record<string, unknown> = {
            id: `${src}-click-fill`,
            type: 'fill',
            source: src,
            paint: { 'fill-color': '#000', 'fill-opacity': 0 },
          };
          if (info.sourceLayer) invisibleFill['source-layer'] = info.sourceLayer;
          style.layers.splice(info.idx, 0, invisibleFill);
        }
      }

      // Update the style using setStyle
      map.setStyle(style);

      // Re-apply non-mercator projection after the style finishes loading
      if (currentProjection?.type && currentProjection.type !== 'mercator') {
        map.once('style.load', () => {
          map.setProjection(currentProjection);
        });
      }

      // If we haven't zoomed yet, zoom to the style's center and zoom level
      if (!hasZoomedRef.current) {
        if (styleData.center && styleData.zoom !== undefined) {
          map.jumpTo({
            center: styleData.center,
            zoom: styleData.zoom,
            pitch: styleData.pitch || 0,
            bearing: styleData.bearing || 0,
          });
        }
        hasZoomedRef.current = true;
      }
    } catch (err) {
      console.error('Error updating style:', err);
      addError('Failed to update map style: ' + (err instanceof Error ? err.message : String(err)), true);
    }
  }, [styleData, addError]); // Only re-run when actual style data changes

  // Load legend symbols separately — depends on mapData but should NOT
  // trigger a full setStyle() call (which wipes paint overrides).
  // biome-ignore lint/correctness/useExhaustiveDependencies: styleData guard ensures map has a style
  useEffect(() => {
    const map = localMapRef.current;
    if (!map || !styleData) return;
    loadLegendSymbols(map);
  }, [loadLegendSymbols, styleData]);

  // Refresh deck.gl overlay when 3D agri layers appear/change
  // biome-ignore lint/correctness/useExhaustiveDependencies: pointCloudLayers handled in map load
  useEffect(() => {
    if (!deckOverlayRef.current || deckgl3dLayers.length === 0) return;
    let cancelled = false;

    (async () => {
      try {
        const overlaidAgriLayers = await Promise.all(
          deckgl3dLayers.map((layer) =>
            createAgriIndicesLayer(layer.id, `/api/layer/${layer.id}.geojson`).catch((err) => {
              console.error(`Error refreshing agri 3D layer for ${layer.id}:`, err);
              return null;
            }),
          ),
        );
        if (cancelled) return;

        // Re-create point cloud layers too so they're not lost
        const overlaidPCLayers = await Promise.all(pointCloudLayers.map((layer) => createPointCloudLayer(layer)));
        if (cancelled) return;

        const allDeckLayers = [...overlaidPCLayers, ...overlaidAgriLayers].filter(Boolean);
        deckOverlayRef.current.setProps({ layers: allDeckLayers });
      } catch (err) {
        console.error('Error refreshing deck.gl 3D layers:', err);
      }
    })();

    return () => {
      cancelled = true;
    };
  }, [deckgl3dLayers]);

  useEffect(() => {
    const map = localMapRef.current;
    if (!map) return;

    const applyVisibility = () => {
      if (!map.isStyleLoaded()) return;
      const style = map.getStyle();
      if (!style?.layers) return;

      style.layers.forEach((layer) => {
        if ('source' in layer && layer.source) {
          const src = layer.source as string;
          // Source IDs may be prefixed (e.g. "worldcover-source-{id}",
          // "raster-source-{id}", "cog-source-{id}") so check both
          // exact match and whether the source contains a hidden layer ID.
          const isHidden = hiddenLayerIDs.some((id) => src === id || src.endsWith(`-${id}`));
          const visibility = isHidden ? 'none' : 'visible';
          try {
            map.setLayoutProperty(layer.id, 'visibility', visibility);
          } catch {
            // layer may have been removed between getStyle() and setLayoutProperty()
          }
        }
      });
    };

    // Apply immediately if style is loaded, otherwise wait for it
    if (map.isStyleLoaded()) {
      applyVisibility();
    } else {
      map.once('style.load', applyVisibility);
    }

    // Re-apply after a full style reload (setStyle call from LLM).
    // Using 'style.load' instead of 'styledata' is critical: 'styledata' fires
    // on every setLayoutProperty call, which would immediately override any
    // visibility change. 'style.load' only fires on full style reloads.
    map.on('style.load', applyVisibility);

    return () => {
      map.off('style.load', applyVisibility);
    };
  }, [hiddenLayerIDs]);

  // Update the points source when pointer positions change
  useEffect(() => {
    const map = localMapRef.current;
    if (map && map.isStyleLoaded()) {
      const source = map.getSource('pointer-positions');
      if (source) {
        (source as maplibregl.GeoJSONSource).setData(pointsGeoJSON);
      }
    }
  }, [pointsGeoJSON]);

  const [inputValue, setInputValue] = useState('');
  const readyStateRef = useRef<number>(readyState);

  useEffect(() => {
    readyStateRef.current = readyState;
  }, [readyState]);

  // Function to send a message
  const sendMessage = async (text: string) => {
    if (!text.trim()) return;

    setInputValue(''); // Clear input after preparing to send

    const userMessage: ChatCompletionUserMessageParam = {
      role: 'user',
      content: text,
    };

    // Create and add ephemeral action
    const actionId = `send-message-${Date.now()}`;
    const sendingAction: EphemeralAction = {
      map_id: mapId,
      ephemeral: true,
      action_id: actionId,
      action: 'Sending message to Sage...',
      timestamp: new Date().toISOString(),
      completed_at: null,
      layer_id: null,
      status: 'active',
      updates: {
        style_json: false,
      },
    };

    try {
      let conversationIdToUse: number | null = conversationId;

      // If no conversation, create one first
      if (conversationIdToUse === null) {
        // Creating conversation also an ephemeral action
        const createConversationAction: EphemeralAction = {
          map_id: mapId,
          ephemeral: true,
          action_id: `create-conversation-${Date.now()}`,
          action: 'Creating new conversation...',
          timestamp: new Date().toISOString(),
          completed_at: null,
          layer_id: null,
          status: 'active',
          updates: {
            style_json: false,
          },
        };
        setActiveActions((prev) => [...prev, createConversationAction]);

        const createResp = await apiFetch(`/api/conversations`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ project_id: project.id }),
        });
        if (!createResp.ok) {
          const err = await createResp.json().catch(() => ({ detail: createResp.statusText }));
          const d = err.detail;
          throw new Error(typeof d === 'string' ? d : d ? JSON.stringify(d) : createResp.statusText);
        }
        const newConv = (await createResp.json()) as Conversation;
        conversationIdToUse = newConv.id;
        setConversationId(conversationIdToUse);

        // Wait briefly for websocket to connect to the new conversation
        const maxWaitMs = 10000;
        const start = Date.now();
        while (Date.now() - start < maxWaitMs && readyStateRef.current !== ReadyState.OPEN) {
          await new Promise((r) => setTimeout(r, 100));
        }
        setActiveActions((prev) => prev.filter((a) => a.action_id !== createConversationAction.action_id));
      }

      setActiveActions((prev) => [...prev, sendingAction]);

      const sendBody: MessageSendRequest = {
        message: userMessage,
        selected_feature: null,
      };
      if (selectedFeature) {
        sendBody.selected_feature = {
          layer_id: selectedFeature.source,
          attributes: selectedFeature.properties,
        };
      }

      const response = await apiFetch(`/api/maps/conversations/${conversationIdToUse}/maps/${mapId}/send`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
        },
        body: JSON.stringify(sendBody),
      });

      if (response.ok) {
        await response.json();
        invalidateProjectData();
      } else if (response.status === 401) {
        // Session expired during chat send. Give a clear, actionable message.
        addError('Your session has expired. Please refresh the page to continue chatting.', true);
        return;
      } else {
        const errorData = await response.json().catch(() => ({ detail: response.statusText }));
        const d = errorData.detail;
        throw new Error(typeof d === 'string' ? d : d ? JSON.stringify(d) : response.statusText);
      }
    } catch (error) {
      const msg = error instanceof Error ? error.message : 'Network error';
      // Translate cryptic "Token expired" into actionable message
      if (msg.toLowerCase().includes('token expired') || msg.toLowerCase().includes('unauthorized')) {
        addError('Your session has expired. Please refresh the page to continue chatting.', true);
      } else {
        addError(msg, true);
      }
    } finally {
      // Remove the ephemeral action when done
      setActiveActions((prev) => prev.filter((a) => a.action_id !== actionId));
    }
  };

  // Handle input submission
  const handleKeyDown = (e: React.KeyboardEvent<HTMLInputElement>) => {
    if (e.key === 'Enter' && inputValue.trim()) {
      sendMessage(inputValue);
      setInputValue('');
    }
  };

  // Update basemap control when basemap changes
  useEffect(() => {
    if (basemapControlRef.current && currentBasemap) {
      basemapControlRef.current.updateBasemap(currentBasemap);
    }
  }, [currentBasemap]);

  // Update basemap control callback when handleBasemapChange changes
  useEffect(() => {
    if (basemapControlRef.current) {
      basemapControlRef.current.updateCallback(handleBasemapChange);
    }
  }, [handleBasemapChange]);

  // Update Sentinel Hub tile source URLs when mosaic mode changes
  useEffect(() => {
    const map = localMapRef.current;
    if (!map || !isSentinel2Active) return;

    const style = map.getStyle();
    if (!style?.sources) return;

    for (const [sourceId, sourceDef] of Object.entries(style.sources)) {
      if (sourceDef.type !== 'raster' || !('tiles' in sourceDef)) continue;
      const tiles = (sourceDef as any).tiles as string[] | undefined;
      if (!tiles?.some((t: string) => t.includes('/api/satellite/'))) continue;

      // Replace or add mosaic param in the tile URL (avoid new URL() which encodes {z}/{x}/{y} templates)
      const newTiles = tiles.map((url: string) => {
        const hasQuery = url.includes('?');
        const base = hasQuery ? url.replace(/([&?])mosaic=[^&]*/g, '') : url;
        const sep = base.includes('?') ? '&' : '?';
        return `${base}${sep}mosaic=${mosaicMode}`;
      });

      // Use internal method to update tiles and force reload
      const src = map.getSource(sourceId);
      if (src && 'setTiles' in src) {
        (src as any).setTiles(newTiles);
      }
    }
  }, [mosaicMode, isSentinel2Active]);

  // Detect sentinel2_live basemap from initial style load
  useEffect(() => {
    setIsSentinel2Active(currentBasemap === 'sentinel2_live' || currentBasemap === 'ndvi_map');
    if (currentBasemap !== 'sentinel2_live') setSceneInfo(null);
  }, [currentBasemap]);

  // Fetch scene info when Sentinel-2 Live basemap is active
  useEffect(() => {
    const map = localMapRef.current;
    if (!map || !isSentinel2Active) return;

    let cancelled = false;
    let debounceTimer: ReturnType<typeof setTimeout>;

    const fetchSceneInfo = () => {
      const bounds = map.getBounds();
      if (!bounds) return;

      const params = new URLSearchParams({
        west: bounds.getWest().toFixed(4),
        south: bounds.getSouth().toFixed(4),
        east: bounds.getEast().toFixed(4),
        north: bounds.getNorth().toFixed(4),
        collection: 'sentinel-2-l2a',
        mosaic: mosaicMode,
      });

      apiFetch(`/api/satellite/scene-info?${params}`)
        .then((r) => r.json())
        .then((data) => {
          if (!cancelled) setSceneInfo(data);
        })
        .catch(() => {
          /* scene info is best-effort */
        });
    };

    const onMoveEnd = () => {
      clearTimeout(debounceTimer);
      debounceTimer = setTimeout(fetchSceneInfo, 500);
    };

    // Fetch immediately + on map move
    fetchSceneInfo();
    map.on('moveend', onMoveEnd);

    return () => {
      cancelled = true;
      clearTimeout(debounceTimer);
      map.off('moveend', onMoveEnd);
    };
  }, [isSentinel2Active, mapInstanceId, mosaicMode]);

  // Effect to log when attribute table is opened/closed
  useEffect(() => {
    if (showAttributeTable && selectedLayer) {
      // Debug: Opening attributes for layer
    }
  }, [showAttributeTable, selectedLayer]);

  // Find the last message in the conversation history
  const lastMsg: SanitizedMessage | undefined = mapTree?.tree
    .find((node) => node.map_id === mapId)
    ?.messages.sort((a, b) => {
      if (a.created_at && b.created_at) {
        return new Date(b.created_at).getTime() - new Date(a.created_at).getTime();
      }
      return 0;
    })[0];

  // Determine the last assistant message to display. Only show if it's the very
  // last message in the conversation and has text content.
  const lastAssistantMsg: string | undefined = lastMsg && lastMsg.role === 'assistant' ? lastMsg.content : undefined;

  // Determine the last user message for the input placeholder.
  const lastUserMsg: string | undefined = lastMsg && lastMsg.role === 'user' ? lastMsg.content : undefined;

  // especially chat disconnected errors happen all the time and shouldn't
  // override the text box
  const criticalErrors = errors.filter((e) => e.shouldOverrideMessages);

  return (
    <>
      <div className={`relative map-container ${className} grow max-h-screen`} style={{ width, height }}>
        <div ref={mapContainerRef} style={{ width: '100%', height: '100%', minHeight: '100vh' }} className="bg-slate-950" />

        {/* Sentinel-2 scene info badge with mosaic toggle */}
        {isSentinel2Active && sceneInfo?.scene_date && (
          <div className="absolute bottom-8 left-28 z-10 bg-black/70 text-white text-xs px-3 py-1.5 rounded-md backdrop-blur-sm flex items-center gap-2">
            <span className="font-semibold">
              Captured: {new Date(sceneInfo.scene_date).toLocaleDateString('en-GB', { day: 'numeric', month: 'short', year: 'numeric' })}
            </span>
            {sceneInfo.cloud_cover != null && <span className="text-white/70">| Cloud: {Math.round(sceneInfo.cloud_cover)}%</span>}
            <span className="text-white/50">
              | {sceneInfo.scenes_available} scene{sceneInfo.scenes_available !== 1 ? 's' : ''} in range
            </span>
            <span className="text-white/30">|</span>
            <button
              type="button"
              className={`px-1.5 py-0.5 rounded text-[10px] font-medium transition-colors ${
                mosaicMode === 'leastCC' ? 'bg-emerald-500/80 text-white' : 'bg-white/10 text-white/60 hover:bg-white/20'
              }`}
              onClick={() => setMosaicMode('leastCC')}
              title="Show clearest (least cloudy) scene"
            >
              Clearest
            </button>
            <button
              type="button"
              className={`px-1.5 py-0.5 rounded text-[10px] font-medium transition-colors ${
                mosaicMode === 'mostRecent' ? 'bg-blue-500/80 text-white' : 'bg-white/10 text-white/60 hover:bg-white/20'
              }`}
              onClick={() => setMosaicMode('mostRecent')}
              title="Show most recent scene"
            >
              Most Recent
            </button>
          </div>
        )}

        {/* Render the attribute table if showAttributeTable is true */}
        {selectedLayer && (
          <div className="absolute top-1/2 left-1/2 transform -translate-x-1/2 -translate-y-1/2 z-50 w-4/5 max-w-4xl">
            <AttributeTable layer={selectedLayer} isOpen={showAttributeTable} onClose={() => setShowAttributeTable(false)} />
          </div>
        )}

        {mapData && openDropzone && (
          <LayerList
            project={project}
            currentMapData={mapData}
            mapRef={mapRef}
            openDropzone={openDropzone}
            isInConversation={conversationId !== null}
            readyState={readyState}
            activeActions={activeActions}
            setShowAttributeTable={setShowAttributeTable}
            setSelectedLayer={setSelectedLayer}
            updateMapData={invalidateMapData}
            layerSymbols={layerSymbols}
            zoomHistory={zoomHistory}
            zoomHistoryIndex={zoomHistoryIndex}
            setZoomHistoryIndex={setZoomHistoryIndex}
            uploadingFiles={uploadingFiles}
            demoConfig={demoConfig}
            hiddenLayerIDs={hiddenLayerIDs}
            toggleLayerVisibility={toggleLayerVisibility}
            errors={errors}
            loadingLayerIDs={loadingLayerIDs}
            paintOverrides={paintOverrides}
            onLayerOpacityChange={setLayerOpacity}
            onLayerColorChange={setLayerColor}
          />
        )}
        {/* Pie chart overlays for single-feature buffer layers */}
        {mapRef.current &&
          Array.from(pieOverlays.entries()).map(([layerId, data]) => (
            <BufferPieOverlay
              key={layerId}
              map={mapRef.current!}
              center={data.center}
              slices={data.slices}
              onRemove={() => {
                setPieOverlays((prev) => {
                  const next = new Map(prev);
                  next.delete(layerId);
                  return next;
                });
              }}
            />
          ))}
        {selectedFeature && (
          <Card className="absolute bottom-10 left-4 max-h-[60vh] overflow-auto py-2 rounded-sm border-0 gap-2 max-w-72 w-full">
            <CardHeader className="px-2">
              <CardTitle className="text-base flex justify-between items-center gap-2">
                <div className="flex gap-2 items-baseline">
                  {mapData?.layers.find((l) => l.id === selectedFeature.source) ? (
                    <>
                      <span>{mapData?.layers.find((l) => l.id === selectedFeature.source)?.name}</span>
                      <span className="text-xs text-gray-500 dark:text-gray-400">
                        {mapData?.layers.find((l) => l.id === selectedFeature.source)?.type}
                      </span>
                    </>
                  ) : (
                    <span>Selected feature</span>
                  )}
                </div>
                <div className="flex gap-2">
                  <button
                    onClick={() => {
                      if (selectedFeature && selectedFeature.geometry && mapRef.current) {
                        const map = mapRef.current;
                        const feature_bbox = bbox(selectedFeature.geometry);
                        map.fitBounds(
                          [
                            [feature_bbox[0], feature_bbox[1]],
                            [feature_bbox[2], feature_bbox[3]],
                          ],
                          {
                            padding: 50,
                            duration: 1000,
                          },
                        );
                      }
                    }}
                    className="text-gray-500 hover:text-gray-700 dark:text-gray-400 dark:hover:text-gray-200"
                    title="Zoom to feature"
                  >
                    <ZoomIn className="h-4 w-4 cursor-pointer" />
                  </button>
                  <button
                    onClick={() => selectFeature(null)}
                    className="text-gray-500 hover:text-gray-700 dark:text-gray-400 dark:hover:text-gray-200"
                    title="Deselect feature"
                  >
                    <X className="h-4 w-4 cursor-pointer" />
                  </button>
                </div>
              </CardTitle>
            </CardHeader>
            <CardContent className="px-2 max-h-[50vh] overflow-auto">
              <div className="overflow-x-auto">
                <table className="w-full text-xs">
                  <thead>
                    <tr className="border-b">
                      <th className="text-left py-1 pr-2 font-medium">Attribute</th>
                      <th className="text-left py-1 font-medium">Value</th>
                    </tr>
                  </thead>
                  <tbody>
                    {selectedFeature.properties &&
                      Object.entries(selectedFeature.properties)
                        .filter(([key]) => {
                          // Hide auto-enriched metric columns added by the
                          // (now removed) enrichment API. These are computed
                          // values, not original layer attributes.
                          const enrichedPrefixes = [
                            'soil_', 'ndvi_', 'evi_', 'ndwi_', 'savi_', 'ndre_', 'ndbi_',
                            'temp_', 'rainfall_', 'wind_', 'ch4_', 'n2o_', 'co2_',
                            'cropland_', 'forest_', 'built_', 'rangeland_',
                          ];
                          return !enrichedPrefixes.some((p) => key.startsWith(p));
                        })
                        .map(([key, value]) => (
                        <tr key={key} className="border-b border-gray-100 dark:border-gray-700" title={`Type: ${typeof value}`}>
                          <td className="py-1 pr-2 font-mono text-gray-600 dark:text-gray-400 break-all">{key}</td>
                          <td className="py-1 font-mono break-all">{String(value)}</td>
                        </tr>
                      ))}
                  </tbody>
                </table>
              </div>
            </CardContent>
          </Card>
        )}
        {/* Message display component - always show parent div, animate height */}
        {(criticalErrors.length > 0 || activeActions.length > 0 || lastAssistantMsg) && (
          <div
            className={`z-30 absolute bottom-12 mb-[34px] left-3/5 transform -translate-x-1/2 w-4/5 max-w-lg ${assistantExpanded ? 'max-h-[80vh]' : 'max-h-40'} overflow-auto rounded-t-md shadow-md p-2 text-sm transition-all duration-300 h-auto ${errors.length > 0 ? 'border-red-800' : ''}`}
            style={{ backgroundColor: 'rgba(30, 41, 57, 0.9)' }}
          >
            {/* Expand/contract toggle */}
            {lastAssistantMsg && (
              <button
                onClick={() => setAssistantExpanded((v) => !v)}
                className="absolute right-2 top-2 text-gray-400 hover:text-gray-200 cursor-pointer"
                title={assistantExpanded ? 'Contract' : 'Expand'}
              >
                {assistantExpanded ? <Minimize2 className="h-4 w-4" /> : <Maximize2 className="h-4 w-4" />}
              </button>
            )}
            {criticalErrors.length > 0 ? (
              <div className="space-y-1 max-h-20">
                {criticalErrors.map((error) => (
                  <div key={error.id} className="flex items-center justify-between">
                    <div className="flex flex-col flex-1 mr-2">
                      <span className="text-red-400">{error.message}</span>
                      <span className="text-xs text-slate-500 dark:text-gray-400">{error.timestamp.toLocaleTimeString()}</span>
                    </div>
                    <button
                      onClick={() => dismissError(error.id)}
                      className="text-white cursor-pointer hover:underline shrink-0"
                      title="Dismiss error"
                    >
                      Dismiss
                    </button>
                  </div>
                ))}
              </div>
            ) : activeActions.length > 0 ? (
              <div className="flex items-center justify-between">
                <ol className="space-y-1">
                  {activeActions.map((action, actionIndex) => (
                    <li key={`${action.action_id}-${actionIndex}`} className="flex items-center">
                      {getActionIcon(action.action)}
                      <span>{action.action}</span>
                    </li>
                  ))}
                </ol>
                {isCancelling ? (
                  <span className="text-white ml-2 shrink-0">Cancelling...</span>
                ) : (
                  <button className="text-white cursor-pointer ml-2 shrink-0 hover:underline" onClick={() => setIsCancelling(true)}>
                    Cancel
                  </button>
                )}
              </div>
            ) : lastAssistantMsg ? (
              <div className={KUE_MESSAGE_STYLE}>
                <ReactMarkdown remarkPlugins={[remarkGfm]}>{lastAssistantMsg}</ReactMarkdown>
              </div>
            ) : null}
          </div>
        )}
        <div
          className={`z-30 absolute bottom-12 left-3/5 transform -translate-x-1/2 w-4/5 max-w-xl bg-white dark:bg-gray-800 shadow-md focus-within:ring-2 focus-within:ring-white/30 flex items-center border border-input bg-input rounded-md`}
        >
          <Input
            className={`flex-1 border-none shadow-none !bg-transparent focus:!ring-0 focus:!ring-offset-0 focus-visible:!ring-0 focus-visible:!ring-offset-0 focus-visible:!outline-none`}
            placeholder={lastUserMsg || 'Type in for Sage to do something...'}
            value={inputValue}
            onChange={(e) => setInputValue(e.target.value)}
            onKeyDown={handleKeyDown}
          />
          {selectedFeature && (
            <Tooltip>
              <TooltipTrigger asChild>
                <span onClick={() => selectFeature(null)} className={`px-2 hover:cursor-pointer text-gray-400 hover:text-gray-200`}>
                  <MousePointerClick className="h-6 w-6 inline-block" />
                </span>
              </TooltipTrigger>
              <TooltipContent>
                <p>Sage can see your selected feature</p>
              </TooltipContent>
            </Tooltip>
          )}
        </div>
      </div>

      <VersionVisualization
        mapTree={mapTree}
        conversationId={conversationId}
        currentMapId={mapId}
        conversations={conversations}
        conversationsEnabled={conversationsEnabled}
        setConversationId={setConversationId}
        activeActions={activeActions}
      />
    </>
  );
}
