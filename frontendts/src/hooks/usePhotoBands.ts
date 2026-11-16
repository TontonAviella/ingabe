import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { getJson, sendJson } from '@/lib/apiJson';
import type { ServerError } from '@/lib/serverError';

/** What a photo's file says of its bands, and whether the reader has to say which band is which. */
export interface PhotoBands {
  band_count: number;
  kind: 'colour' | 'multispectral' | 'unknown';
  roles: Record<string, number>;
  needs_names: boolean;
  said_by_reader: boolean;
  presets: { label: string; order: string[] }[];
  choices: { id: string; label: string }[];
  working: boolean;
  failed: boolean;
}

const WORKING_REFRESH_MS = 10_000;

/** Asked only for a photo the question cards could not read. */
export function usePhotoBands(layerId: string | null, enabled: boolean) {
  return useQuery<PhotoBands, ServerError>({
    queryKey: ['photo-bands', layerId],
    queryFn: () => getJson<PhotoBands>(`/api/layer/${layerId}/bands`),
    enabled: !!layerId && enabled,
    retry: false,
    refetchInterval: (query) => (query.state.data?.working ? WORKING_REFRESH_MS : false),
  });
}

/** The reader says which band is which; the photo's cards are asked again once the names are written. */
export function useSayPhotoBands(layerId: string | null) {
  const queryClient = useQueryClient();
  return useMutation<void, ServerError, { order: string[]; thermal_unit: string | null }>({
    mutationFn: async (body) => {
      await sendJson(`/api/layer/${layerId}/bands`, 'PUT', body);
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['photo-bands', layerId] });
    },
  });
}
