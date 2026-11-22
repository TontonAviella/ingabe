/** The box around a line's corners ([longitude, latitude] each), as MapLibre's fitBounds takes it:
 * [[west, south], [east, north]]. null for no corners. */
export function boundsOf(corners: readonly (readonly number[])[]): [[number, number], [number, number]] | null {
  if (!corners.length) return null;
  const lons = corners.map((c) => c[0]);
  const lats = corners.map((c) => c[1]);
  return [
    [Math.min(...lons), Math.min(...lats)],
    [Math.max(...lons), Math.max(...lats)],
  ];
}
