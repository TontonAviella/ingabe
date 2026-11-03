import { useState } from 'react';
import { useSavePlantingDate, useSavePlantsSeen } from '@/hooks/useTowers';
import type { CountCheck, FieldFacts, TowerPart } from '@/lib/towerContract';

// "Can you see each plant on its own here?" for a farm part on one photo. The person's answer decides whether the
// plants can be counted there (answer_levels: on 82 judged drone squares no pixel measure matched people's eyes);
// the planting date only words a hint. Answers are kept with who gave them, as examples to learn the question from.

const EYEBROW = 'text-[11px] font-bold uppercase tracking-[0.12em] text-[#D9A066]';
const CHOICE =
  'flex-1 rounded-[10px] border px-3 py-2 text-[14px] font-semibold cursor-pointer focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[#F3EDE6]';

const VERDICT: Record<CountCheck['countable'], string> = {
  yes: 'Counting allowed here: you saw each plant on its own.',
  no: 'Not counted here: you saw the plants touching. The area can still be mapped.',
  maybe: 'Not counted until someone answers.',
};

export function CountCheckCard({
  layerId,
  sha256,
  part,
  check,
  field,
}: {
  layerId: string;
  sha256: string;
  part: TowerPart;
  check: CountCheck;
  field: FieldFacts | undefined;
}) {
  const saveSeen = useSavePlantsSeen(layerId, sha256);
  const saveDate = useSavePlantingDate(layerId);
  const [dateDraft, setDateDraft] = useState<string | null>(null);
  const apart = check.seen?.apart;
  const answer = (value: boolean) => saveSeen.mutate({ part, apart: apart === value ? null : value });
  const error = saveSeen.error?.message ?? saveDate.error?.message ?? null;
  const shownDate = dateDraft ?? field?.planting_date ?? '';

  return (
    <section
      aria-label="Can the plants be counted here?"
      className="flex flex-col gap-2.5 rounded-[14px] border border-white/10 bg-white/[0.04] p-3"
    >
      <span className={EYEBROW}>Can we count here?</span>
      <p className="m-0 text-[15px] font-semibold text-[#F3EDE6]">{check.question}</p>
      {check.hint && <p className="m-0 text-[13px] text-[#B8A99B]">The dates say: {check.hint}. Your eye decides.</p>}
      <div className="flex gap-2" role="group" aria-label={check.question}>
        <button
          type="button"
          aria-pressed={apart === true}
          disabled={saveSeen.isPending}
          onClick={() => answer(true)}
          className={`${CHOICE} ${apart === true ? 'border-[#7CC287] bg-[#7CC287] text-[#0B0908]' : 'border-white/15 bg-transparent text-[#F3EDE6]'}`}
        >
          Yes, each on its own
        </button>
        <button
          type="button"
          aria-pressed={apart === false}
          disabled={saveSeen.isPending}
          onClick={() => answer(false)}
          className={`${CHOICE} ${apart === false ? 'border-[#EC7A6C] bg-[#EC7A6C] text-[#0B0908]' : 'border-white/15 bg-transparent text-[#F3EDE6]'}`}
        >
          No, plants touch
        </button>
      </div>
      <p className="m-0 text-[13px] text-[#D8CCBF]">{VERDICT[check.countable]}</p>
      {field && (
        <label className="flex flex-wrap items-center gap-2 text-[13px] text-[#B8A99B]">
          <span>Planting date of this field</span>
          <input
            id="planting-date"
            type="date"
            value={shownDate}
            max={new Date().toISOString().slice(0, 10)}
            onChange={(e) => setDateDraft(e.target.value)}
            onBlur={() => {
              if (dateDraft === null || dateDraft === (field.planting_date ?? '')) return;
              saveDate.mutate({ planting_date: dateDraft || null }, { onSettled: () => setDateDraft(null) });
            }}
            className="rounded-[8px] border border-white/15 bg-transparent px-2 py-1 text-[#F3EDE6]"
          />
          {saveDate.isPending && <span>Saving…</span>}
        </label>
      )}
      {error && (
        <p role="alert" className="m-0 text-[13px] text-[#E9B987]">
          {error}
        </p>
      )}
    </section>
  );
}
