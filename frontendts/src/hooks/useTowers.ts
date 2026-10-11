import { type QueryClient, useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { getJson, sendJson } from '@/lib/apiJson';
import type { ServerError } from '@/lib/serverError';
import {
  type FinderJob,
  type InspectionPhotos,
  type MarksPayload,
  type PartWork,
  PHOTOS_TO_CHECK_AUDIENCE,
  PHOTOS_TO_CHECK_CARD,
  type PhotoWork,
  type ReviewPayload,
  type TowerAudience,
  type TowerCardAnswer,
  type TowerCards,
  type TowerPart,
} from '@/lib/towerContract';
import { anyJobActive } from '@/lib/towerReview';

// Requests of the tower inspection screens (src/lib/towerContract.ts has the shapes). Reads need view rights on the
// layer, writes edit rights; every route answers 404 when towers are off or the project is not a telecom project.

const base = (layerId: string) => `/api/towers/layer/${encodeURIComponent(layerId)}`;
const photoBase = (layerId: string, sha256: string) => `${base(layerId)}/photos/${encodeURIComponent(sha256)}`;

// While a find job is in line or running, ask for the photo's work again at this pace.
const JOB_POLL_MS = 2_000;

const workKey = (layerId: string, sha256: string) => ['tower-work', layerId, sha256];

/** A 404 means "no towers here" (switched off, or not a telecom project): asking again will not change it. */
const noRetryOn404 = (count: number, error: ServerError) => error.status !== 404 && count < 2;

export function useInspectionPhotos(layerId: string | null) {
  return useQuery<InspectionPhotos, ServerError>({
    queryKey: ['tower-photos', layerId],
    queryFn: () => getJson<InspectionPhotos>(`${base(layerId as string)}/photos`),
    enabled: !!layerId,
    staleTime: 5 * 60 * 1000,
    retry: noRetryOn404,
  });
}

function fetchWork(layerId: string, sha256: string) {
  return getJson<PhotoWork>(`${photoBase(layerId, sha256)}/work`);
}

/** The marks, finds and checks of one photo; asked again every 2 s while a find job is in line or running. */
export function usePhotoWork(layerId: string, sha256: string) {
  return useQuery<PhotoWork, ServerError>({
    queryKey: workKey(layerId, sha256),
    queryFn: () => fetchWork(layerId, sha256),
    retry: noRetryOn404,
    refetchInterval: (query) => (anyJobActive(query.state.data) ? JOB_POLL_MS : false),
  });
}

function useSetPart(layerId: string, sha256: string) {
  const queryClient = useQueryClient();
  return (part: TowerPart, change: (old: PartWork) => PartWork) =>
    queryClient.setQueryData<PhotoWork>(workKey(layerId, sha256), (old) =>
      old ? { ...old, parts: { ...old.parts, [part]: change(old.parts[part]) } } : old,
    );
}

/** Save one part's marks (and the look-alikes); the server answers with that part's work. */
export function useSaveMarks(layerId: string, sha256: string) {
  const setPart = useSetPart(layerId, sha256);
  return useMutation<PartWork, ServerError, MarksPayload>({
    mutationFn: (payload) => sendJson<PartWork>(`${photoBase(layerId, sha256)}/marks`, 'PUT', payload),
    onSuccess: (work, payload) => setPart(payload.part, () => work),
  });
}

/** Start finding the rest of one part. 422: the server's reason (marks too small, a photo not like the measured
 * ones); 429/503: the finder's line is full. */
export function useFindTheRest(layerId: string, sha256: string) {
  const queryClient = useQueryClient();
  const setPart = useSetPart(layerId, sha256);
  return useMutation<FinderJob, ServerError, { part: TowerPart }>({
    mutationFn: (payload) => sendJson<FinderJob>(`${photoBase(layerId, sha256)}/find`, 'POST', payload),
    onSuccess: (job, { part }) => {
      setPart(part, (old) => ({ ...old, job }));
      queryClient.invalidateQueries({ queryKey: workKey(layerId, sha256) });
    },
  });
}

/** Save the person's check of one part; the cards are asked again, since their counts come from these checks. */
export function useSaveReview(layerId: string, sha256: string) {
  const queryClient = useQueryClient();
  const setPart = useSetPart(layerId, sha256);
  return useMutation<PartWork, ServerError, ReviewPayload>({
    mutationFn: (payload) => sendJson<PartWork>(`${photoBase(layerId, sha256)}/review`, 'PUT', payload),
    onSuccess: (work, payload) => {
      setPart(payload.part, () => work);
      queryClient.invalidateQueries({ queryKey: ['tower-cards', layerId] });
      queryClient.invalidateQueries({ queryKey: ['tower-card-answer', layerId] });
    },
  });
}

export function useTowerCards(layerId: string | null, audience: TowerAudience) {
  return useQuery<TowerCards, ServerError>({
    queryKey: ['tower-cards', layerId, audience],
    queryFn: () => getJson<TowerCards>(`${base(layerId as string)}/cards?audience=${audience}`),
    enabled: !!layerId,
    placeholderData: (previous) => previous,
    staleTime: 60_000,
    retry: noRetryOn404,
  });
}

const answerKey = (layerId: string | null, cardId: string | null, audience: TowerAudience) => [
  'tower-card-answer',
  layerId,
  cardId,
  audience,
];

function fetchAnswer(layerId: string, cardId: string, audience: TowerAudience) {
  return getJson<TowerCardAnswer>(`${base(layerId)}/cards/${encodeURIComponent(cardId)}?audience=${audience}`);
}

/** The server's list of the photos still to check, asked afresh: it changes with every mark, find and review. */
export function photosToCheckNow(queryClient: QueryClient, layerId: string): Promise<TowerCardAnswer> {
  return queryClient.fetchQuery({
    queryKey: answerKey(layerId, PHOTOS_TO_CHECK_CARD, PHOTOS_TO_CHECK_AUDIENCE),
    queryFn: () => fetchAnswer(layerId, PHOTOS_TO_CHECK_CARD, PHOTOS_TO_CHECK_AUDIENCE),
    staleTime: 0,
  });
}

export function useTowerCardAnswer(layerId: string | null, cardId: string | null, audience: TowerAudience) {
  return useQuery<TowerCardAnswer, ServerError>({
    queryKey: answerKey(layerId, cardId, audience),
    queryFn: () => fetchAnswer(layerId as string, cardId as string, audience),
    enabled: !!layerId && !!cardId,
    staleTime: 60_000,
    retry: false,
    refetchInterval: (query) => (query.state.data?.progress ? 15_000 : false),
  });
}
