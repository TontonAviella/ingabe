import { useState } from 'react';
import { type FieldFlight, useSayFlownOn } from '@/hooks/useFieldTaps';

const day = 'min-h-11 rounded-[12px] border border-white/10 bg-panel-2 px-3 text-[13px] text-fg sm:min-h-9';
const save = 'inline-flex min-h-11 cursor-pointer items-center px-2 text-[12.5px] text-fg-soft underline disabled:opacity-50 sm:min-h-9';

/** One field on every flight of its place, oldest first, in the server's own words; and the day this photo was
 * flown, which only the reader knows. Nothing here says better or worse: the flights stand side by side. */
export function FieldFlights({
  layerId,
  flights,
  flownOn,
  note,
}: {
  layerId: string;
  flights: FieldFlight[];
  flownOn: string | null;
  note?: string;
}) {
  const say = useSayFlownOn(layerId);
  const [given, setGiven] = useState<string | null>(null);
  const value = given ?? flownOn ?? '';
  const today = new Date().toISOString().slice(0, 10);
  return (
    <div className="flex flex-col gap-1 border-white/10 border-t pt-2">
      {flights.length > 0 && (
        <>
          <span className="font-semibold text-[12.5px]" title={note}>
            This field on each flight
          </span>
          <ul className="m-0 flex list-none flex-col gap-0.5 p-0">
            {flights.map((flight) => (
              <li
                key={`${flight.day}|${flight.photo}|${flight.this}`}
                className={`text-[12.5px] leading-snug ${flight.this ? 'text-fg' : 'text-fg-soft'}`}
                title={flight.photo}
              >
                <span className={flight.this ? 'font-semibold' : ''}>
                  {flight.day_said}
                  {flight.this ? ' (this photo)' : ''}
                </span>
                {`: ${flight.said}`}
              </li>
            ))}
          </ul>
          {note && <span className="text-[12px] text-fg-muted">{note}</span>}
        </>
      )}
      <form
        className="flex flex-wrap items-center gap-2"
        onSubmit={(e) => {
          e.preventDefault();
          if (!say.isPending) say.mutate(value || null, { onSuccess: () => setGiven(null) });
        }}
      >
        <label className="flex flex-wrap items-center gap-2 text-[12.5px] text-fg-soft">
          Day this photo was flown
          <input type="date" className={day} max={today} value={value} onChange={(e) => setGiven(e.target.value)} />
        </label>
        {given !== null && given !== (flownOn ?? '') && (
          <button type="submit" className={save} disabled={say.isPending}>
            Save
          </button>
        )}
        {!flownOn && given === null && <span className="text-[12px] text-fg-muted">Not given yet: it goes by the day it was added.</span>}
        {say.error && (
          <span role="alert" className="basis-full text-[12px] text-caramel-hi">
            {say.error.message}
          </span>
        )}
      </form>
    </div>
  );
}
