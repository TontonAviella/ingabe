// Which drawn layers belong to a layer the person has hidden.
//
// A hidden layer came back on the map in two ways (the live site, 2026-11-19): the hide was dropped when the
// map was still loading tiles, and every fresh style from the server drew all layers again. So the hidden list
// is applied in two places with the same rule: on the map as it is, and on a style before it is given to the map.

/** true when a map source belongs to a hidden layer. A source is the layer's id, or the id after a prefix
 * ("raster-source-{id}", "cog-source-{id}", "worldcover-source-{id}"). */
export function isHiddenSource(source: unknown, hiddenLayerIDs: readonly string[]): boolean {
  if (typeof source !== 'string') return false;
  return hiddenLayerIDs.some((id) => source === id || source.endsWith(`-${id}`));
}

interface StyleLayerLike {
  source?: unknown;
  layout?: Record<string, unknown>;
}

/** Marks the hidden layers' drawn layers as not shown, in a style about to be given to the map. Changes the
 * style it is given (pass a copy). */
export function hideInStyle(style: { layers?: StyleLayerLike[] }, hiddenLayerIDs: readonly string[]): void {
  if (hiddenLayerIDs.length === 0) return;
  for (const layer of style.layers ?? []) {
    if (isHiddenSource(layer.source, hiddenLayerIDs)) {
      layer.layout = { ...(layer.layout ?? {}), visibility: 'none' };
    }
  }
}
