import { apiFetch } from '@mundi/ee';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';

interface ReportAudience {
  audience: string;
  source: 'user' | 'partner' | 'default';
  options: Array<{ key: string; label: string }>;
}

const KEY = ['user', 'report-audience'];

/** "Answers for: Farmer / Insurer / Agronomist / Scientist" — the view Sage's
 * reports use for this user. Saved on the account; the partner's default
 * applies until the user picks one. */
export function ReportAudienceSelect() {
  const queryClient = useQueryClient();
  const { data } = useQuery<ReportAudience>({
    queryKey: KEY,
    queryFn: async () => {
      const res = await apiFetch('/api/user/report-audience');
      if (!res.ok) throw new Error('Failed to load report audience');
      return res.json();
    },
    staleTime: 5 * 60 * 1000,
  });
  const save = useMutation({
    mutationFn: async (audience: string) => {
      const res = await apiFetch('/api/user/report-audience', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ audience }),
      });
      if (!res.ok) throw new Error((await res.json().catch(() => null))?.detail ?? 'Could not save');
      return (await res.json()) as ReportAudience;
    },
    onSuccess: (saved) => queryClient.setQueryData(KEY, saved),
  });

  if (!data) return null;
  const hint =
    data.source === 'user' ? 'Your choice' : data.source === 'partner' ? "Your organisation's default" : 'Default — pick who you are';
  return (
    <label className="flex items-center gap-2 px-3 pb-2 text-xs text-gray-300" title={hint}>
      <span>Answers for</span>
      <select
        className="rounded bg-gray-800 px-1.5 py-0.5 text-gray-100 border border-gray-600 disabled:opacity-60"
        value={data.audience}
        disabled={save.isPending}
        onChange={(e) => save.mutate(e.target.value)}
        aria-label="Who Sage's reports are written for"
      >
        {data.options.map((o) => (
          <option key={o.key} value={o.key}>
            {o.label}
          </option>
        ))}
      </select>
      {save.isError && <span className="text-red-400">{(save.error as Error).message}</span>}
    </label>
  );
}
