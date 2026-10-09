import { apiFetch } from '@mundi/ee';
import { useQuery } from '@tanstack/react-query';
import { lazy, Suspense, useEffect, useState } from 'react';

// Ingabe serves three industries. The first time someone signs in, before anything else, they pick theirs from
// three miniature worlds (the same dioramas as nozalabs.rw), so "grid" or "towers" is never a guess. The choice is
// saved on the account (/api/user/industry) and can be changed from the sidebar ("Your industry").

export type IndustryKey = 'agriculture' | 'power_grid' | 'telecom';

export interface IndustryState {
  industry: IndustryKey | null;
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

/** Asks a signed-in user which industry they work in, once, before anything else; also reopened from the sidebar. */
export function IndustryGate() {
  const { data } = useIndustry();
  const [reopened, setReopened] = useState(false);

  useEffect(() => {
    const open = () => setReopened(true);
    window.addEventListener(OPEN_INDUSTRY_PICKER, open);
    return () => window.removeEventListener(OPEN_INDUSTRY_PICKER, open);
  }, []);

  if (!data || (data.industry !== null && !reopened)) return null;
  return (
    <Suspense fallback={<div className="fixed inset-0 z-[10000] bg-[#0B0908]" />}>
      <IndustryPicker data={data} onClose={() => setReopened(false)} />
    </Suspense>
  );
}
