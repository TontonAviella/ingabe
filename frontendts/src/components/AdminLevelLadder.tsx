import type { AdminLevel, MapLevel } from '@/hooks/useRwandaApi';
import { LEVEL_NAMES } from '@/lib/adminLevels';

/** District › Sector › Cell › Village, with the level on screen underlined and
 * levels without their own values greyed out. */
export function AdminLevelLadder({ levels, current, className = '' }: { levels: MapLevel[]; current: AdminLevel; className?: string }) {
  return (
    <div
      className={`bg-white/95 dark:bg-gray-800/95 text-gray-900 dark:text-gray-100 px-2 py-1 rounded-md shadow text-[11px] flex items-center gap-1 ${className}`}
      aria-label={`Showing ${LEVEL_NAMES[current].toLowerCase()} outlines`}
    >
      {levels.map((l, i) => (
        <span key={l.level} className="flex items-center gap-1">
          {i > 0 && (
            <span aria-hidden className="text-gray-400">
              ›
            </span>
          )}
          <span
            className={
              l.level === current ? 'font-semibold underline underline-offset-2' : l.has_values ? '' : 'text-gray-400 dark:text-gray-500'
            }
            title={l.has_values ? `${LEVEL_NAMES[l.level]} values` : `${LEVEL_NAMES[l.level]}s show their ${l.values_from} value`}
          >
            {LEVEL_NAMES[l.level]}
          </span>
        </span>
      ))}
    </div>
  );
}
