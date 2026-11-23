import { describe, expect, it } from 'vitest';
import { drawnAtZoom } from './tappedFeature';

const hexagon = (size: number, minzoom?: number, maxzoom?: number) => ({ size, layer: { minzoom, maxzoom } });

describe('drawnAtZoom', () => {
  // the plants layer of the satellite view: three hexagon sizes, one drawn at a time
  const wide = hexagon(10, undefined, 15);
  const middle = hexagon(11, 15, 16.5);
  const small = hexagon(12, 16.5);

  it('keeps only the hexagon size the map is drawing', () => {
    // at 16.2 the zoom-16 tile already holds the small hexagons, and the map answers a tap with them first
    expect(drawnAtZoom([small, middle], 16.2)).toEqual([middle]);
    expect(drawnAtZoom([small, middle], 16.5)).toEqual([small]);
    expect(drawnAtZoom([middle, wide], 14.9)).toEqual([wide]);
    expect(drawnAtZoom([middle, wide], 15)).toEqual([middle]);
  });

  it('keeps layers with no zoom range and the order the map gave', () => {
    const always = hexagon(0);
    expect(drawnAtZoom([always, small, middle, wide], 3)).toEqual([always, wide]);
    expect(drawnAtZoom([middle, always], 16)).toEqual([middle, always]);
  });
});
