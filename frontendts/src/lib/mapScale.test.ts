import { describe, expect, it, vi } from 'vitest';
import { addScale } from '@/lib/mapScale';

// Found 2026-11-02 on the Cyampirita map with 3D hills on: the scale read "NaN m" for good. The map jumps to the
// photo with the hills switched on but not yet drawn; both ends of the scale's 100 px then read the same stale point
// (0 m, which MapLibre rounds to NaN), and MapLibre measures again only when the map moves. It must measure again
// once the map has settled.

type Handler = () => void;

function fakeMap() {
  const handlers: Record<string, Handler[]> = {};
  const controls = new Set<unknown>();
  return {
    handlers,
    on: vi.fn((event: string, handler: Handler) => {
      (handlers[event] ??= []).push(handler);
    }),
    addControl: vi.fn((control: unknown) => controls.add(control)),
    hasControl: vi.fn((control: unknown) => controls.has(control)),
    removeControl: (control: unknown) => controls.delete(control),
    fire: (event: string) => {
      for (const handler of handlers[event] ?? []) handler();
    },
  };
}

describe('the map scale', () => {
  it('sits bottom-left and measures again whenever the map settles', () => {
    const map = fakeMap();
    const scale = addScale(map as never);
    expect(map.addControl).toHaveBeenCalledWith(scale, 'bottom-left');
    const remeasure = vi.spyOn(scale, 'setUnit').mockImplementation(() => undefined);

    map.fire('idle');
    expect(remeasure).toHaveBeenCalledTimes(1);
    map.fire('idle');
    expect(remeasure).toHaveBeenCalledTimes(2);
  });

  it('does nothing once the scale is gone from the map', () => {
    const map = fakeMap();
    const scale = addScale(map as never);
    const remeasure = vi.spyOn(scale, 'setUnit').mockImplementation(() => undefined);
    map.removeControl(scale);
    map.fire('idle');
    expect(remeasure).not.toHaveBeenCalled();
  });
});
