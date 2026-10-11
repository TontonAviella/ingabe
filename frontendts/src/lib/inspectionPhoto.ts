import type { MapLayer } from '@/lib/types';

// The photo behind a camera point of a photo inspection (a vector layer made
// from uploaded drone or field photos). The server names each photo by its
// SHA-256 and serves it to anyone who may view the layer.

const SHA256 = /^[0-9a-f]{64}$/;

// The original_format the upload gives a photo inspection: a ZIP of photos, or one photo
// (src/upload/handlers/photo_set_handler.py).
const INSPECTION_FORMATS = ['photo_zip', 'photo'];

/** True for a layer of camera points made from uploaded photos. */
export function isPhotoInspectionLayer(layer: MapLayer): boolean {
  return layer.type === 'vector' && INSPECTION_FORMATS.includes(layer.metadata?.original_format ?? '');
}

/** The URL of the inspection photo for a feature of `layerId`, or null when the feature is not a camera point. */
export function inspectionPhotoUrl(layerId: unknown, properties: Record<string, unknown> | null | undefined): string | null {
  if (typeof layerId !== 'string' || !properties) return null;
  const { sha256, photo_key: photoKey } = properties;
  if (typeof photoKey !== 'string' || typeof sha256 !== 'string' || !SHA256.test(sha256)) return null;
  return `/api/layer/${encodeURIComponent(layerId)}/photos/${sha256}`;
}
