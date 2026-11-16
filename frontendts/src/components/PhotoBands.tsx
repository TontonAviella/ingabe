import { useState } from 'react';
import { type PhotoBands, useSayPhotoBands } from '@/hooks/usePhotoBands';

const UNITS = [
  { id: '', label: 'I do not know the unit' },
  { id: 'celsius', label: 'Degrees Celsius' },
  { id: 'kelvin', label: 'Kelvin' },
];

/** For a photo whose file does not say which band is which. A farmer or an insurer is not asked about bands: they
 * are told, in plain words, that the photo cannot be measured yet and who can settle it. The question itself is one
 * step away, for the person who flew or processed the photo: one tap on the camera's usual order, or each band
 * named by hand. Shown in place of the question cards, which cannot read such a photo. */
export function PhotoBandsForm({ layerId, bands }: { layerId: string; bands: PhotoBands }) {
  const say = useSayPhotoBands(layerId);
  // Nothing is chosen for the reader: a wrong order gives numbers that look right, so the choice must be theirs.
  const [order, setOrder] = useState<string[] | null>(null);
  const [byHand, setByHand] = useState(bands.presets.length === 0);
  const hand = order ?? Array<string>(bands.band_count).fill('other');
  const [unit, setUnit] = useState('');
  const working = bands.working || say.isPending;
  // closed until someone says they know the file; it stays open after a failed save, so the error is seen
  const [mine, setMine] = useState(false);

  if (working) {
    return (
      <p className="m-0 py-6 text-[15px] text-fg-soft">
        Ingabe is writing the band names into its copy of the photo. This takes a few minutes for a large photo; the questions appear when
        it is done.
      </p>
    );
  }
  if (!mine && !say.error) {
    return (
      <div className="flex flex-col gap-3 py-2">
        <h3 className="m-0 text-[20px] font-bold leading-tight text-fg">This photo cannot be measured yet</h3>
        <p className="m-0 text-[14px] leading-snug text-fg-soft">
          It comes from a camera that sees more than the eye, and its file is missing one detail from whoever made it. You do not need to
          know anything about it: the person who flew or processed the photo can add that detail here in a minute. Until then Ingabe shows
          the picture and measures nothing from it, rather than guess.
        </p>
        {bands.failed && (
          <p role="alert" className="m-0 text-[13px] text-caramel-hi">
            The last try to add it did not work.
          </p>
        )}
        <button
          type="button"
          className="self-start cursor-pointer rounded-full border border-white/15 px-4 py-2 text-[14px] font-semibold text-fg"
          onClick={() => setMine(true)}
        >
          I flew or processed this photo
        </button>
      </div>
    );
  }
  return (
    <div className="flex flex-col gap-3 py-2">
      <h3 className="m-0 text-[20px] font-bold leading-tight text-fg">Which band is which?</h3>
      <p className="m-0 text-[14px] leading-snug text-fg-soft">
        This photo has {bands.band_count} band{bands.band_count === 1 ? '' : 's'} and its file does not say what each one is. The order
        depends on the camera, so Ingabe will not guess. Tell it once and the photo can be measured.
      </p>
      {bands.presets.map((preset) => (
        <label key={preset.label} className="flex cursor-pointer items-start gap-2 text-[14px] leading-snug text-fg">
          <input
            type="radio"
            name="band-order"
            className="mt-1"
            checked={!byHand && preset.order.join() === (order ?? []).join()}
            onChange={() => {
              setByHand(false);
              setOrder(preset.order);
            }}
          />
          <span>{preset.label}</span>
        </label>
      ))}
      {bands.presets.length > 0 && (
        <label className="flex cursor-pointer items-start gap-2 text-[14px] leading-snug text-fg">
          <input
            type="radio"
            name="band-order"
            className="mt-1"
            checked={byHand}
            onChange={() => {
              setByHand(true);
              setOrder(hand);
            }}
          />
          <span>Another order</span>
        </label>
      )}
      {byHand &&
        hand.map((role, index) => (
          <label key={index} className="flex items-center justify-between gap-3 text-[14px] text-fg">
            <span>Band {index + 1}</span>
            <select
              className="rounded-md border border-white/15 bg-transparent px-2 py-1 text-[14px] text-fg"
              value={role}
              onChange={(event) => setOrder(hand.map((old, k) => (k === index ? event.target.value : old)))}
            >
              {bands.choices.map((choice) => (
                <option key={choice.id} value={choice.id}>
                  {choice.label}
                </option>
              ))}
            </select>
          </label>
        ))}
      {order?.includes('thermal') && (
        <label className="flex items-center justify-between gap-3 text-[14px] text-fg">
          <span>The thermal band is in</span>
          <select
            className="rounded-md border border-white/15 bg-transparent px-2 py-1 text-[14px] text-fg"
            value={unit}
            onChange={(event) => setUnit(event.target.value)}
          >
            {UNITS.map((choice) => (
              <option key={choice.id} value={choice.id}>
                {choice.label}
              </option>
            ))}
          </select>
        </label>
      )}
      <p className="m-0 text-[13px] leading-snug text-fg-muted">
        If you are not sure, ask whoever flew or processed the photo: the same camera comes out in one order from one program and another
        from the next. A wrong order gives wrong numbers that look right. You can change it later; the photo as you uploaded it is kept.
      </p>
      <button
        type="button"
        className="self-start rounded-full bg-caramel px-4 py-2 text-[14px] font-semibold text-bg cursor-pointer disabled:cursor-default disabled:opacity-40"
        disabled={order === null}
        onClick={() => order && say.mutate({ order, thermal_unit: order.includes('thermal') && unit ? unit : null })}
      >
        Save the band names
      </button>
      {(say.error || bands.failed) && (
        <p role="alert" className="m-0 text-[13px] text-caramel-hi">
          {say.error?.message ?? 'The band names could not be written. Try again.'}
        </p>
      )}
    </div>
  );
}
