import { Hand, Maximize, Minus, PenLine, Plus } from 'lucide-react';
import { useEffect, useId, useLayoutEffect, useRef, useState } from 'react';
import {
  type Box,
  boxFromCorners,
  boxToScreen,
  centreOn,
  clampPoint,
  dragBox,
  fitView,
  type Handle,
  hasSize,
  isDrag,
  type Point,
  pinchView,
  sameShape,
  toPhoto,
  type View,
  type ZoomLimits,
  zoomAt,
  zoomLimits,
} from '@/lib/photoView';

// One photo, zoomable and pannable, with boxes drawn over it in the ORIGINAL photo's pixels (src/lib/photoView.ts
// does every conversion). Mouse, pen and touch all go through pointer events: one pointer draws a box (the Draw
// tool), pans (the Move tool, or the middle button) or pans and taps (the Tap tool: a tap without a drag names a
// point); two fingers pinch and pan, the wheel zooms around the pointer. Tapping a box selects it; a selected box
// shows corner handles to fix it and can be dragged. A box may carry its part's outline, drawn instead of the
// rectangle, and the spotlight dims the photo around the boxes marked lit.

export type BoxKind = 'mark' | 'lookalike' | 'suggestion' | 'kept' | 'fixed' | 'rejected' | 'added';

export interface CanvasBox {
  key: string;
  box: Box;
  kind: BoxKind;
  /** A short label drawn at its top-left corner (a mark's number). */
  label?: string;
  selectable: boolean;
  editable: boolean;
  /** The part's outline (polygons in the photo's pixels), drawn instead of the rectangle; the box stays what counts. */
  outline?: number[][][];
  /** The part's colour, instead of the kind's. */
  color?: string;
  /** The one being asked about now: drawn thicker. */
  current?: boolean;
  /** Stays bright under the spotlight. */
  lit?: boolean;
}

export type CanvasTool = 'draw' | 'move' | 'tap';

/** How each kind of box is drawn; widths and dashes are in screen pixels. */
export const BOX_STYLE: Record<BoxKind, { stroke: string; width: number; dash?: number; fill: string; opacity?: number }> = {
  mark: { stroke: '#F2C14E', width: 2.5, fill: 'rgba(242,193,78,0.12)' },
  lookalike: { stroke: '#F3EDE6', width: 1.5, dash: 4, fill: 'none', opacity: 0.55 },
  suggestion: { stroke: '#5BC0EB', width: 2, dash: 6, fill: 'rgba(91,192,235,0.06)' },
  kept: { stroke: '#5BC0EB', width: 2.5, fill: 'rgba(91,192,235,0.16)' },
  fixed: { stroke: '#5BC0EB', width: 2.5, fill: 'rgba(91,192,235,0.16)' },
  added: { stroke: '#9BE7FF', width: 2.5, fill: 'rgba(155,231,255,0.16)' },
  rejected: { stroke: '#FF4D1A', width: 1.5, dash: 3, fill: 'none', opacity: 0.6 },
};

const HANDLE_PX = 7; // drawn radius of a corner handle
const HANDLE_HIT_PX = 18; // its touch radius
const EDGE_HIT_PX = 14; // how wide a box's edge is to a tap
const CORNERS: Exclude<Handle, 'move'>[] = ['nw', 'ne', 'sw', 'se'];

type Gesture =
  | { kind: 'draw'; fromScreen: Point; fromPhoto: Point }
  | { kind: 'edit'; key: string; handle: Handle; startBox: Box; fromPhoto: Point }
  | { kind: 'pan'; startView: View; fromScreen: Point }
  | { kind: 'pinch'; startView: View; a: Point; b: Point; ids: [number, number] }
  | { kind: 'wait' };

function cornerOf([x0, y0, x1, y1]: Box, handle: Exclude<Handle, 'move'>): Point {
  return { nw: [x0, y0], ne: [x1, y0], sw: [x0, y1], se: [x1, y1] }[handle] as Point;
}

function sameBox(a: Box, b: Box): boolean {
  return a.every((v, i) => v === b[i]);
}

export function PhotoCanvas({
  url,
  width,
  height,
  alt,
  boxes,
  selected,
  tool,
  disabled,
  focus,
  onSelect,
  onDraw,
  onChange,
  onTap,
  onToolChange,
  spotlight = false,
  selectedControls,
}: {
  url: string;
  /** The original photo's size, as the server measured it. */
  width: number;
  height: number;
  alt: string;
  boxes: CanvasBox[];
  selected: string | null;
  tool: CanvasTool;
  disabled: boolean;
  /** Centre the view on this box; a new nonce asks again. */
  focus: { box: Box; nonce: number } | null;
  onSelect: (key: string | null) => void;
  onDraw: (box: Box) => void;
  onChange: (key: string, box: Box) => void;
  /** A tap without a drag with the Tap tool, at this point of the photo. */
  onTap?: (point: Point) => void;
  /** Offered where both drawing and moving make sense: a Draw / Move switch beside the zoom buttons. */
  onToolChange?: (tool: 'draw' | 'move') => void;
  /** Dim the photo around the boxes marked lit. */
  spotlight?: boolean;
  /** Buttons shown next to the selected box. */
  selectedControls?: React.ReactNode;
}) {
  const ref = useRef<HTMLDivElement>(null);
  const maskId = useId();
  const [size, setSize] = useState<[number, number] | null>(null);
  const [view, setView] = useState<View | null>(null);
  const [shown, setShown] = useState<'loading' | 'ok' | 'turned' | 'failed'>('loading');
  const [natural, setNatural] = useState<[number, number] | null>(null);
  const [drawing, setDrawing] = useState<Box | null>(null);
  const [live, setLive] = useState<{ key: string; box: Box } | null>(null);
  const pointers = useRef(new Map<number, Point>());
  const gesture = useRef<Gesture | null>(null);
  const viewRef = useRef<View | null>(null);
  viewRef.current = view;
  const limits: ZoomLimits | null = size ? zoomLimits(fitView(width, height, size[0], size[1])) : null;
  const limitsRef = useRef<ZoomLimits | null>(null);
  limitsRef.current = limits;

  useLayoutEffect(() => {
    const el = ref.current;
    if (!el) return;
    const measure = () => {
      const r = el.getBoundingClientRect();
      if (r.width > 0 && r.height > 0) setSize([r.width, r.height]);
    };
    measure();
    if (typeof ResizeObserver === 'undefined') return;
    const observer = new ResizeObserver(measure);
    observer.observe(el);
    return () => observer.disconnect();
  }, []);

  // The view follows the viewport's size until the person zooms or pans; then a resize keeps their view.
  const moved = useRef(false);
  const moveView = (next: React.SetStateAction<View | null>) => {
    moved.current = true;
    setView(next);
  };
  const fitPhoto = () => {
    if (!size) return;
    moved.current = false;
    setView(fitView(width, height, size[0], size[1]));
  };

  // biome-ignore lint/correctness/useExhaustiveDependencies: a new photo loads again and is fitted afresh
  useEffect(() => {
    moved.current = false;
    setShown('loading');
  }, [url]);

  // biome-ignore lint/correctness/useExhaustiveDependencies: refit on a new photo or viewport size, unless moved
  useEffect(() => {
    if (size && !moved.current) setView(fitView(width, height, size[0], size[1]));
  }, [url, width, height, size]);

  // biome-ignore lint/correctness/useExhaustiveDependencies: the nonce decides when to move
  useEffect(() => {
    if (focus && size && limits) moveView((v) => (v ? centreOn(v, focus.box, size[0], size[1], limits) : v));
  }, [focus?.nonce]);

  // The wheel zooms around the pointer (a trackpad pinch arrives as a wheel with ctrlKey). Not a React handler:
  // those are passive, and the page must not zoom or scroll instead.
  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    const onWheel = (e: WheelEvent) => {
      e.preventDefault();
      const r = el.getBoundingClientRect();
      const factor = Math.exp(-e.deltaY * (e.ctrlKey ? 0.01 : 0.0015));
      const at: Point = [e.clientX - r.left, e.clientY - r.top];
      moved.current = true;
      setView((v) => (v && limitsRef.current ? zoomAt(v, factor, at, limitsRef.current) : v));
    };
    el.addEventListener('wheel', onWheel, { passive: false });
    return () => el.removeEventListener('wheel', onWheel);
  }, []);

  const local = (e: React.PointerEvent): Point => {
    const r = (ref.current as HTMLDivElement).getBoundingClientRect();
    return [e.clientX - r.left, e.clientY - r.top];
  };

  const blocked = disabled || shown !== 'ok';

  const onPointerDown = (e: React.PointerEvent<HTMLDivElement>) => {
    const v = viewRef.current;
    if (!v || blocked) return;
    if (e.pointerType === 'mouse' && e.button !== 0 && e.button !== 1) return;
    const p = local(e);
    pointers.current.set(e.pointerId, p);
    try {
      ref.current?.setPointerCapture(e.pointerId);
    } catch {
      /* the pointer is already gone */
    }
    if (pointers.current.size === 2) {
      const [[ida, a], [idb, b]] = [...pointers.current.entries()];
      gesture.current = { kind: 'pinch', startView: v, a, b, ids: [ida, idb] };
      setDrawing(null);
      setLive(null);
      return;
    }
    if (pointers.current.size > 2) return;
    const target = e.target as Element;
    const fromPhoto = toPhoto(v, p);
    const handleEl = target.closest('[data-handle]');
    const handleBox = handleEl && boxes.find((b) => b.key === handleEl.getAttribute('data-key'));
    if (handleEl && handleBox) {
      gesture.current = {
        kind: 'edit',
        key: handleBox.key,
        handle: handleEl.getAttribute('data-handle') as Handle,
        startBox: handleBox.box,
        fromPhoto,
      };
      return;
    }
    const boxEl = target.closest('[data-box]');
    const hit = boxEl && boxes.find((b) => b.key === boxEl.getAttribute('data-box'));
    if (hit?.selectable) {
      if (hit.key === selected && hit.editable) {
        gesture.current = { kind: 'edit', key: hit.key, handle: 'move', startBox: hit.box, fromPhoto };
      } else {
        onSelect(hit.key);
        gesture.current = { kind: 'wait' };
      }
      return;
    }
    if (tool !== 'draw' || e.button === 1) {
      gesture.current = { kind: 'pan', startView: v, fromScreen: p };
      return;
    }
    gesture.current = { kind: 'draw', fromScreen: p, fromPhoto: clampPoint(fromPhoto, width, height) };
  };

  const onPointerMove = (e: React.PointerEvent<HTMLDivElement>) => {
    if (!pointers.current.has(e.pointerId)) return;
    const p = local(e);
    pointers.current.set(e.pointerId, p);
    const g = gesture.current;
    const v = viewRef.current;
    if (!g || !v) return;
    if (g.kind === 'pinch') {
      const a = pointers.current.get(g.ids[0]);
      const b = pointers.current.get(g.ids[1]);
      if (a && b && limits) moveView(pinchView(g.startView, g.a, g.b, a, b, limits));
    } else if (g.kind === 'pan') {
      moveView({ ...g.startView, x: g.startView.x + p[0] - g.fromScreen[0], y: g.startView.y + p[1] - g.fromScreen[1] });
    } else if (g.kind === 'draw') {
      setDrawing(isDrag(g.fromScreen, p) ? boxFromCorners(g.fromPhoto, toPhoto(v, p), width, height) : null);
    } else if (g.kind === 'edit') {
      setLive({ key: g.key, box: dragBox(g.startBox, g.handle, g.fromPhoto, toPhoto(v, p), width, height) });
    }
  };

  const onPointerEnd = (e: React.PointerEvent<HTMLDivElement>, cancelled: boolean) => {
    if (!pointers.current.has(e.pointerId)) return;
    const p = local(e);
    pointers.current.delete(e.pointerId);
    const g = gesture.current;
    const v = viewRef.current;
    if (g?.kind === 'pinch') {
      // The other finger may still be down: it neither draws nor pans until it lifts too.
      gesture.current = pointers.current.size > 0 ? { kind: 'wait' } : null;
      return;
    }
    gesture.current = null;
    setDrawing(null);
    setLive(null);
    if (!g || !v || cancelled) return;
    if (g.kind === 'pan' && tool === 'tap' && onTap && !isDrag(g.fromScreen, p)) {
      onTap(clampPoint(toPhoto(v, p), width, height));
      return;
    }
    if (g.kind === 'draw') {
      const box = boxFromCorners(g.fromPhoto, toPhoto(v, p), width, height);
      if (isDrag(g.fromScreen, p) && hasSize(box)) onDraw(box);
      else onSelect(null);
    } else if (g.kind === 'edit') {
      const box = dragBox(g.startBox, g.handle, g.fromPhoto, toPhoto(v, p), width, height);
      if (hasSize(box) && !sameBox(box, g.startBox)) onChange(g.key, box);
    }
  };

  const zoomBy = (factor: number) => {
    if (size && limits) moveView((v) => (v ? zoomAt(v, factor, [size[0] / 2, size[1] / 2], limits) : v));
  };

  const px = (screenPx: number) => (view ? screenPx / view.scale : screenPx);
  // The selected box is drawn last, so its handles are on top of every other box.
  const ordered = [...boxes.filter((b) => b.key !== selected), ...boxes.filter((b) => b.key === selected)];
  const selectedBox = boxes.find((b) => b.key === selected);
  const selectedRect = view && selectedBox ? boxToScreen(view, live?.key === selectedBox.key ? live.box : selectedBox.box) : null;

  return (
    <div
      ref={ref}
      role="application"
      aria-label={alt}
      className="relative h-full w-full overflow-hidden touch-none select-none bg-[#0B0908]"
      style={{ cursor: blocked ? 'default' : tool === 'move' ? 'grab' : tool === 'tap' ? 'pointer' : 'crosshair' }}
      onPointerDown={onPointerDown}
      onPointerMove={onPointerMove}
      onPointerUp={(e) => onPointerEnd(e, false)}
      onPointerCancel={(e) => onPointerEnd(e, true)}
    >
      {view && (
        <div
          className="absolute left-0 top-0"
          style={{ width, height, transform: `translate(${view.x}px, ${view.y}px) scale(${view.scale})`, transformOrigin: '0 0' }}
        >
          <img
            src={url}
            alt={alt}
            draggable={false}
            className="pointer-events-none block"
            style={{ width, height, maxWidth: 'none' }}
            onLoad={(e) => {
              const img = e.currentTarget;
              setNatural([img.naturalWidth, img.naturalHeight]);
              setShown(sameShape(img.naturalWidth, img.naturalHeight, width, height) ? 'ok' : 'turned');
            }}
            onError={() => setShown('failed')}
          />
          <svg
            className="absolute left-0 top-0 overflow-visible"
            width={width}
            height={height}
            viewBox={`0 0 ${width} ${height}`}
            aria-hidden="true"
          >
            {spotlight && (
              <>
                <defs>
                  <mask id={maskId}>
                    <rect x={0} y={0} width={width} height={height} fill="white" />
                    {boxes
                      .filter((b) => b.lit)
                      .map((b) =>
                        b.outline?.length ? (
                          b.outline.map((poly, i) => (
                            <polygon key={`${b.key}-${i}`} points={poly.map((q) => q.join(',')).join(' ')} fill="black" />
                          ))
                        ) : (
                          <rect
                            key={b.key}
                            x={b.box[0]}
                            y={b.box[1]}
                            width={b.box[2] - b.box[0]}
                            height={b.box[3] - b.box[1]}
                            fill="black"
                          />
                        ),
                      )}
                  </mask>
                </defs>
                <rect
                  x={0}
                  y={0}
                  width={width}
                  height={height}
                  fill="#0B0908"
                  fillOpacity={0.55}
                  mask={`url(#${maskId})`}
                  pointerEvents="none"
                />
              </>
            )}
            {ordered.map((b) => {
              const box = live?.key === b.key ? live.box : b.box;
              const [x0, y0, x1, y1] = box;
              const style = BOX_STYLE[b.kind];
              const isSelected = b.key === selected;
              const movable = isSelected && b.editable;
              const stroke = b.kind === 'rejected' ? style.stroke : (b.color ?? style.stroke);
              const strokeWidth = px((b.current ? 4 : isSelected ? style.width + 1 : style.width) + (b.outline ? 0.5 : 0));
              const dash = style.dash ? `${px(style.dash)} ${px(style.dash)}` : undefined;
              const outlined = !!b.outline?.length && !live;
              return (
                <g key={b.key}>
                  {outlined ? (
                    b.outline?.map((poly, i) => (
                      <polygon
                        key={i}
                        points={poly.map((q) => q.join(',')).join(' ')}
                        fill={b.kind === 'rejected' ? 'none' : stroke}
                        fillOpacity={b.current ? 0.2 : 0.1}
                        stroke={stroke}
                        strokeWidth={strokeWidth}
                        strokeLinejoin="round"
                        strokeDasharray={b.kind === 'rejected' ? dash : undefined}
                        opacity={style.opacity}
                        pointerEvents="none"
                      />
                    ))
                  ) : (
                    <rect
                      x={x0}
                      y={y0}
                      width={x1 - x0}
                      height={y1 - y0}
                      fill={b.color && b.kind !== 'rejected' ? `${b.color}1A` : style.fill}
                      stroke={stroke}
                      strokeWidth={strokeWidth}
                      strokeDasharray={b.kind === 'suggestion' && b.color ? undefined : dash}
                      opacity={style.opacity}
                      pointerEvents="none"
                    />
                  )}
                  {isSelected && (
                    <rect
                      x={x0 - px(3)}
                      y={y0 - px(3)}
                      width={x1 - x0 + px(6)}
                      height={y1 - y0 + px(6)}
                      fill="none"
                      stroke="#FFFFFF"
                      strokeWidth={px(1)}
                      pointerEvents="none"
                    />
                  )}
                  {b.selectable && (
                    <rect
                      data-box={b.key}
                      x={x0}
                      y={y0}
                      width={x1 - x0}
                      height={y1 - y0}
                      fill="transparent"
                      stroke="transparent"
                      strokeWidth={px(EDGE_HIT_PX)}
                      pointerEvents={movable || b.outline ? 'all' : 'stroke'}
                      style={{ cursor: movable ? 'move' : 'pointer' }}
                    />
                  )}
                  {b.label && (
                    <text
                      x={x0 + px(3)}
                      y={y0 - px(5)}
                      fontSize={px(13)}
                      fontWeight={700}
                      fill={style.stroke}
                      stroke="#0B0908"
                      strokeWidth={px(3)}
                      paintOrder="stroke"
                      pointerEvents="none"
                    >
                      {b.label}
                    </text>
                  )}
                  {movable &&
                    CORNERS.map((h) => {
                      const [cx, cy] = cornerOf(box, h);
                      return (
                        <g
                          key={h}
                          data-key={b.key}
                          data-handle={h}
                          style={{ cursor: h === 'nw' || h === 'se' ? 'nwse-resize' : 'nesw-resize' }}
                        >
                          <circle cx={cx} cy={cy} r={px(HANDLE_HIT_PX)} fill="transparent" />
                          <circle cx={cx} cy={cy} r={px(HANDLE_PX)} fill="#FFFFFF" stroke={style.stroke} strokeWidth={px(2)} />
                        </g>
                      );
                    })}
                </g>
              );
            })}
            {drawing && (
              <rect
                x={drawing[0]}
                y={drawing[1]}
                width={drawing[2] - drawing[0]}
                height={drawing[3] - drawing[1]}
                fill="rgba(255,255,255,0.08)"
                stroke="#FFFFFF"
                strokeWidth={px(1.5)}
                strokeDasharray={`${px(5)} ${px(4)}`}
                pointerEvents="none"
              />
            )}
          </svg>
        </div>
      )}

      {selectedRect && selectedControls && size && !live && (
        <div
          className="absolute z-10 flex gap-1.5"
          style={{
            left: Math.min(Math.max(selectedRect.left + selectedRect.width, 8), size[0] - 8),
            top: Math.min(Math.max(selectedRect.top - 8, 56), size[1] - 8),
            transform: 'translate(-100%, -100%)',
          }}
          onPointerDown={(e) => e.stopPropagation()}
        >
          {selectedControls}
        </div>
      )}

      <div className="absolute right-3 bottom-3 z-10 flex flex-col gap-1.5" onPointerDown={(e) => e.stopPropagation()}>
        {onToolChange && tool !== 'tap' && (
          <button
            type="button"
            aria-label={tool === 'draw' ? 'Move the photo instead of drawing' : 'Draw boxes'}
            aria-pressed={tool === 'move'}
            onClick={() => onToolChange(tool === 'draw' ? 'move' : 'draw')}
            className="flex size-10 items-center justify-center rounded-full bg-[#17110E]/90 border border-white/10 text-[#F3EDE6] hover:bg-[#2A1F19] cursor-pointer"
          >
            {tool === 'draw' ? <Hand className="size-4" /> : <PenLine className="size-4" />}
          </button>
        )}
        <button
          type="button"
          aria-label="Zoom in"
          onClick={() => zoomBy(1.5)}
          className="flex size-10 items-center justify-center rounded-full bg-[#17110E]/90 border border-white/10 text-[#F3EDE6] hover:bg-[#2A1F19] cursor-pointer"
        >
          <Plus className="size-5" />
        </button>
        <button
          type="button"
          aria-label="Zoom out"
          onClick={() => zoomBy(1 / 1.5)}
          className="flex size-10 items-center justify-center rounded-full bg-[#17110E]/90 border border-white/10 text-[#F3EDE6] hover:bg-[#2A1F19] cursor-pointer"
        >
          <Minus className="size-5" />
        </button>
        <button
          type="button"
          aria-label="Show the whole photo"
          onClick={fitPhoto}
          className="flex size-10 items-center justify-center rounded-full bg-[#17110E]/90 border border-white/10 text-[#F3EDE6] hover:bg-[#2A1F19] cursor-pointer"
        >
          <Maximize className="size-4" />
        </button>
      </div>

      {shown === 'loading' && (
        <p className="absolute inset-x-0 top-1/2 m-0 text-center text-[15px] text-[#D8CCBF] pointer-events-none">Loading the photo…</p>
      )}
      {shown === 'failed' && (
        <p role="alert" className="absolute inset-x-6 top-1/2 m-0 text-center text-[15px] text-[#E9B987]">
          The photo could not be loaded.
        </p>
      )}
      {shown === 'turned' && natural && (
        <p
          role="alert"
          className="absolute inset-x-6 top-1/3 m-0 rounded-[14px] bg-[#110C0A]/90 p-4 text-center text-[15px] text-[#E9B987]"
        >
          This photo shows here as {natural[0]} × {natural[1]} pixels, but the server measured it as {width} × {height}. Boxes drawn on it
          would land in the wrong place, so marking is off for this photo.
        </p>
      )}
    </div>
  );
}
