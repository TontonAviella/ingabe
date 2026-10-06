import { apiFetch } from '@mundi/ee';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';

// Shapes returned by src/routes/drone_card_routes.py. Every word, number and
// status is decided on the server (src/services/drone_cards.py); the panel only shows them.

export interface DroneCard {
  id: string;
  service: number;
  service_name: string;
  question: string;
  preview: string;
  status: 'ready' | 'partly' | 'working' | 'needs_flight' | 'needs_camera' | 'to_build' | 'learn';
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
  overlay: {
    kind: 'bare' | 'attention' | 'good' | 'outline' | 'plots' | 'plot_groups' | 'crop_map' | 'plot_flags';
    legend: string;
    /** One entry per class drawn (a crop map); the key picks its colour. */
    legend_items?: { key: string; label: string; count: number }[];
    geojson: GeoJSON.FeatureCollection;
  } | null;
  facts: { label: string; value: string }[];
  terms: { id: string; word: string; simple: string; why: string }[];
  /** Places to go first, each with a point to fly to. */
  items: { id: string; title: string; detail: string; lon: number; lat: number }[];
  downloads: { label: string; href: string }[];
  /** Set while the answer is still being worked out on the server. */
  progress: { done: number; parts: number; minutes_left: number | null } | null;
  /** Options that change the answer (where the plots come from); picking one is sent to href. */
  choices: {
    label: string;
    options: { id: string; label: string; detail: string; selected: boolean }[];
    href: string;
  } | null;
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

/** The server's own words for a failed request, or its status. */
async function failure(res: Response): Promise<DroneCardsError> {
  let detail = `Request failed (${res.status})`;
  try {
    detail = (await res.json()).detail ?? detail;
  } catch {
    /* not JSON: keep the status */
  }
  return new DroneCardsError(res.status, detail);
}

async function getJson<T>(url: string): Promise<T> {
  const res = await apiFetch(url);
  if (!res.ok) throw await failure(res);
  return res.json();
}

function audienceParam(audience: string | null) {
  return audience ? `?audience=${encodeURIComponent(audience)}` : '';
}

// While a card is still being worked out on the server, ask again at this pace.
const WORKING_REFRESH_MS = 15_000;

export function useDroneDeck(layerId: string | null, audience: string | null) {
  return useQuery<DroneDeck, DroneCardsError>({
    queryKey: ['drone-cards', layerId, audience],
    queryFn: () => getJson<DroneDeck>(`/api/layer/${layerId}/cards${audienceParam(audience)}`),
    enabled: !!layerId,
    staleTime: 10 * 60 * 1000,
    retry: (count, error) => error.status === 409 && count < 20,
    retryDelay: 15_000,
    refetchInterval: (query) =>
      query.state.data?.services.some((service) => service.cards.some((card) => card.status === 'working')) ? WORKING_REFRESH_MS : false,
  });
}

export function useDroneCardAnswer(layerId: string | null, cardId: string | null, audience: string | null) {
  return useQuery<DroneCardAnswer, DroneCardsError>({
    queryKey: ['drone-card-answer', layerId, cardId, audience],
    queryFn: () => getJson<DroneCardAnswer>(`/api/layer/${layerId}/cards/${cardId}${audienceParam(audience)}`),
    enabled: !!layerId && !!cardId,
    staleTime: 10 * 60 * 1000,
    retry: false,
    refetchInterval: (query) => (query.state.data?.progress ? WORKING_REFRESH_MS : false),
  });
}

/** Pick where a photo's plots come from; every card of that photo is asked again. */
export function useChoosePlotSource(layerId: string | null) {
  const queryClient = useQueryClient();
  return useMutation<void, DroneCardsError, { href: string; source: string }>({
    mutationFn: async ({ href, source }) => {
      const res = await apiFetch(href, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ source }),
      });
      if (!res.ok) throw await failure(res);
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['drone-cards', layerId] });
      queryClient.invalidateQueries({ queryKey: ['drone-card-answer', layerId] });
    },
  });
}
