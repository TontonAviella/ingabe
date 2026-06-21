import { gunzipSync, strFromU8 } from 'fflate';
import type { GeoJsonLayerUpdate } from './types';

function base64ToBytes(value: string): Uint8Array {
  if (typeof globalThis.atob === 'function') {
    const binary = globalThis.atob(value);
    const bytes = new Uint8Array(binary.length);
    for (let i = 0; i < binary.length; i += 1) {
      bytes[i] = binary.charCodeAt(i);
    }
    return bytes;
  }
  if (typeof Buffer !== 'undefined') {
    return new Uint8Array(Buffer.from(value, 'base64'));
  }
  throw new Error('No base64 decoder is available in this browser.');
}

export function decodeGeoJsonLayerData(layer: GeoJsonLayerUpdate): unknown {
  if (layer.geojson) {
    return layer.geojson;
  }
  if (layer.geojson_encoding === 'gzip+base64' && layer.geojson_gzip_b64) {
    const compressed = base64ToBytes(layer.geojson_gzip_b64);
    const jsonText = strFromU8(gunzipSync(compressed));
    return JSON.parse(jsonText);
  }
  throw new Error('GeoJSON layer update did not include renderable GeoJSON data.');
}

export function geoJsonFeatureCount(geojson: unknown): number | null {
  if (geojson && typeof geojson === 'object' && 'features' in geojson && Array.isArray((geojson as { features?: unknown }).features)) {
    return (geojson as { features: unknown[] }).features.length;
  }
  return null;
}
