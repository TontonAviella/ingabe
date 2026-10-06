import { apiFetch } from '@mundi/ee';
import { useQuery } from '@tanstack/react-query';

// Shapes returned by src/routes/drone_card_routes.py. Every word, number and
// status is decided on the server (src/services/drone_cards.py); the panel only shows them.

export interface DroneCard {
  id: string;
  service: number;
  service_name: string;
  question: string;
  preview: string;
  status: 'ready' | 'partly' | 'needs_flight' | 'needs_camera' | 'to_build' | 'learn';
  status_label: string;
}

export interface DroneDeck {
  photo: {
    summary: string;
    layer_id: string;
    name: string;
    place: string | null;
    area_ha: number | null;
    resolution_cm: number | null;
    camera: 'colour' | 'multispectral';
    camera_label: string;
    bounds: [number, number, number, number];
    photos_here: number;
  };
  audience: string;
  audience_label: string;
  audiences: { id: string; label: string }[];
  for_you: DroneCard[];
  services: { service: number; name: string; cards: DroneCard[] }[];
}

export interface DroneCardAnswer extends DroneCard {
  what: string;
  why: string;
  todo: string;
  how_sure: { level: 'low' | 'medium' | 'high'; label: string; bars: number; because: string[]; surer: string | null };
  overlay: { kind: 'bare' | 'attention' | 'good' | 'outline'; legend: string; geojson: GeoJSON.FeatureCollection } | null;
  facts: { label: string; value: string }[];
  terms: { id: string; word: string; simple: string; why: string }[];
}

/** Errors the server explains in words (a photo still processing, not a colour photo). */
export class DroneCardsError extends Error {
  constructor(
    public status: number,
    message: string,
  ) {
    super(message);
  }
}

async function getJson<T>(url: string): Promise<T> {
  const res = await apiFetch(url);
  if (!res.ok) {
    let detail = `Request failed (${res.status})`;
    try {
      detail = (await res.json()).detail ?? detail;
    } catch {
      /* not JSON: keep the status */
    }
    throw new DroneCardsError(res.status, detail);
  }
  return res.json();
}

function audienceParam(audience: string | null) {
  return audience ? `?audience=${encodeURIComponent(audience)}` : '';
}

export function useDroneDeck(layerId: string | null, audience: string | null) {
  return useQuery<DroneDeck, DroneCardsError>({
    queryKey: ['drone-cards', layerId, audience],
    queryFn: () => getJson<DroneDeck>(`/api/layer/${layerId}/cards${audienceParam(audience)}`),
    enabled: !!layerId,
    staleTime: 10 * 60 * 1000,
    retry: (count, error) => error.status === 409 && count < 20,
    retryDelay: 15_000,
  });
}

export function useDroneCardAnswer(layerId: string | null, cardId: string | null, audience: string | null) {
  return useQuery<DroneCardAnswer, DroneCardsError>({
    queryKey: ['drone-card-answer', layerId, cardId, audience],
    queryFn: () => getJson<DroneCardAnswer>(`/api/layer/${layerId}/cards/${cardId}${audienceParam(audience)}`),
    enabled: !!layerId && !!cardId,
    staleTime: 10 * 60 * 1000,
    retry: false,
  });
}
