import { apiFetch } from '@mundi/ee';
import { useQuery } from '@tanstack/react-query';
import { lazy, Suspense, useEffect, useState } from 'react';

// Ingabe serves three industries. The first time someone signs in, before anything else, they pick theirs from
// three miniature worlds (the same dioramas as nozalabs.rw), so "grid" or "towers" is never a guess. The choice is
// saved on the account (/api/user/industry) and can be changed from the sidebar ("Your industry"). Inside a company,
// its owners and admins choose for everyone; members of a company that has chosen are never asked.

export type IndustryKey = 'agriculture' | 'power_grid' | 'telecom';

export interface IndustryState {
  industry: IndustryKey | null; // what new projects get: the company's industry once it has one, else the user's
  source: 'company' | 'you' | null;
  // The company the user acts for. Its owners and admins choose its industry; other members follow it.
  company: { name: string; industry: IndustryKey | null; can_set: boolean } | null;
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
