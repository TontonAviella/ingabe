import type { AdminLevel } from '@/hooks/useRwandaApi';
import { formatWeek } from '@/lib/adminLevels';

type Props = Record<string, unknown> | null | undefined;

/** Hover card for an admin unit: its name and parents, and its district's NDVI,
 * saying when the value is shared by every unit of a finer level. */
export function AdminUnitTooltip({ district, unit, x, y }: { district: Props; unit?: Props; x: number; y: number }) {
  if (!district) return null;
  const ndvi = district.mean_ndvi;
  const unitLevel = unit?.level as AdminLevel | undefined;
  const parents = unit ? [unit.cell, unit.sector].filter(Boolean).join(', ') : '';
  const sharedNote = unitLevel ? (district[`shared_note_${unitLevel}`] as string | undefined) : undefined;
  const week = formatWeek(district.week_start as string | null | undefined);
  return (
    <div
      className="absolute bg-white dark:bg-gray-800 text-gray-900 dark:text-gray-100 px-3 py-2 rounded-md shadow-lg text-xs pointer-events-none z-10 max-w-64"
      style={{ left: x + 10, top: y + 10 }}
    >
      {unit && unitLevel ? (
        <>
          <div className="font-semibold">
            {String(unit.name)} {unitLevel}
          </div>
          <div className="text-gray-600 dark:text-gray-400 mb-1">
            {parents ? `${parents}, ` : ''}
            {String(district.district)} district
          </div>
        </>
      ) : (
        <div className="font-semibold mb-1">{String(district.district)} district</div>
      )}
      {typeof ndvi === 'number' ? (
        <>
          <div>
            Vegetation index (NDVI): <span className="font-semibold">{ndvi.toFixed(2)}</span> — {String(district.ndvi_label)}
          </div>
          {week && <div className="text-gray-600 dark:text-gray-400">Week of {week}</div>}
          {sharedNote && <div className="mt-1 text-gray-600 dark:text-gray-400">{sharedNote}</div>}
        </>
      ) : (
        <div className="text-gray-600 dark:text-gray-400">No vegetation data yet</div>
      )}
    </div>
  );
}
