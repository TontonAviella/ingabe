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

/**
 * Inject paint overrides directly into a MapLibre style JSON object.
 * This ensures overrides survive `setStyle()` calls by being part of the
 * style itself, rather than relying on post-`setStyle()` `setPaintProperty` calls.
 *
 * The input style is mutated in-place — callers should deep-clone first.
 */
export function injectOverridesIntoStyle(style: Record<string, unknown>, currentOverrides: PaintOverrides): void {
  const layers = style.layers as Array<Record<string, unknown>> | undefined;
  if (!layers) return;

  for (const [layerId, override] of Object.entries(currentOverrides)) {
    for (const sl of layers) {
      if (!sl.source) continue;
      const src = sl.source as string;
      if (src !== layerId && !src.endsWith(`-${layerId}`)) continue;

      const paint = ((sl.paint as Record<string, unknown>) ??= {});
      const layerType = sl.type as string;

      // Opacity
      if (override.opacity !== undefined) {
        const prop = OPACITY_PROP[layerType];
        if (prop) paint[prop] = override.opacity;
      }

      // Choropleth expression (fill layers only, takes priority over solid color)
      if (layerType === 'fill' && override.choroplethExpression !== undefined) {
        paint['fill-color'] = override.choroplethExpression;
      } else if (override.color !== undefined) {
        const colorPropMap: Record<string, string> = {
          fill: 'fill-color',
          line: 'line-color',
          circle: 'circle-color',
        };
        const cp = colorPropMap[layerType];
        if (cp) paint[cp] = override.color;
      }
    }
  }
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

      if (matchingLayers.length === 0 && (override.choroplethExpression || override.color || override.opacity !== undefined)) {
        console.warn('[paintOverrides] No matching layers for source', layerId, '— available sources:', [
          ...new Set(style.layers.filter((sl) => 'source' in sl).map((sl) => (sl as any).source)),
        ]);
      }

      for (const styleLayer of matchingLayers) {
        // Apply opacity
        if (override.opacity !== undefined) {
          const prop = OPACITY_PROP[styleLayer.type];
          if (prop) {
            try {
              currentMap.setPaintProperty(styleLayer.id, prop, override.opacity);
            } catch (e) {
              console.warn('[paintOverrides] setPaintProperty opacity failed:', styleLayer.id, e);
            }
          }
        }

        // Apply color / choropleth expression
        // choroplethExpression takes priority over solid color for fill layers.
        if (styleLayer.type === 'fill' && override.choroplethExpression !== undefined) {
          try {
            currentMap.setPaintProperty(styleLayer.id, 'fill-color', override.choroplethExpression);
          } catch (e) {
            console.warn('[paintOverrides] setPaintProperty choropleth failed:', styleLayer.id, e);
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
            } catch (e) {
              console.warn('[paintOverrides] setPaintProperty color failed:', styleLayer.id, e);
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
      // Boost fill-opacity so choropleth colors are clearly visible on the map.
      // Default LLM styles use 0.3 opacity which makes classification colors
      // nearly invisible on satellite basemaps.
      const opacity = rest.opacity !== undefined && rest.opacity >= 0.7 ? rest.opacity : 0.8;
      const next: PaintOverrides = {
        ...overridesRef.current,
        [layerId]: {
          ...rest,
          opacity,
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
        body: JSON.stringify({ choroplethExpression: expression, choroplethColumn: column, opacity }),
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

  return { overrides, overridesRef, setLayerOpacity, setLayerColor, setLayerChoropleth, loadOverrides };
}
