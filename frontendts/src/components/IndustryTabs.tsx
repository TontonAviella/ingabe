import { Loader2 } from 'lucide-react';
import { type IndustryKey, useIndustry, useSwitchIndustry } from '@/components/IndustryGate';

// The home's one-tap industry switch: every industry as a tab, the current one lit, each with its count of the
// user's maps when known. Tapping another tab switches at once (saved on the account), without the full-screen
// picker, which is only the first choice (Roger, 2026-10-22: going from Telecom Towers back to Agriculture took a
// full-screen detour).

/** `counts`: maps per industry for the counts in the tabs; an industry missing from it shows no count. */
export function IndustryTabs({ counts }: { counts: Partial<Record<IndustryKey, number>> }) {
  const { data } = useIndustry();
  const { switchTo, pending } = useSwitchIndustry();
  if (!data || !data.can_choose || data.industry === null) return null;

  return (
    <div role="tablist" aria-label="Industry" className="flex flex-wrap justify-center gap-2">
      {data.options.map((option) => {
        const current = option.key === data.industry;
        const busy = pending === option.key;
        const count = counts[option.key];
        return (
          <button
            key={option.key}
            type="button"
            role="tab"
            aria-selected={current}
            disabled={current || pending !== null}
            onClick={() => switchTo(option.key, option.label)}
            className={`inline-flex items-center gap-2 rounded-full border px-4 py-1.5 text-sm transition-colors ${
              current
                ? 'border-[#D9A066] bg-[#D9A066] font-medium text-[#140E0B]'
                : 'cursor-pointer border-gray-600 text-gray-300 hover:border-[#D9A066] hover:text-white disabled:cursor-wait disabled:opacity-60'
            }`}
          >
            {busy && <Loader2 aria-hidden className="h-3.5 w-3.5 animate-spin" />}
            {option.label}
            {count !== undefined && count > 0 && (
              <span className={`text-xs ${current ? 'text-[#140E0B]/70' : 'text-gray-400'}`}>{count}</span>
            )}
          </button>
        );
      })}
    </div>
  );
}
