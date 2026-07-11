import { describe, expect, it } from 'vitest';
import { parseMapResponse } from './mapResponse';

describe('map response contract regression', () => {
  it('rejects a final auth error instead of treating it as map data', async () => {
    const response = new Response(JSON.stringify({ detail: 'Token expired' }), {
      status: 401,
      headers: { 'content-type': 'application/json' },
    });

    await expect(parseMapResponse(response)).rejects.toThrow('Failed to fetch map: 401');
  });

  it('rejects malformed success payloads before MapLibre renders', async () => {
    const response = new Response(JSON.stringify({ map_id: 'Mbad' }), {
      status: 200,
      headers: { 'content-type': 'application/json' },
    });

    await expect(parseMapResponse(response)).rejects.toThrow('invalid shape');
  });

  it.each([
    { map_id: 'Mbad', project_id: 'Pbad', layers: [null], changelog: [] },
    { map_id: 'Mbad', project_id: 'Pbad', layers: [{ id: 'L1', name: 'Layer', type: 'raster', bounds: [NaN] }], changelog: [] },
    { map_id: 'Mbad', project_id: 'Pbad', layers: [], changelog: 'not-an-array' },
    { map_id: 'Mbad', project_id: 'Pbad', layers: [], changelog: [{ message: 'edit' }] },
  ])('rejects nested payload values that would crash map rendering', async (payload) => {
    const response = new Response(JSON.stringify(payload), {
      status: 200,
      headers: { 'content-type': 'application/json' },
    });

    await expect(parseMapResponse(response)).rejects.toThrow('invalid shape');
  });

  it('accepts a valid empty map', async () => {
    const payload = { map_id: 'Mgood', project_id: 'Pgood', layers: [], changelog: [] };
    const response = new Response(JSON.stringify(payload), {
      status: 200,
      headers: { 'content-type': 'application/json' },
    });

    await expect(parseMapResponse(response)).resolves.toEqual(payload);
  });
});
