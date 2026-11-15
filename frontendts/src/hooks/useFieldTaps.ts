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
  /** true while the server is measuring green cover for kept fields in the background: ask again soon. */
  cover_filling?: boolean;
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
    // green cover of fields kept earlier is measured in the background: ask again until the server says it is done
    refetchInterval: (query) => (query.state.data?.cover_filling ? 3_000 : false),
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

/** A hand-moved line as the server holds it. `tidied`: the line crossed itself and the server cut the loops away;
 * `geometry` is then the line to draw in place of the one sent. */
export interface MovedLine {
  area_ha: number;
  geometry: GeoJSON.Polygon;
  tidied?: boolean;
}

/** The area of an outline whose line the reader moved by hand; rejects with the server's reason when it is no outline. */
export function measureMovedLine(layerId: string, firstTap: Tap, moved: Corner[]): Promise<MovedLine> {
  return sendJson(`${url(layerId)}/moved-line`, 'POST', { first_tap: firstTap, moved });
}

/** A tap on a plant or on something that is not one, as longitude and latitude. */
export interface Place {
  lon: number;
  lat: number;
}

/** The reader's crop in one kept field (src/services/my_crop.py). */
export interface MyCropFound {
  my_crop: number;
  other_plants: number;
  other_green: number;
  /** The share of the field in ground where single plants can be told apart (0..1). */
  counted_share: number;
  per_ha: number | null;
  /** How it was measured and what was not, written by the server. */
  measured: string;
  /** Points with kind_code 0 my crop, 1 other plant, 2 other green. */
  points: GeoJSON.FeatureCollection;
}

/** The plants of the reader's crop in a kept field, or `working` while the server reads the field: ask again. */
export function countMyCrop(
  layerId: string,
  fieldId: string,
  crop: Place[],
  other: Place[],
): Promise<MyCropFound | { working: true; done: number; total: number }> {
  return sendJson(`${url(layerId)}/kept/${fieldId}/my-crop`, 'POST', { crop, other });
}

/** What the reader's own checks of the plant count say for a field (src/services/count_check.py). */
export interface CountChecked {
  parts: number;
  app: number;
  you: number;
  wrong: number;
  missed: number;
  /** The same, as a sentence written by the server. */
  said: string;
}

/** One part of a field, chosen by the server, with the plants the count holds there. */
export interface SquareToCheck {
  /** The part's place on the photo's grid, sent back with the check. */
  square: [number, number];
  /** Each plant as [longitude, latitude]; a ring that is not a plant is named by its place in this list. */
  plants: [number, number][];
  app: number;
  /** The part of the square that lies in the field: plants are counted there and nowhere else. */
  part: GeoJSON.Polygon | GeoJSON.MultiPolygon;
  side_m: number;
  squares_in_field: number;
  checked: CountChecked | null;
  measured: string;
}

export interface CheckedSquare {
  app: number;
  you: number;
  wrong: number;
  missed: number;
}

const checkUrl = (layerId: string, fieldId: string) => `${url(layerId)}/kept/${fieldId}/count-check`;

/** What the reader's checks say for a kept field; `checked` is null when none was made. */
export function useCountChecked(layerId: string | null, fieldId: string | null) {
  return useQuery<{ checked: CountChecked | null; measured: string }, ServerError>({
    queryKey: ['field-count-check', layerId, fieldId],
    queryFn: () => getJson(checkUrl(layerId as string, fieldId as string)),
    enabled: !!layerId && !!fieldId,
    retry: false,
  });
}

/** The next part of the field to check; `seen` are parts already shown and passed over. */
export function squareToCheck(layerId: string, fieldId: string, seen: [number, number][]): Promise<SquareToCheck> {
  return sendJson(`${checkUrl(layerId, fieldId)}/square`, 'POST', { seen });
}

/** Keep the reader's check of one part: the rings that are not plants and the plants that got no ring. */
export function keepCountCheck(
  layerId: string,
  fieldId: string,
  square: [number, number],
  wrong: number[],
  missed: Place[],
): Promise<{ here: CheckedSquare; checked: CountChecked | null; measured: string }> {
  return sendJson(checkUrl(layerId, fieldId), 'POST', { square, wrong, missed });
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
