import { type Map as MLMap, ScaleControl } from 'maplibre-gl';

/**
 * The map's scale bar, bottom-left, measured again each time the map settles.
 *
 * MapLibre measures the scale only when the map moves. With 3D hills on, the first measure can come before the hills
 * are drawn: both ends of the bar then read the same stale point of the terrain's coordinate picture, 0 m, which
 * MapLibre rounds to "NaN m". When the map does not move again (it opens on a photo and stays), that stuck. On
 * "idle" the hills are drawn, so the measure is right (seen on the Cyampirita map, 2026-11-02).
 */
export function addScale(map: MLMap): ScaleControl {
  const scale = new ScaleControl();
  map.addControl(scale, 'bottom-left');
  map.on('idle', () => {
    if (map.hasControl(scale)) scale.setUnit('metric'); // re-measures; 'metric' is the default unit
  });
  return scale;
}
