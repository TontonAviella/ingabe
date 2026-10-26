import { apiFetch } from '@mundi/ee';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';

interface ReportAudience {
  audience: string;
  source: 'user' | 'partner' | 'default';
  options: Array<{ key: string; label: string }>;
}

const KEY = ['user', 'report-audience'];

/** The user's role (src/services/reader_roles.py) in the industry they work in, and saving another. The question
 * panels and the "Answers for" select share it, so a role picked in one is the role in all of them. */
/** The reader's saved role; `enabled` false asks nothing (a panel with nothing to show). */
export function useReaderRole(enabled = true) {
  const queryClient = useQueryClient();
  const query = useQuery<ReportAudience>({
    queryKey: KEY,
    enabled,
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
  return { data: query.data, role: query.data?.source === 'default' ? null : (query.data?.audience ?? null), save };
}

/** "Answers for: Farmer / Cooperative / Agronomist / …" — the user's role in the industry they work in: the
 * questions they are asked and the words Sage's reports use. Saved on the account; the partner's default applies
 * until the user picks one. */
export function ReportAudienceSelect({ label = 'Answers for', compact = false }: { label?: string; compact?: boolean } = {}) {
  const { data, save } = useReaderRole();

  if (!data) return null;
  const hint =
    data.source === 'user' ? 'Your choice' : data.source === 'partner' ? "Your organisation's default" : 'Default — pick who you are';
  return (
    <label
      className={
        compact
          ? 'flex items-center justify-between gap-2 px-2.5 pt-2 text-[12.5px] text-[#B8A99B]'
          : 'flex items-center gap-2 px-3 pb-2 text-xs text-gray-300'
      }
      title={hint}
    >
      <span>{label}</span>
      <select
        className={
          compact
            ? 'rounded-[7px] border border-white/[0.07] bg-[#221813] px-1.5 py-0.5 text-[#F3EDE6] disabled:opacity-60'
            : 'rounded bg-gray-800 px-1.5 py-0.5 text-gray-100 border border-gray-600 disabled:opacity-60'
        }
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
