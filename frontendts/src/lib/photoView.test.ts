import { describe, expect, it } from 'vitest';
import {
  type Box,
  boxFromCorners,
  boxToScreen,
  centreOn,
  dragBox,
  fitView,
  hasSize,
  isDrag,
  pinchView,
  sameShape,
  toPhoto,
  toScreen,
  type View,
  zoomAt,
  zoomLimits,
} from './photoView';

// A DJI tower photo (5472 x 3648) shown in a 1200 x 800 viewport.
const W = 5472;
const H = 3648;

function close(a: number[], b: number[], digits = 6) {
  expect(a.length).toBe(b.length);
  a.forEach((v, i) => {
    expect(v).toBeCloseTo(b[i], digits);
  });
}

describe('screen <-> original photo pixels', () => {
  it('fits the photo in the middle of the viewport', () => {
    const view = fitView(W, H, 1200, 900);
    expect(view.scale).toBeCloseTo(1200 / W);
    expect(view.x).toBeCloseTo(0);
    expect(view.y).toBeCloseTo((900 - H * (1200 / W)) / 2);
    close(toScreen(view, [0, 0]), [0, view.y]);
    close(toScreen(view, [W, H]), [1200, 900 - view.y]);
  });

  it('round-trips points at any zoom and pan', () => {
    const views: View[] = [fitView(W, H, 1200, 800), { scale: 2.5, x: -3000, y: -1200 }, { scale: 0.05, x: 400, y: 13 }];
    for (const view of views) {
      for (const p of [
        [0, 0],
        [W, H],
        [1234.5, 987.25],
      ] as [number, number][]) {
        close(toPhoto(view, toScreen(view, p)), p);
      }
    }
  });

  it('turns a screen point into the original pixel under it', () => {
    const view: View = { scale: 0.25, x: 100, y: 50 };
    close(toPhoto(view, [100, 50]), [0, 0]);
    close(toPhoto(view, [600, 300]), [2000, 1000]);
  });

  it('draws a photo box where it is on the screen', () => {
    expect(boxToScreen({ scale: 0.5, x: 10, y: 20 }, [100, 200, 300, 260])).toEqual({ left: 60, top: 120, width: 100, height: 30 });
  });

  it('keeps the photo point under the pointer when zooming', () => {
    const view = fitView(W, H, 1200, 800);
    const limits = zoomLimits(view);
    const at: [number, number] = [700, 333];
    const before = toPhoto(view, at);
    const zoomed = zoomAt(view, 3, at, limits);
    expect(zoomed.scale).toBeCloseTo(view.scale * 3);
    close(toPhoto(zoomed, at), before);
  });

  it('stops zooming at the limits', () => {
    const view = fitView(W, H, 1200, 800);
    const limits = zoomLimits(view);
    expect(zoomAt(view, 1000, [0, 0], limits).scale).toBe(limits.max);
    expect(zoomAt(view, 0.001, [0, 0], limits).scale).toBe(limits.min);
    expect(limits.max).toBe(4);
    expect(zoomLimits(fitView(640, 480, 1200, 900)).max).toBeCloseTo((1200 / 640) * 4);
  });

  it('follows two fingers: spread zooms, the midpoint carries the photo', () => {
    const start = fitView(W, H, 1200, 800);
    const limits = zoomLimits(start);
    const photoUnderMid = toPhoto(start, [500, 400]);
    const view = pinchView(start, [400, 400], [600, 400], [300, 500], [700, 500], limits);
    expect(view.scale).toBeCloseTo(start.scale * 2);
    close(toPhoto(view, [500, 500]), photoUnderMid);
  });

  it('centres a box, zooming out only when it does not fit', () => {
    const view: View = { scale: 2, x: 0, y: 0 };
    const limits = { min: 0.1, max: 4 };
    const small: Box = [1000, 1000, 1100, 1050];
    const centred = centreOn(view, small, 1200, 800, limits);
    expect(centred.scale).toBe(2);
    close(toScreen(centred, [1050, 1025]), [600, 400]);
    const big: Box = [0, 0, 3000, 1000];
    expect(centreOn(view, big, 1200, 800, limits).scale).toBeCloseTo((1200 * 0.8) / 3000);
  });
});

describe('boxes', () => {
  it('normalises a drag in any direction and keeps it inside the photo', () => {
    expect(boxFromCorners([300, 400], [100, 50], W, H)).toEqual([100, 50, 300, 400]);
    expect(boxFromCorners([-20, 3000], [200, 9999], W, H)).toEqual([0, 3000, 200, H]);
  });

  it('resizes by a corner and flips instead of turning inside out', () => {
    const box: Box = [100, 100, 200, 200];
    expect(dragBox(box, 'se', [200, 200], [250, 260], W, H)).toEqual([100, 100, 250, 260]);
    expect(dragBox(box, 'nw', [100, 100], [90, 80], W, H)).toEqual([90, 80, 200, 200]);
    expect(dragBox(box, 'ne', [200, 100], [210, 120], W, H)).toEqual([100, 120, 210, 200]);
    expect(dragBox(box, 'sw', [100, 200], [50, 230], W, H)).toEqual([50, 100, 200, 230]);
    expect(dragBox(box, 'se', [200, 200], [50, 40], W, H)).toEqual([50, 40, 100, 100]);
  });

  it('moves a whole box without leaving the photo', () => {
    const box: Box = [100, 100, 200, 200];
    expect(dragBox(box, 'move', [150, 150], [160, 140], W, H)).toEqual([110, 90, 210, 190]);
    expect(dragBox(box, 'move', [150, 150], [-500, 9999], W, H)).toEqual([0, H - 100, 100, H]);
  });

  it('refuses a box squeezed flat against the edge of the photo', () => {
    expect(hasSize(boxFromCorners([-50, 10], [-10, 90], W, H))).toBe(false);
    expect(hasSize([10, 10, 11, 11])).toBe(true);
  });

  it('tells a tap from a drag on screen', () => {
    expect(isDrag([10, 10], [14, 40])).toBe(false);
    expect(isDrag([10, 10], [16, 16])).toBe(true);
  });
});

describe('the photo the browser shows', () => {
  it('matches the server only with the same shape', () => {
    expect(sameShape(5472, 3648, W, H)).toBe(true);
    expect(sameShape(1368, 912, W, H)).toBe(true); // a smaller copy: boxes scale with it
    expect(sameShape(3648, 5472, W, H)).toBe(false); // turned by its EXIF orientation
    expect(sameShape(0, 0, W, H)).toBe(false);
  });
});
