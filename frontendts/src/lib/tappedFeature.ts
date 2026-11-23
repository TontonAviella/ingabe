/**
 * The features under a tap that the map is drawing at this zoom, topmost first.
 *
 * queryRenderedFeatures also returns features of layers whose zoom range the map is outside of: a tile holds every
 * layer that starts by its own whole zoom, so a layer starting at 16.5 is in the zoom-16 tile and answers a tap at
 * 16.2, where it is not drawn. Hexagon layers hold one hexagon size per zoom range; without this a tap could open
 * a hexagon of a size that is not on the screen.
 */
export function drawnAtZoom<T extends { layer: { minzoom?: number; maxzoom?: number } }>(features: T[], zoom: number): T[] {
  return features.filter((f) => (f.layer.minzoom ?? 0) <= zoom && zoom < (f.layer.maxzoom ?? Number.POSITIVE_INFINITY));
}
