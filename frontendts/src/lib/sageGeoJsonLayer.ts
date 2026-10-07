import type { ExpressionSpecification, Map as MLMap } from 'maplibre-gl';
import type { GeoJsonLayerUpdate } from './types';

/** The colour of each feature: one colour, or a step over `color_property` with the style's stops. */
function colourOf(style: NonNullable<GeoJsonLayerUpdate['style']>): string | ExpressionSpecification {
  const stops = style.stops || [];
  if (!style.color_property || stops.length === 0) return stops[0]?.color || '#888';
  const expr: unknown[] = ['step', ['get', style.color_property], stops[0].color];
  for (let i = 0; i < stops.length - 1; i++) expr.push(stops[i].max, stops[i + 1].color);
  return expr as ExpressionSpecification;
}

/**
 * Draw a layer Sage sent as inline GeoJSON (display_geojson_layer, counted plants, object masks), the same way
 * when it first arrives and when it is replayed after the map's style is reloaded. Points are dots that grow
 * with the zoom; polygons are a fill (or a 3D extrusion) with an outline.
 */
export function addSageGeoJsonLayer(map: MLMap, gl: GeoJsonLayerUpdate, data: unknown) {
  map.addSource(gl.source_id, { type: 'geojson', data: data as GeoJSON.GeoJSON });
  const style = gl.style || {};
  const colour = colourOf(style);
  const strokeColour = style.stroke_color || '#1a1a1a';
  const metadata = style.legend ? { 'mundi:legend': style.legend } : undefined;
  if (style.geometry === 'point') {
    map.addLayer({
      id: `${gl.source_id}-points`,
      type: 'circle',
      source: gl.source_id,
      metadata,
      paint: {
        'circle-color': colour,
        'circle-opacity': 0.95,
        // A plant is about half a metre across: a dot that stays visible far out and sits on the plant close in.
        'circle-radius': ['interpolate', ['exponential', 2], ['zoom'], 15, 1.2, 19, 4, 22, 22],
        'circle-stroke-color': strokeColour,
        'circle-stroke-width': ['interpolate', ['linear'], ['zoom'], 16, 0, 19, 1, 22, 2],
      },
    });
    return;
  }
  const fillOpacity = typeof style.fill_opacity === 'number' ? style.fill_opacity : 0.55;
  const extrusionProperty = typeof style.extrusion_property === 'string' ? style.extrusion_property : style.color_property;
  if (style.extrude_3d === true && extrusionProperty) {
    const scale = typeof style.extrusion_scale === 'number' ? style.extrusion_scale : 40;
    map.addLayer({
      id: `${gl.source_id}-extrusion`,
      type: 'fill-extrusion',
      source: gl.source_id,
      metadata,
      paint: {
        'fill-extrusion-color': colour,
        'fill-extrusion-opacity': Math.min(fillOpacity + 0.08, 0.9),
        'fill-extrusion-base': 0,
        'fill-extrusion-height': ['*', ['coalesce', ['to-number', ['get', extrusionProperty]], 0], scale],
      },
    });
  } else {
    map.addLayer({
      id: `${gl.source_id}-fill`,
      type: 'fill',
      source: gl.source_id,
      metadata,
      paint: { 'fill-color': colour, 'fill-opacity': fillOpacity },
    });
  }
  map.addLayer({
    id: `${gl.source_id}-stroke`,
    type: 'line',
    source: gl.source_id,
    paint: { 'line-color': strokeColour, 'line-width': typeof style.stroke_width === 'number' ? style.stroke_width : 2 },
  });
}
