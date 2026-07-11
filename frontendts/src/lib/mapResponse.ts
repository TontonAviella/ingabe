import type { MapData } from './types';

function isRecord(value: unknown): value is Record<string, unknown> {
  return !!value && typeof value === 'object' && !Array.isArray(value);
}

function isMapLayer(value: unknown): boolean {
  if (!isRecord(value)) return false;
  if (typeof value.id !== 'string' || typeof value.name !== 'string' || typeof value.type !== 'string') return false;
  if (value.bounds !== undefined) {
    if (!Array.isArray(value.bounds) || value.bounds.some((item) => typeof item !== 'number' || !Number.isFinite(item))) {
      return false;
    }
  }
  return value.metadata === undefined || isRecord(value.metadata);
}

function isChangelogEntry(value: unknown): boolean {
  return (
    isRecord(value) &&
    typeof value.message === 'string' &&
    typeof value.map_state === 'string' &&
    (typeof value.last_edited === 'string' || value.last_edited === null)
  );
}

export async function parseMapResponse(response: Response): Promise<MapData> {
  if (response.status === 404) {
    throw new Error('Map not found');
  }
  if (!response.ok) {
    throw new Error(`Failed to fetch map: ${response.status}`);
  }

  const payload: unknown = await response.json();
  if (
    !isRecord(payload) ||
    typeof payload.map_id !== 'string' ||
    typeof payload.project_id !== 'string' ||
    !Array.isArray(payload.layers) ||
    !payload.layers.every(isMapLayer) ||
    !Array.isArray(payload.changelog) ||
    !payload.changelog.every(isChangelogEntry)
  ) {
    throw new Error('Map response has an invalid shape');
  }
  return payload as unknown as MapData;
}
