import { apiFetch } from '@mundi/ee';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { getJson, sendJson } from '@/lib/apiJson';
import type { AnswerDownload, AnswerTerm, AskSage, CardAnswerCore } from '@/lib/cardAnswer';
import { type ServerError, serverError } from '@/lib/serverError';

// Shapes returned by src/routes/drone_card_routes.py. Every word, number and
// status is decided on the server (src/services/drone_cards.py); the panel only shows them.

export interface DroneCard {
  id: string;
  service: number;
  service_name: string;
  question: string;
  preview: string;
  status: 'ready' | 'partly' | 'working' | 'needs_flight' | 'needs_camera' | 'cant_tell' | 'to_build' | 'learn';
  status_label: string;
  /** Its answer changed since this reader last looked. */
  new?: boolean;
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
  /** Questions for Sage built from what this photo shows. */
  ask_sage: AskSage[];
  seed: number;
}

export interface DroneCardAnswer extends DroneCard, CardAnswerCore {
  overlay: {
    kind: 'bare' | 'attention' | 'good' | 'outline' | 'plots' | 'plot_groups' | 'crop_map' | 'plot_flags';
    legend: string;
    /** One entry per class drawn (a crop map); the key picks its colour. */
    legend_items?: { key: string; label: string; count: number }[];
    geojson: GeoJSON.FeatureCollection;
    /** The photo with holes at the plots that matter: drawn dark so they stand out. */
    spotlight?: GeoJSON.FeatureCollection | null;
    /** Exact spots measured inside those plots (bare soil), drawn bright. */
    spots?: GeoJSON.FeatureCollection;
    spots_legend?: string;
  } | null;
  terms: AnswerTerm[];
  downloads: AnswerDownload[];
  /** Options that change the answer (where the plots come from); picking one is sent to href. */
  choices: {
    label: string;
    options: { id: string; label: string; detail: string; selected: boolean }[];
    href: string;
  } | null;
  ask_sage: AskSage[];
  /** A document the reader can add (soil lab report, harvest records) to answer with their own numbers. */
  upload: { label: string; href: string; accept: string } | null;
  /** Plots the reader can say the real crop of; href has a {number} slot. */
  field_check: {
    title: string;
    detail: string;
    plots: { id: string; number: number; title: string; detail: string; lon: number; lat: number; picture?: string }[];
    options: { id: string; label: string }[];
    href: string;
    checked: number;
  } | null;
}

/** What the server read in an added document. */
export interface FarmRecordResult {
  id: string;
  kind: 'soil_report' | 'harvest_records' | 'other';
  title: string | null;
  soil_samples: number;
  harvests: number;
  warnings: string[];
}

function query(audience: string | null, seed: number) {
  const params = new URLSearchParams({ seed: String(seed) });
  if (audience) params.set('audience', audience);
  return `?${params}`;
}

// While a card is still being worked out on the server, ask again at this pace.
const WORKING_REFRESH_MS = 15_000;

/** The deck for a photo; another seed picks other wordings and another mix of questions. */
export function useDroneDeck(layerId: string | null, audience: string | null, seed: number) {
  return useQuery<DroneDeck, ServerError>({
    queryKey: ['drone-cards', layerId, audience, seed],
    queryFn: () => getJson<DroneDeck>(`/api/layer/${layerId}/cards${query(audience, seed)}`),
    placeholderData: (previous) => previous,
    enabled: !!layerId,
    staleTime: 10 * 60 * 1000,
    retry: (count, error) => error.status === 409 && count < 20,
    retryDelay: 15_000,
    refetchInterval: (query) =>
      query.state.data?.services.some((service) => service.cards.some((card) => card.status === 'working')) ? WORKING_REFRESH_MS : false,
  });
}

export function useDroneCardAnswer(layerId: string | null, cardId: string | null, audience: string | null, seed: number) {
  return useQuery<DroneCardAnswer, ServerError>({
    queryKey: ['drone-card-answer', layerId, cardId, audience, seed],
    queryFn: () => getJson<DroneCardAnswer>(`/api/layer/${layerId}/cards/${cardId}${query(audience, seed)}`),
    enabled: !!layerId && !!cardId,
    staleTime: 10 * 60 * 1000,
    retry: false,
    refetchInterval: (query) => (query.state.data?.progress ? WORKING_REFRESH_MS : false),
  });
}

/** Pick where a photo's plots come from; every card of that photo is asked again. */
export function useChoosePlotSource(layerId: string | null) {
  const queryClient = useQueryClient();
  return useMutation<void, ServerError, { href: string; source: string }>({
    mutationFn: async ({ href, source }) => {
      await sendJson(href, 'PUT', { source });
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['drone-cards', layerId] });
      queryClient.invalidateQueries({ queryKey: ['drone-card-answer', layerId] });
    },
  });
}

/** Add a soil lab report or harvest records; the photo's cards are asked again once it is read. */
export function useAddFarmRecord(layerId: string | null) {
  const queryClient = useQueryClient();
  return useMutation<FarmRecordResult, ServerError, { href: string; file: File }>({
    mutationFn: async ({ href, file }) => {
      const body = new FormData();
      body.append('file', file);
      // Reading a document takes the vision model about 10 seconds; give it more than the usual 30 s limit.
      const res = await apiFetch(href, { method: 'POST', body, signal: AbortSignal.timeout(120_000) });
      if (!res.ok) throw await serverError(res);
      return res.json();
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['drone-cards', layerId] });
      queryClient.invalidateQueries({ queryKey: ['drone-card-answer', layerId] });
    },
  });
}

/** Say what really grows in a plot; every card of the photo is asked again. */
export function useCheckPlot(layerId: string | null) {
  const queryClient = useQueryClient();
  return useMutation<void, ServerError, { href: string; number: number; crop: string }>({
    mutationFn: async ({ href, number, crop }) => {
      await sendJson(href.replace('{number}', String(number)), 'POST', { crop });
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['drone-cards', layerId] });
      queryClient.invalidateQueries({ queryKey: ['drone-card-answer', layerId] });
    },
  });
}
