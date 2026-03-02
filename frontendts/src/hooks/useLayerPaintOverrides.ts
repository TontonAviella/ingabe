/**
 * useLayerPaintOverrides — manages user paint/layout property overrides
 * that survive MapLibre setStyle() resets triggered by LLM tool calls.
 *
 * Pattern proven by anymap-ts MapLibreRenderer.ts:549-580:
 *   map.setPaintProperty(layerId, opacityProp, value)  ← direct, no setStyle()
 *   Replayed via map.on('styledata', replay)           ← same as visibility toggle
 */

import { apiFetch } from '@mundi/ee';
import type { Map as MLMap } from 'maplibre-gl';
import { useCallback, useEffect, useRef, useState } from 'react';

// Map from MapLibre layer type → opacity paint property
// Direct port from anymap-ts MapLibreRenderer.ts:569-579
const OPACITY_PROP: Record<string, string> = {
  fill: 'fill-opacity',
  line: 'line-opacity',
  circle: 'circle-opacity',
  symbol: 'icon-opacity',
  raster: 'raster-opacity',
  'fill-extrusion': 'fill-extrusion-opacity',
  heatmap: 'heatmap-opacity',
  background: 'background-opacity',
};

export interface LayerPaintOverride {
  opacity?: number;
  /** Solid-color override. Cleared when choroplethExpression is set. */
  color?: string;
  /**
   * MapLibre step expression for choropleth fill-color.
   * Takes priority over `color` for fill layers.
   * Cleared when a solid color is applied.
   */
  choroplethExpression?: unknown[];
  /** Column used for the choropleth (informational). */
  choroplethColumn?: string;
}

export type PaintOverrides = Record<string, LayerPaintOverride>;

interface UseLayerPaintOverridesOptions {
  map: MLMap | null;
  mapId: string;
  isMapReady: boolean;
}

export function useLayerPaintOverrides({ map, mapId, isMapReady }: UseLayerPaintOverridesOptions) {
  const [overrides, setOverrides] = useState<PaintOverrides>({});
  // Keep a ref so the styledata replay closure always has current overrides
  const overridesRef = useRef<PaintOverrides>({});

  // Sync ref whenever state updates
  useEffect(() => {
    overridesRef.current = overrides;
  }, [overrides]);

  /**
   * Apply all current overrides to the map.
   * Called immediately on change AND replayed after every setStyle() via 'styledata'.
   */
  const applyOverrides = useCallback((currentMap: MLMap, currentOverrides: PaintOverrides) => {
    for (const [layerId, override] of Object.entries(currentOverrides)) {
      // Find all MapLibre style layers that belong to this data layer
      // Sources can be named exactly layerId OR prefixed (e.g. cog-source-{layerId})
      const style = currentMap.getStyle();
      if (!style?.layers) continue;

      const matchingLayers = style.layers.filter((sl) => {
        if (!('source' in sl)) return false;
        const src = sl.source as string;
        return src === layerId || src.endsWith(`-${layerId}`);
      });

      for (const styleLayer of matchingLayers) {
        // Apply opacity
        if (override.opacity !== undefined) {
          const prop = OPACITY_PROP[styleLayer.type];
          if (prop) {
            try {
              currentMap.setPaintProperty(styleLayer.id, prop, override.opacity);
            } catch {
              // Layer may have been removed mid-replay
            }
          }
        }

        // Apply color / choropleth expression
        // choroplethExpression takes priority over solid color for fill layers.
        if (styleLayer.type === 'fill' && override.choroplethExpression !== undefined) {
          try {
            currentMap.setPaintProperty(styleLayer.id, 'fill-color', override.choroplethExpression);
          } catch {
            // Layer may have been removed
          }
        } else if (override.color !== undefined) {
          const colorPropMap: Record<string, string> = {
            fill: 'fill-color',
            line: 'line-color',
            circle: 'circle-color',
          };
          const colorProp = colorPropMap[styleLayer.type];
          if (colorProp) {
            try {
              currentMap.setPaintProperty(styleLayer.id, colorProp, override.color);
            } catch {
              // Layer may have been removed
            }
          }
        }
      }
    }
  }, []);

  // Register 'styledata' replay — fires after every map.setStyle() call
  // This is the same pattern as visibility toggle at MapLibreMap.tsx:1070-1112
  useEffect(() => {
    if (!map || !isMapReady) return;

    const onStyleData = () => {
      applyOverrides(map, overridesRef.current);
    };

    map.on('styledata', onStyleData);
    return () => {
      map.off('styledata', onStyleData);
    };
  }, [map, isMapReady, applyOverrides]);

  /**
   * Set opacity for a layer. Immediately applies and registers for replay.
   */
  const setLayerOpacity = useCallback(
    (layerId: string, opacity: number) => {
      if (!map || !isMapReady) return;

      const next: PaintOverrides = {
        ...overridesRef.current,
        [layerId]: { ...overridesRef.current[layerId], opacity },
      };
      overridesRef.current = next;
      setOverrides(next);

      // Apply immediately — don't wait for next styledata event
      applyOverrides(map, next);

      // Persist to DB (fire-and-forget, non-blocking)
      apiFetch(`/api/maps/${mapId}/layer/${layerId}/overrides`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ opacity }),
      }).catch(() => {
        // Non-critical — overrides are in-memory anyway
      });
    },
    [map, mapId, isMapReady, applyOverrides],
  );

  /**
   * Set fill/line color for a vector layer.
   * Clears any active choropleth expression since solid color takes over.
   */
  const setLayerColor = useCallback(
    (layerId: string, color: string) => {
      if (!map || !isMapReady) return;

      const { choroplethExpression: _ce, choroplethColumn: _cc, ...rest } = overridesRef.current[layerId] ?? {};
      const next: PaintOverrides = {
        ...overridesRef.current,
        [layerId]: { ...rest, color },
      };
      overridesRef.current = next;
      setOverrides(next);

      applyOverrides(map, next);

      apiFetch(`/api/maps/${mapId}/layer/${layerId}/overrides`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ color }),
      }).catch(() => {
        // Non-critical — overrides are in-memory anyway
      });
    },
    [map, mapId, isMapReady, applyOverrides],
  );

  /**
   * Apply a choropleth step expression to the fill-color of a layer.
   * Clears any solid color override since choropleth takes priority.
   */
  const setLayerChoropleth = useCallback(
    (layerId: string, column: string, expression: unknown[]) => {
      if (!map || !isMapReady) return;

      const { color: _c, ...rest } = overridesRef.current[layerId] ?? {};
      const next: PaintOverrides = {
        ...overridesRef.current,
        [layerId]: {
          ...rest,
          choroplethExpression: expression,
          choroplethColumn: column,
        },
      };
      overridesRef.current = next;
      setOverrides(next);

      applyOverrides(map, next);

      // Persist to DB (fire-and-forget, non-blocking)
      apiFetch(`/api/maps/${mapId}/layer/${layerId}/overrides`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ choroplethExpression: expression, choroplethColumn: column }),
      }).catch(() => {
        // Non-critical — overrides are in-memory anyway
      });
    },
    [map, mapId, isMapReady, applyOverrides],
  );

  /**
   * Load persisted overrides from DB on mount and apply them.
   */
  const loadOverrides = useCallback(
    async (layerIds: string[]) => {
      if (!map || !isMapReady || layerIds.length === 0) return;

      try {
        const res = await apiFetch(`/api/maps/${mapId}/layer-overrides`);
        if (!res.ok) return;
        const data = (await res.json()) as PaintOverrides;
        // Filter to only layers currently on this map
        const filtered: PaintOverrides = {};
        for (const id of layerIds) {
          if (data[id]) filtered[id] = data[id];
        }
        overridesRef.current = filtered;
        setOverrides(filtered);
        applyOverrides(map, filtered);
      } catch {
        // Non-critical
      }
    },
    [map, mapId, isMapReady, applyOverrides],
  );

  return { overrides, setLayerOpacity, setLayerColor, setLayerChoropleth, loadOverrides };
}
