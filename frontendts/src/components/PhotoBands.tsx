import { useQueryClient } from '@tanstack/react-query';
import { useEffect, useState } from 'react';
import { type PhotoBands, useSayPhotoBands } from '@/hooks/usePhotoBands';

const UNITS = [
  { id: '', label: 'I do not know the unit' },
  { id: 'celsius', label: 'Degrees Celsius' },
  { id: 'kelvin', label: 'Kelvin' },
];

/** The reader says which band is which, for a photo whose file does not: one tap on the camera's usual order, or
 * each band named by hand. Shown in place of the question cards, which cannot read such a photo. */
export function PhotoBandsForm({ layerId, bands }: { layerId: string; bands: PhotoBands }) {
  const queryClient = useQueryClient();
  const say = useSayPhotoBands(layerId);
  const [order, setOrder] = useState<string[]>(() => bands.presets[0]?.order ?? Array(bands.band_count).fill('other'));
  const [byHand, setByHand] = useState(bands.presets.length === 0);
  const [unit, setUnit] = useState('');
  const working = bands.working || say.isPending;

  // Once the names are written the photo no longer needs them: ask the cards again.
  useEffect(() => {
    if (!bands.needs_names) queryClient.invalidateQueries({ queryKey: ['drone-cards', layerId] });
  }, [bands.needs_names, layerId, queryClient]);

  if (working) {
    return (
      <p className="m-0 py-6 text-[15px] text-fg-soft">
        Ingabe is writing the band names into its copy of the photo. This takes a few minutes for a large photo; the questions appear when
        it is done.
      </p>
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
            checked={!byHand && preset.order.join() === order.join()}
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
          <input type="radio" name="band-order" className="mt-1" checked={byHand} onChange={() => setByHand(true)} />
          <span>Another order</span>
        </label>
      )}
      {byHand &&
        order.map((role, index) => (
          <label key={index} className="flex items-center justify-between gap-3 text-[14px] text-fg">
            <span>Band {index + 1}</span>
            <select
              className="rounded-md border border-white/15 bg-transparent px-2 py-1 text-[14px] text-fg"
              value={role}
              onChange={(event) => setOrder(order.map((old, k) => (k === index ? event.target.value : old)))}
            >
              {bands.choices.map((choice) => (
                <option key={choice.id} value={choice.id}>
                  {choice.label}
                </option>
              ))}
            </select>
          </label>
        ))}
      {order.includes('thermal') && (
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
        If you are not sure, ask whoever flew or processed the photo. A wrong order gives wrong numbers that look right.
      </p>
      <button
        type="button"
        className="self-start rounded-full bg-caramel px-4 py-2 text-[14px] font-semibold text-bg cursor-pointer"
        onClick={() => say.mutate({ order, thermal_unit: order.includes('thermal') && unit ? unit : null })}
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
