// Screen <-> photo coordinates for marking boxes on one photo that can be zoomed and panned.
//
// Every box the app sends or receives is [x0, y0, x1, y1] in the ORIGINAL photo's pixels (the size the server
// gives as width x height), x to the right and y down from the top-left corner. The screen only ever changes
// the view: screen = photo * scale + (x, y), in CSS pixels from the top-left of the viewport element.

export type Point = [number, number];
export type Box = [number, number, number, number];

export interface View {
  scale: number;
  x: number;
  y: number;
}

export interface ZoomLimits {
  min: number;
  max: number;
}

/** The corner a box is dragged by to resize it, or 'move' to drag the whole box. */
export type Handle = 'nw' | 'ne' | 'sw' | 'se' | 'move';

/** A drag shorter than this on screen, either way, is a tap, not a box. */
export const MIN_DRAG_PX = 6;

/** Photo pixels under a point of the screen. */
export function toPhoto(view: View, [sx, sy]: Point): Point {
  return [(sx - view.x) / view.scale, (sy - view.y) / view.scale];
}

/** Where a photo pixel is on the screen. */
export function toScreen(view: View, [px, py]: Point): Point {
  return [px * view.scale + view.x, py * view.scale + view.y];
}

/** A box of the photo as a rectangle on the screen. */
export function boxToScreen(view: View, [x0, y0, x1, y1]: Box): { left: number; top: number; width: number; height: number } {
  const [left, top] = toScreen(view, [x0, y0]);
  return { left, top, width: (x1 - x0) * view.scale, height: (y1 - y0) * view.scale };
}

/** The whole photo in the middle of a viewport of viewW x viewH, as large as fits. */
export function fitView(photoW: number, photoH: number, viewW: number, viewH: number): View {
  if (!(photoW > 0 && photoH > 0 && viewW > 0 && viewH > 0)) throw new Error('fitView needs a photo and a viewport with a size');
  const scale = Math.min(viewW / photoW, viewH / photoH);
  return { scale, x: (viewW - photoW * scale) / 2, y: (viewH - photoH * scale) / 2 };
}

/** From half the fitted size in to 4 screen pixels per photo pixel (at least 4 times the fitted size). */
export function zoomLimits(fit: View): ZoomLimits {
  return { min: fit.scale / 2, max: Math.max(4, fit.scale * 4) };
}

function clamp(value: number, low: number, high: number): number {
  return Math.min(high, Math.max(low, value));
}

/** The view zoomed by `factor` around a point of the screen, which stays under the pointer. */
export function zoomAt(view: View, factor: number, at: Point, limits: ZoomLimits): View {
  const scale = clamp(view.scale * factor, limits.min, limits.max);
  const [px, py] = toPhoto(view, at);
  return { scale, x: at[0] - px * scale, y: at[1] - py * scale };
}

/** Two fingers: the zoom follows their spread, and the photo point under their first midpoint follows the midpoint. */
export function pinchView(start: View, startA: Point, startB: Point, nowA: Point, nowB: Point, limits: ZoomLimits): View {
  const spread = (a: Point, b: Point) => Math.hypot(a[0] - b[0], a[1] - b[1]);
  const mid = (a: Point, b: Point): Point => [(a[0] + b[0]) / 2, (a[1] + b[1]) / 2];
  const startSpread = spread(startA, startB);
  const factor = startSpread > 0 ? spread(nowA, nowB) / startSpread : 1;
  const scale = clamp(start.scale * factor, limits.min, limits.max);
  const [px, py] = toPhoto(start, mid(startA, startB));
  const [mx, my] = mid(nowA, nowB);
  return { scale, x: mx - px * scale, y: my - py * scale };
}

/** The view with the box in the middle of the viewport, zoomed out only as far as needed for it to fit. */
export function centreOn(view: View, [x0, y0, x1, y1]: Box, viewW: number, viewH: number, limits: ZoomLimits): View {
  const fits = Math.min((viewW * 0.8) / Math.max(x1 - x0, 1), (viewH * 0.8) / Math.max(y1 - y0, 1));
  const scale = clamp(Math.min(view.scale, fits), limits.min, limits.max);
  const cx = (x0 + x1) / 2;
  const cy = (y0 + y1) / 2;
  return { scale, x: viewW / 2 - cx * scale, y: viewH / 2 - cy * scale };
}

/** A point kept inside the photo. */
export function clampPoint([px, py]: Point, photoW: number, photoH: number): Point {
  return [clamp(px, 0, photoW), clamp(py, 0, photoH)];
}

/** The box with two opposite corners at a and b (either order), kept inside the photo. */
export function boxFromCorners(a: Point, b: Point, photoW: number, photoH: number): Box {
  const [ax, ay] = clampPoint(a, photoW, photoH);
  const [bx, by] = clampPoint(b, photoW, photoH);
  return [Math.min(ax, bx), Math.min(ay, by), Math.max(ax, bx), Math.max(ay, by)];
}

/** The box after dragging one of its handles from `from` to `to` (photo pixels), kept inside the photo. A corner
 * dragged past the opposite one flips the box rather than turning it inside out. */
export function dragBox(box: Box, handle: Handle, from: Point, to: Point, photoW: number, photoH: number): Box {
  const [x0, y0, x1, y1] = box;
  if (handle === 'move') {
    const dx = clamp(to[0] - from[0], -x0, photoW - x1);
    const dy = clamp(to[1] - from[1], -y0, photoH - y1);
    return [x0 + dx, y0 + dy, x1 + dx, y1 + dy];
  }
  const fixed: Point = { nw: [x1, y1], ne: [x0, y1], sw: [x1, y0], se: [x0, y0] }[handle] as Point;
  const moving: Point = { nw: [x0, y0], ne: [x1, y0], sw: [x0, y1], se: [x1, y1] }[handle] as Point;
  return boxFromCorners(fixed, [moving[0] + to[0] - from[0], moving[1] + to[1] - from[1]], photoW, photoH);
}

/** True when a box covers at least one photo pixel each way (a box squeezed against the photo's edge may not). */
export function hasSize([x0, y0, x1, y1]: Box): boolean {
  return x1 - x0 >= 1 && y1 - y0 >= 1;
}

/** True when a drag between two screen points is long enough, both ways, to be a box rather than a tap. */
export function isDrag(a: Point, b: Point): boolean {
  return Math.abs(a[0] - b[0]) >= MIN_DRAG_PX && Math.abs(a[1] - b[1]) >= MIN_DRAG_PX;
}

/** True when the photo the browser shows has the shape the server measured: otherwise the browser turned it (EXIF
 * orientation) or was sent another picture, and boxes drawn on it would land in the wrong place. */
export function sameShape(shownW: number, shownH: number, photoW: number, photoH: number): boolean {
  if (!(shownW > 0 && shownH > 0 && photoW > 0 && photoH > 0)) return false;
  return Math.abs(shownW / shownH - photoW / photoH) <= 0.01 * (photoW / photoH);
}
