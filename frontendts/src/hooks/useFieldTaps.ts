import { apiFetch } from '@mundi/ee';
import { type QueryClient, useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { getJson, sendJson } from '@/lib/apiJson';
import { type ServerError, serverError } from '@/lib/serverError';

// Shapes returned by src/routes/field_tap_routes.py. The outlines, their areas, the number of corrections allowed
// and the words about what was measured are all decided on the server (src/services/field_taps.py).

export interface Tap {
  lon: number;
  lat: number;
  /** false: "not this part" */
  add: boolean;
}

export interface TapOutline {
  /** Which of the model's three outlines this is (0, 1, 2); sent back to correct or keep it. */
  choice: number;
  geometry: GeoJSON.Polygon;
  area_ha: number;
}

export interface KeptOutline {
  id: string;
  /** The field's number on this photo; it never changes and is never given to another field. */
  number: number;
  /** A point inside the outline, [longitude, latitude]. */
  centre: [number, number];
  geometry: GeoJSON.Polygon;
  area_ha: number;
  corrections: number;
  /** true: the reader moved the line by hand; the area is of their line, not the model's. */
  by_hand?: boolean;
  kept_at: string;
  /** Share of the field under living green plants (0..1), or why it was not measured. Missing until measured. */
  green_cover?: { share: number | null; why_not: string | null };
}

/** A corner of an outline as [longitude, latitude]. */
export type Corner = [number, number];

export interface FieldTapState {
  prepared: boolean;
  windows: number;
  max_corrections: number;
  measured: string;
  /** What the green cover figure is and how it was measured, written by the server. */
  cover_measured: string;
  kept: KeptOutline[];
  /** How many taps on this photo ended without a kept outline. */
  turned_down: number;
}

/** Why a tap ended without a kept outline; the server's list (field_taps.TURN_DOWN_REASONS). */
export type TurnDownReason = 'none_of_these' | 'started_again' | 'closed' | 'no_outline';

const url = (layerId: string) => `/api/layer/${layerId}/field-taps`;

/** What the photo offers before the first tap. 404: outlines from taps are not available here, final. */
export function useFieldTapState(layerId: string | null) {
  return useQuery<FieldTapState, ServerError>({
    queryKey: ['field-taps', layerId],
    queryFn: () => getJson<FieldTapState>(url(layerId as string)),
    enabled: !!layerId,
    retry: (count, error) => !(error.status >= 400 && error.status < 500) && count < 2,
    staleTime: 60_000,
  });
}

/** left_out: first-tap choices the server did not offer because they ran along the edge of what was read. */
export function outlinesFor(layerId: string, taps: Tap[], pick: number | null): Promise<{ outlines: TapOutline[]; left_out?: number }> {
  return sendJson(`${url(layerId)}/outline`, 'POST', { taps, pick });
}

/** Tell the server these taps ended without a kept outline, so kept and turned-down taps can be counted. */
export function turnDown(layerId: string, taps: Tap[], pick: number | null, reason: TurnDownReason): Promise<unknown> {
  return sendJson(`${url(layerId)}/turned-down`, 'POST', { taps, pick, reason });
}

/** The area of an outline whose line the reader moved by hand; rejects with the server's reason when it is no outline. */
export function measureMovedLine(layerId: string, firstTap: Tap, moved: Corner[]): Promise<{ area_ha: number }> {
  return sendJson(`${url(layerId)}/moved-line`, 'POST', { first_tap: firstTap, moved });
}

/** The kept fields changed: they are also the plots the question cards read, so those are asked again too. */
function fieldsChanged(client: QueryClient, layerId: string | null) {
  client.invalidateQueries({ queryKey: ['field-taps', layerId] });
  client.invalidateQueries({ queryKey: ['drone-cards', layerId] });
  client.invalidateQueries({ queryKey: ['drone-card-answer', layerId] });
}

export function useKeepOutline(layerId: string | null) {
  const client = useQueryClient();
  return useMutation<KeptOutline, ServerError, { taps: Tap[]; pick: number; moved?: Corner[] }>({
    mutationFn: (body) => sendJson<KeptOutline>(`${url(layerId as string)}/kept`, 'POST', body),
    onSuccess: () => fieldsChanged(client, layerId),
  });
}

export function useRemoveOutline(layerId: string | null) {
  const client = useQueryClient();
  return useMutation<void, ServerError, string>({
    mutationFn: async (id) => {
      const res = await apiFetch(`${url(layerId as string)}/kept/${id}`, { method: 'DELETE' });
      if (!res.ok) throw await serverError(res);
    },
    onSuccess: () => fieldsChanged(client, layerId),
  });
}
