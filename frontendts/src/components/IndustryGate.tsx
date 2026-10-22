import { apiFetch } from '@mundi/ee';
import { type QueryClient, useQuery, useQueryClient } from '@tanstack/react-query';
import { lazy, Suspense, useCallback, useEffect, useRef, useState } from 'react';
import { toast } from 'sonner';

// Ingabe serves three industries. The first time someone signs in, before anything else, they pick theirs from
// three miniature worlds (the same dioramas as nozalabs.rw), so "grid" or "towers" is never a guess. The choice is
// saved on the account (/api/user/industry) and switched in one tap from the home's industry tabs or the sidebar's
// "Your industry" menu (the full-screen picker is only the first choice). A switch changes
// only the user's own view, also for a company's owners and admins. Members of a company that has an industry start
// in it (its default) and are never asked, but can switch too.

export type IndustryKey = 'agriculture' | 'power_grid' | 'telecom';

export interface IndustryState {
  industry: IndustryKey | null; // the industry the user works in now: their own choice, else their company's
  source: 'company' | 'you' | null;
  // The company the user acts for, and its industry: the default for members who never chose their own.
  company: { name: string; industry: IndustryKey | null } | null;
  can_choose: boolean; // false without an account row (legacy single-user mode): nothing to ask
  options: { key: IndustryKey; label: string; note: string }[];
}

export const INDUSTRY_KEY = ['user', 'industry'];
export const OPEN_INDUSTRY_PICKER = 'ingabe:choose-industry';

const IndustryPicker = lazy(() => import('@/components/IndustryPicker'));

/** The signed-in user's industry; null data when nobody is signed in. */
export function useIndustry() {
  return useQuery<IndustryState | null>({
    queryKey: INDUSTRY_KEY,
    queryFn: async () => {
      const res = await apiFetch('/api/user/industry');
      if (res.status === 401 || res.status === 403) return null; // not signed in: nothing to ask
      if (!res.ok) throw new Error('Could not load your industry');
      return res.json();
    },
    staleTime: 10 * 60 * 1000,
    retry: 1,
  });
}

// One request per target industry at a time, shared by every caller (the tabs, the sidebar menu, a map that mounts
// twice): on 2026-10-22 opening a Telecom map sent the same switch twice within a second.
const switching = new Map<IndustryKey, Promise<IndustryState>>();

/** Switches the user's industry to `wanted` (saved on the account) and reloads the lists; resolves with the saved
 * state. The lists keep showing the current industry until the server has saved the new one: the server, not this
 * tab, decides what they list, so the industry query only changes once it agrees. */
export function switchIndustry(queryClient: QueryClient, wanted: IndustryKey): Promise<IndustryState> {
  const pending = switching.get(wanted);
  if (pending) return pending;
  const request = apiFetch('/api/user/industry', {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ industry: wanted }),
  })
    .then(async (res) => {
      if (!res.ok) throw new Error((await res.json().catch(() => null))?.detail ?? 'Could not switch your industry');
      const saved = (await res.json()) as IndustryState;
      queryClient.setQueryData(INDUSTRY_KEY, saved);
      queryClient.invalidateQueries({ queryKey: ['projects'] });
      return saved;
    })
    .finally(() => switching.delete(wanted));
  switching.set(wanted, request);
  return request;
}

/** One-tap switching for buttons and menus: `pending` is the industry being switched to, for a busy state. */
export function useSwitchIndustry() {
  const queryClient = useQueryClient();
  const [pending, setPending] = useState<IndustryKey | null>(null);
  const switchTo = useCallback(
    (wanted: IndustryKey, label: string) => {
      setPending(wanted);
      switchIndustry(queryClient, wanted)
        .catch(() => toast.error(`Could not switch to ${label}. Check your connection and try again.`))
        .finally(() => setPending(null));
    },
    [queryClient],
  );
  return { switchTo, pending };
}

/** One industry at a time: opening a project of another industry switches the user into the project's (saved on
 * the account like a sidebar switch), so the sidebar, the lists, the cards and Sage all say the same industry. Once
 * per project and industry; nothing when signed out or in the legacy single-user mode. */
export function useFollowProjectIndustry(projectIndustry: IndustryKey | null | undefined) {
  const { data } = useIndustry();
  const queryClient = useQueryClient();
  const switched = useRef<string | null>(null);
  useEffect(() => {
    const wanted = projectIndustry ?? 'agriculture';
    if (!data || !data.can_choose || data.industry === wanted || switched.current === wanted) return;
    switched.current = wanted;
    const label = data.options.find((o) => o.key === wanted)?.label ?? wanted;
    const first = !switching.has(wanted); // a second mount joins the first switch and says nothing again
    switchIndustry(queryClient, wanted)
      .then(() => {
        if (first) toast(`This project is ${label}: you are working in ${label} now.`);
      })
      .catch(() => {
        switched.current = null;
        if (first) toast.error(`This project is ${label}, but your industry could not be switched to it.`);
      });
  }, [data, projectIndustry, queryClient]);
}

/** True once new projects' industry is settled: known, or nothing to ask (signed out, legacy single-user mode). */
export function useIndustryKnown(): boolean {
  const { data, isLoading, isError } = useIndustry();
  if (isLoading || isError) return false;
  return !data || !data.can_choose || data.industry !== null;
}

/** Asks a signed-in user which industry they work in, once, before anything else; also reopened from the sidebar. */
export function IndustryGate() {
  const { data, isError, refetch, isFetching } = useIndustry();
  const [reopened, setReopened] = useState(false);

  useEffect(() => {
    const open = () => setReopened(true);
    window.addEventListener(OPEN_INDUSTRY_PICKER, open);
    return () => window.removeEventListener(OPEN_INDUSTRY_PICKER, open);
  }, []);

  // Fail closed: if the industry cannot be loaded, say so instead of letting the app run as agriculture (R1-31).
  if (isError) {
    return (
      <div className="fixed inset-0 z-[10000] flex items-center justify-center bg-[#0B0908] px-6 text-center text-[#F6F1EB]">
        <div className="max-w-sm">
          <p className="text-[15px]">Ingabe could not load your industry.</p>
          <p className="mt-2 text-[13px] text-[#B8A99B]">Check your connection, then try again.</p>
          <button
            type="button"
            onClick={() => refetch()}
            disabled={isFetching}
            className="mt-5 rounded-full bg-[#D9A066] px-5 py-2 text-[14px] font-medium text-[#0B0908] disabled:opacity-60"
          >
            {isFetching ? 'Trying…' : 'Try again'}
          </button>
        </div>
      </div>
    );
  }
  if (!data || !data.can_choose || (data.industry !== null && !reopened)) return null;
  return (
    <Suspense fallback={<div className="fixed inset-0 z-[10000] bg-[#0B0908]" />}>
      <IndustryPicker data={data} onClose={() => setReopened(false)} />
    </Suspense>
  );
}
