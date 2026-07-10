import type { MapData } from './types';

export async function parseMapResponse(response: Response): Promise<MapData> {
  if (response.status === 404) {
    throw new Error('Map not found');
  }
  if (!response.ok) {
    throw new Error(`Failed to fetch map: ${response.status}`);
  }

  const payload: unknown = await response.json();
  if (!payload || typeof payload !== 'object' || !Array.isArray((payload as { layers?: unknown }).layers)) {
    throw new Error('Map response is missing its layers array');
  }
  return payload as MapData;
}
