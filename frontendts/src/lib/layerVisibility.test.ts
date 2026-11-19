import { describe, expect, it } from 'vitest';
import { hideInStyle, isHiddenSource } from './layerVisibility';

describe('isHiddenSource', () => {
  it('matches the layer id alone or after a prefix', () => {
    expect(isHiddenSource('Labc123', ['Labc123'])).toBe(true);
    expect(isHiddenSource('raster-source-Labc123', ['Labc123'])).toBe(true);
    expect(isHiddenSource('cog-source-Labc123', ['Lother', 'Labc123'])).toBe(true);
  });

  it('does not match another layer, a basemap or a layer without a source', () => {
    expect(isHiddenSource('Labc1234', ['Labc123'])).toBe(false);
    expect(isHiddenSource('satellite', ['Labc123'])).toBe(false);
    expect(isHiddenSource(undefined, ['Labc123'])).toBe(false);
    expect(isHiddenSource('Labc123', [])).toBe(false);
  });
});

describe('hideInStyle', () => {
  it('hides every drawn layer of a hidden layer and leaves the others alone', () => {
    const style = {
      layers: [
        { id: 'basemap', source: 'satellite' },
        { id: 'photo', source: 'raster-source-Lphoto', layout: { visibility: 'visible' } },
        { id: 'hex-fill', source: 'Lhex', layout: { 'line-cap': 'round' } },
        { id: 'hex-line', source: 'Lhex' },
        { id: 'background' },
      ],
    };
    hideInStyle(style, ['Lhex']);
    expect(style.layers[0].layout).toBeUndefined();
    expect(style.layers[1].layout).toEqual({ visibility: 'visible' });
    expect(style.layers[2].layout).toEqual({ 'line-cap': 'round', visibility: 'none' });
    expect(style.layers[3].layout).toEqual({ visibility: 'none' });
    expect(style.layers[4].layout).toBeUndefined();
  });
});
