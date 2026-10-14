import { useQuery } from '@tanstack/react-query';
import { getJson, sendJson } from '@/lib/apiJson';
import type { ServerError } from '@/lib/serverError';
import type { FieldCloseLook, InspectionCloseLooks, PhotoCloseLook } from '@/lib/towerContract';

// A close look is started by asking for it (POST: safe to repeat, a kept or running look is never started twice; a
// person who may not edit gets 403 and only reads it), and asked again while it works. 404: not available here
// (another industry, not a photo), final.

const POLL_MS = 5_000;
const final = (count: number, error: ServerError) => !(error.status >= 400 && error.status < 500) && count < 2;

// Each look is asked to start once per page visit; after that it is only read (also while it works).
const started = new Set<string>();

async function startOrRead<T>(url: string): Promise<T> {
  if (started.has(url)) return getJson<T>(url);
  started.add(url);
  try {
    return await sendJson<T>(url, 'POST', {});
  } catch (error) {
    if ((error as ServerError).status !== 403) {
      started.delete(url);
      throw error;
    }
    return getJson<T>(url);
  }
}

/** The close look at one inspection photo, started when the photo is opened. */
export function usePhotoCloseLook(layerId: string, sha256: string) {
  const url = `/api/towers/layer/${encodeURIComponent(layerId)}/photos/${encodeURIComponent(sha256)}/close-look`;
  return useQuery<PhotoCloseLook, ServerError>({
    queryKey: ['close-look-photo', layerId, sha256],
    queryFn: () => startOrRead<PhotoCloseLook>(url),
    retry: final,
    refetchInterval: (query) => (query.state.data?.status === 'working' ? POLL_MS : false),
    staleTime: 60_000,
  });
}

/** The close looks of an inspection's photos, for the map; a small inspection is looked at whole. */
export function useInspectionCloseLooks(layerId: string | null) {
  const url = `/api/towers/layer/${encodeURIComponent(layerId ?? '')}/close-look`;
  return useQuery<InspectionCloseLooks, ServerError>({
    queryKey: ['close-look-inspection', layerId],
    queryFn: () => startOrRead<InspectionCloseLooks>(url),
    enabled: !!layerId,
    retry: final,
    refetchInterval: (query) => (query.state.data?.running ? POLL_MS : false),
    staleTime: 60_000,
  });
}

/** The close look at a drone photo (a farm orthomosaic), for the map. */
export function useFieldCloseLook(layerId: string | null) {
  const url = `/api/layer/${encodeURIComponent(layerId ?? '')}/close-look`;
  return useQuery<FieldCloseLook, ServerError>({
    queryKey: ['close-look-field', layerId],
    queryFn: () => startOrRead<FieldCloseLook>(url),
    enabled: !!layerId,
    retry: final,
    refetchInterval: (query) => (query.state.data?.status === 'working' ? POLL_MS : false),
    staleTime: 60_000,
  });
}
