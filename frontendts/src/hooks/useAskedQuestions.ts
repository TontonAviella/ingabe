import { useQuery } from '@tanstack/react-query';
import { getJson, sendJson } from '@/lib/apiJson';

// A person's own questions to Sage on a photo, and the questions others in the same industry asked (in general
// words, offered once 3 or more people asked them). The server owns the rules: src/services/asked_questions.py.

/** Questions others asked, a few offered today. `base` is the photo's cards path (drone or tower). */
export function useOthersAsked(base: string | null) {
  return useQuery<{ questions: string[] }>({
    queryKey: ['others-asked', base],
    queryFn: () => getJson<{ questions: string[] }>(`${base}/asked`),
    enabled: !!base,
    staleTime: 60 * 60 * 1000,
    retry: false,
  });
}

/** Keep a question the person asked, so Ingabe learns what people ask. Never blocks or fails the question itself. */
export function rememberAsked(base: string, question: string) {
  void sendJson(`${base}/asked`, 'POST', { question }).catch(() => undefined);
}
