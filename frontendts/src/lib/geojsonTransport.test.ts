import { gzipSync, strToU8 } from 'fflate';
import { describe, expect, it } from 'vitest';
import { decodeGeoJsonLayerData, geoJsonFeatureCount } from './geojsonTransport';

describe('geojsonTransport', () => {
  it('decodes gzip+base64 GeoJSON layer updates', () => {
    const geojson = {
      type: 'FeatureCollection',
      features: [
        {
          type: 'Feature',
          geometry: { type: 'Point', coordinates: [30, -2] },
          properties: { confidence: 0.72 },
        },
      ],
    };
    const compressed = gzipSync(strToU8(JSON.stringify(geojson)));
    const payload = Buffer.from(compressed).toString('base64');

    const decoded = decodeGeoJsonLayerData({
      source_id: 'test-source',
      geojson_encoding: 'gzip+base64',
      geojson_gzip_b64: payload,
    });

    expect(decoded).toEqual(geojson);
    expect(geoJsonFeatureCount(decoded)).toBe(1);
  });

  it('keeps identity GeoJSON updates renderable', () => {
    const geojson = { type: 'FeatureCollection', features: [] };

    expect(decodeGeoJsonLayerData({ source_id: 'plain', geojson })).toBe(geojson);
    expect(geoJsonFeatureCount(geojson)).toBe(0);
  });
});
