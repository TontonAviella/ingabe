import { afterEach, describe, expect, it, vi } from 'vitest';
import { stopWatchingGraphics, watchGraphics } from './mapHealth';

describe('watchGraphics', () => {
  afterEach(() => vi.unstubAllGlobals());

  it('ignores the context loss of a map the app removes itself, and reports one it did not cause', () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response(null, { status: 204 }));
    vi.stubGlobal('fetch', fetchMock);

    // Leaving a map page: map.remove() loses the context on purpose, after the watch is taken off.
    const left = document.createElement('canvas');
    watchGraphics(left);
    stopWatchingGraphics(left);
    left.dispatchEvent(new Event('webglcontextlost'));
    expect(fetchMock).not.toHaveBeenCalled();

    // A map whose graphics are lost while it is shown (a GPU reset): reported.
    const shown = document.createElement('canvas');
    watchGraphics(shown);
    shown.dispatchEvent(new Event('webglcontextlost'));
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(JSON.parse(fetchMock.mock.calls[0][1].body)).toEqual({ event: 'webgl_lost' });
  });
});
