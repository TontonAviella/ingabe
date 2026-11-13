import type { GeoJSONSource, MapMouseEvent, MapTouchEvent, Map as MLMap } from 'maplibre-gl';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
  type Corner,
  type KeptOutline,
  measureMovedLine,
  outlinesFor,
  type Tap,
  type TapOutline,
  type TurnDownReason,
  turnDown,
  useFieldTapState,
  useKeepOutline,
  useRemoveOutline,
} from '@/hooks/useFieldTaps';
import type { ServerError } from '@/lib/serverError';
import type { MapLayer } from '@/lib/types';

// Tap a field on a prepared drone photo: up to three outlines come back, the reader picks one, may correct it
// ("this part too" / "not this part"), goes back a step if a correction made it worse, and keeps it. Every outline,
// area and limit comes from the server; this component draws them and sends taps.

const SOURCE = 'field-taps';
const LAYERS = {
  fill: 'field-taps-fill',
  casing: 'field-taps-casing',
  line: 'field-taps-line',
  taps: 'field-taps-taps',
  label: 'field-taps-label',
};
const CHOICE_COLOURS = ['#f5a524', '#38bdf8', '#f472b6'];
const KEPT_COLOUR = '#ffffff';
const SELECTED_COLOUR = '#f5a524';
const LABEL_FONT = ['Open Sans Semibold'];
const CHOICE_NAMES = ['A', 'B', 'C'];
const EMPTY: GeoJSON.FeatureCollection = { type: 'FeatureCollection', features: [] };

/** One step of the work on a field: the taps so far and what the server drew for them. */
interface Step {
  taps: Tap[];
  outlines: TapOutline[];
}

// off: closed. first: waiting for the first tap, or showing its three outlines. review: one outline picked.
// add / remove: waiting for a correcting tap. move: the reader pulls the line of the picked outline.
type Mode = 'off' | 'first' | 'review' | 'add' | 'remove' | 'move';

// An area in the unit and to the digits the outline supports: square metres for small fields (a hectare figure with
// two decimals would print "0.00 ha" for a real 40 m² field), to the nearest 10 m² once there are 100 of them.
const ha = (value: number) => {
  if (value >= 1) return `${value.toFixed(1)} ha`;
  if (value >= 0.05) return `${value.toFixed(2)} ha`;
  const m2 = value * 10_000;
  if (m2 < 10) return 'under 10 m²';
  return `${m2 >= 100 ? Math.round(m2 / 10) * 10 : Math.round(m2)} m²`;
};
// 44 px tall under a finger, 36 px with a mouse
const link = 'inline-flex min-h-11 cursor-pointer items-center px-2 text-[12.5px] text-fg-soft underline sm:min-h-9';

const closed = (ring: Corner[]): GeoJSON.Polygon => ({ type: 'Polygon', coordinates: [[...ring, ring[0]]] });

// Pulling the line: the reader grabs the outline within GRAB_PX of the pointer and drags. Corners within REACH_PX of
// the grab follow, the nearest fully and the farthest not at all, so the line bends instead of making a spike. Before
// a pull the outline gets a corner at least every STEP_PX, so a long straight side can bend too.
const GRAB_PX = 14;
const REACH_PX = 60;
const STEP_PX = 10;
const MAX_CORNERS = 560; // under the server's limit; past it the outline is pulled with the corners it has

type Pixel = { x: number; y: number };

/** The outline with a corner at least every STEP_PX on screen. */
function withCloseCorners(ring: Corner[], project: (c: Corner) => Pixel, unproject: (p: Pixel) => Corner): Corner[] {
  const out: Corner[] = [];
  ring.forEach((corner, i) => {
    out.push(corner);
    const a = project(corner);
    const b = project(ring[(i + 1) % ring.length]);
    const pieces = Math.ceil(Math.hypot(b.x - a.x, b.y - a.y) / STEP_PX);
    for (let k = 1; k < pieces; k++) out.push(unproject({ x: a.x + ((b.x - a.x) * k) / pieces, y: a.y + ((b.y - a.y) * k) / pieces }));
  });
  return out.length <= MAX_CORNERS ? out : ring;
}

function features(
  steps: Step[],
  pick: number | null,
  kept: KeptOutline[],
  ring: Corner[] | null = null,
  moving = false,
  selected: string | null = null,
): GeoJSON.FeatureCollection {
  const out: GeoJSON.Feature[] = kept.flatMap((k) => [
    {
      type: 'Feature' as const,
      geometry: k.geometry,
      properties: { id: k.id, colour: k.id === selected ? SELECTED_COLOUR : KEPT_COLOUR, width: k.id === selected ? 4 : 2 },
    },
    {
      type: 'Feature' as const,
      geometry: { type: 'Point' as const, coordinates: k.centre },
      properties: { kind: 'label', label: String(k.number) },
    },
  ]);
  const step = steps.at(-1);
  if (step) {
    step.outlines.forEach((o, i) => {
      if (pick !== null && o.choice !== pick) return;
      const geometry = ring && pick !== null ? closed(ring) : o.geometry;
      out.push({ type: 'Feature', geometry, properties: { colour: CHOICE_COLOURS[i], width: ring && moving ? 5 : 3 } });
    });
    for (const t of step.taps) {
      out.push({
        type: 'Feature',
        geometry: { type: 'Point', coordinates: [t.lon, t.lat] },
        properties: { kind: 'tap', colour: t.add ? '#22c55e' : '#ef4444' },
      });
      // not by colour alone: "this part too" carries a plus, "not this part" a minus
      out.push({
        type: 'Feature',
        geometry: { type: 'Point', coordinates: [t.lon, t.lat] },
        properties: { kind: 'label', label: t.add ? '+' : '−' },
      });
    }
  }
  return { type: 'FeatureCollection', features: out };
}

function useDrawn(map: MLMap | null, data: GeoJSON.FeatureCollection) {
  useEffect(() => {
    if (!map) return;
    const apply = () => {
      const added = !map.getSource(SOURCE);
      if (added) {
        map.addSource(SOURCE, { type: 'geojson', data: EMPTY });
        map.addLayer({
          id: LAYERS.fill,
          type: 'fill',
          source: SOURCE,
          filter: ['==', ['geometry-type'], 'Polygon'],
          paint: { 'fill-color': ['get', 'colour'], 'fill-opacity': 0.12 },
        });
        map.addLayer({
          // a dark edge under every outline, so a pale line still shows on pale soil
          id: LAYERS.casing,
          type: 'line',
          source: SOURCE,
          filter: ['==', ['geometry-type'], 'Polygon'],
          paint: { 'line-color': '#0B0908', 'line-opacity': 0.7, 'line-width': ['+', ['get', 'width'], 3] },
        });
        map.addLayer({
          id: LAYERS.line,
          type: 'line',
          source: SOURCE,
          filter: ['==', ['geometry-type'], 'Polygon'],
          paint: { 'line-color': ['get', 'colour'], 'line-width': ['get', 'width'] },
        });
        map.addLayer({
          id: LAYERS.taps,
          type: 'circle',
          source: SOURCE,
          filter: ['==', ['get', 'kind'], 'tap'],
          paint: { 'circle-radius': 6, 'circle-color': ['get', 'colour'], 'circle-stroke-color': '#ffffff', 'circle-stroke-width': 2 },
        });
      }
      (map.getSource(SOURCE) as GeoJSONSource).setData(data);
      if (!added) return;
      map.addLayer({
        id: LAYERS.label,
        type: 'symbol',
        source: SOURCE,
        filter: ['==', ['get', 'kind'], 'label'],
        layout: { 'text-field': ['get', 'label'], 'text-font': LABEL_FONT, 'text-size': 13, 'text-allow-overlap': true },
        paint: { 'text-color': '#FFF7EC', 'text-halo-color': '#0B0908', 'text-halo-width': 1.6 },
      });
    };
    if (map.isStyleLoaded()) apply();
    else map.once('idle', apply);
    map.on('style.load', apply);
    return () => {
      map.off('style.load', apply);
      map.off('idle', apply);
    };
  }, [map, data]);
}

export function FieldTaps({
  map,
  layers,
  hiddenLayerIDs,
  onTapping,
  onAskSage,
}: {
  map: MLMap | null;
  layers: MapLayer[];
  hiddenLayerIDs: string[];
  /** Told true while the panel is open, so other map cards can stay out of the way of the taps. */
  onTapping?: (tapping: boolean) => void;
  /** Sends a question to Sage on this map. */
  onAskSage?: (prompt: string) => void;
}) {
  const photo = useMemo(() => layers.find((l) => l.type === 'raster' && !hiddenLayerIDs.includes(l.id)) ?? null, [layers, hiddenLayerIDs]);
  const layerId = photo?.id ?? null;
  const state = useFieldTapState(layerId);
  const keep = useKeepOutline(layerId);
  const remove = useRemoveOutline(layerId);
  const [mode, setMode] = useState<Mode>('off');
  const [steps, setSteps] = useState<Step[]>([]);
  const [pick, setPick] = useState<number | null>(null);
  const [busy, setBusy] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);
  const [listed, setListed] = useState(false);
  // The kept field the reader clicked: its number and size show, and a question about it can go to Sage.
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [question, setQuestion] = useState('');
  const [removing, setRemoving] = useState(false); // "Remove Field 7?" is showing
  const [slow, setSlow] = useState(false); // an answer is taking over a second: say what is happening
  // The picked outline's corners once the reader starts moving its line, and the area the server gives for them
  // (null while a moved line is not a valid outline).
  const [ring, setRing] = useState<Corner[] | null>(null);
  const [ringArea, setRingArea] = useState<number | null>(null);
  const ringRef = useRef<Corner[] | null>(null);
  ringRef.current = ring;
  // Each start-again makes earlier answers stale: one that arrives late is dropped, not shown.
  const round = useRef(0);

  const prepared = !!state.data?.prepared;
  const kept = state.data?.kept ?? [];
  const step = steps.at(-1) ?? null;
  const corrections = step ? step.taps.length - 1 : 0;
  const selected = mode === 'off' ? (kept.find((k) => k.id === selectedId) ?? null) : null;
  const shown =
    mode === 'off' ? features([], null, kept, null, false, selected?.id ?? null) : features(steps, pick, kept, ring, mode === 'move');
  useDrawn(map, prepared ? shown : EMPTY);

  const startAgain = useCallback((next: Mode) => {
    round.current += 1;
    setSteps([]);
    setPick(null);
    setProblem(null);
    setRing(null);
    setRingArea(null);
    setMode(next);
  }, []);

  // biome-ignore lint/correctness/useExhaustiveDependencies: a new photo starts from nothing
  useEffect(() => startAgain('off'), [layerId, startAgain]);

  // A tap that was answered and not kept is told to the server; a failure to tell it is shown, never hidden.
  const record = useCallback(
    (taps: Tap[], chosen: number | null, reason: TurnDownReason) => {
      if (!layerId) return;
      turnDown(layerId, taps, chosen, reason).catch((error: ServerError) => setProblem(`Not recorded: ${error.message}`));
    },
    [layerId],
  );

  const ask = useCallback(
    async (taps: Tap[], chosen: number | null) => {
      if (!layerId) return;
      setBusy(true);
      setProblem(null);
      const waiting = setTimeout(() => setSlow(true), 1200);
      const asked = round.current;
      try {
        const { outlines } = await outlinesFor(layerId, taps, chosen);
        if (asked !== round.current) return; // the reader closed or started again while this was on its way
        if (outlines.length === 0) {
          if (taps.length === 1) record(taps, null, 'no_outline');
          setProblem(
            taps.length === 1
              ? 'No outline found under that tap. Tap the field again.'
              : 'That correction left no outline. Go back a step.',
          );
          if (taps.length === 1) return;
        }
        setSteps((before) => (taps.length === 1 ? [{ taps, outlines }] : [...before, { taps, outlines }]));
        setMode(taps.length === 1 ? 'first' : 'review');
      } catch (error) {
        if (asked === round.current) setProblem((error as ServerError).message);
      } finally {
        clearTimeout(waiting);
        setSlow(false);
        setBusy(false);
      }
    },
    [layerId, record],
  );

  // Taps on the map while a tap is being asked for.
  useEffect(() => {
    if (!map || mode === 'off' || mode === 'review' || mode === 'move' || busy) return;
    if (mode === 'first' && step) return; // three outlines are showing: the reader picks one first
    const onClick = (e: MapMouseEvent) => {
      const tap: Tap = { lon: e.lngLat.lng, lat: e.lngLat.lat, add: mode !== 'remove' };
      void ask(mode === 'first' ? [tap] : [...(step?.taps ?? []), tap], mode === 'first' ? null : pick);
    };
    map.on('click', onClick);
    map.getCanvas().style.cursor = 'crosshair';
    return () => {
      map.off('click', onClick);
      map.getCanvas().style.cursor = '';
    };
  }, [map, mode, busy, step, pick, ask]);

  // With the panel closed, a click on a kept field picks it; a click anywhere else lets it go.
  useEffect(() => {
    if (!map || mode !== 'off' || !prepared) return;
    const onClick = (e: MapMouseEvent) => {
      const hit = map.queryRenderedFeatures(e.point, { layers: [LAYERS.fill] }).find((f) => f.properties?.id);
      setSelectedId(hit ? String(hit.properties.id) : null);
      setQuestion('');
      setRemoving(false);
    };
    map.on('click', onClick);
    return () => {
      map.off('click', onClick);
    };
  }, [map, mode, prepared]);

  // Pulling the line of the picked outline. The corners live here while the reader pulls; the server says
  // what area the moved line has, or why it is no longer an outline.
  const firstTap = step?.taps[0] ?? null;
  useEffect(() => {
    if (!map || mode !== 'move' || !layerId || !firstTap || !ringRef.current) return;
    let corners = ringRef.current;
    let pull: { from: Corner; base: Corner[]; weights: number[] } | null = null;
    const at = (e: MapMouseEvent | MapTouchEvent): Corner => [e.lngLat.lng, e.lngLat.lat];
    const onDown = (e: MapMouseEvent | MapTouchEvent) => {
      const project = (c: Corner): Pixel => map.project(c);
      const base = withCloseCorners(corners, project, (p) => {
        const { lng, lat } = map.unproject([p.x, p.y]);
        return [lng, lat];
      });
      const far = base.map((c) => Math.hypot(project(c).x - e.point.x, project(c).y - e.point.y));
      if (Math.min(...far) > GRAB_PX) return; // not on the line: the map pans as usual
      e.preventDefault(); // the map stays still while the line is pulled
      pull = { from: at(e), base, weights: far.map((d) => (d < REACH_PX ? 0.5 * (1 + Math.cos((Math.PI * d) / REACH_PX)) : 0)) };
    };
    const onMove = (e: MapMouseEvent | MapTouchEvent) => {
      if (!pull) return;
      const [lng, lat] = at(e);
      const { from, base, weights } = pull;
      corners = base.map((c, i) => [c[0] + weights[i] * (lng - from[0]), c[1] + weights[i] * (lat - from[1])]);
      setRing(corners);
    };
    const onUp = () => {
      if (!pull) return;
      pull = null;
      setRingArea(null); // not measured yet: Keep waits for the server's answer on this line
      measureMovedLine(layerId, firstTap, corners)
        .then((answer) => {
          setRingArea(answer.area_ha);
          setProblem(null);
        })
        .catch((error: ServerError) => {
          setRingArea(null);
          setProblem(error.message);
        });
    };
    map.on('mousedown', onDown);
    map.on('touchstart', onDown);
    map.on('mousemove', onMove);
    map.on('touchmove', onMove);
    map.on('mouseup', onUp);
    map.on('touchend', onUp);
    return () => {
      map.off('mousedown', onDown);
      map.off('touchstart', onDown);
      map.off('mousemove', onMove);
      map.off('touchmove', onMove);
      map.off('mouseup', onUp);
      map.off('touchend', onUp);
    };
  }, [map, mode, layerId, firstTap]);

  const open = mode !== 'off' && !!layerId && prepared;
  useEffect(() => {
    onTapping?.(open);
    return () => onTapping?.(false);
  }, [open, onTapping]);

  if (!layerId || !prepared) return null;

  /** Leave the field being outlined without keeping it. */
  const giveUp = (reason: TurnDownReason, next: Mode) => {
    if (step) record(step.taps, pick, reason);
    startAgain(next);
  };

  const back = () => {
    setProblem(null);
    if (ring) {
      // the hand-moved line goes first: back to the outline as the model drew it
      setRing(null);
      setRingArea(null);
      setMode('review');
      return;
    }
    if (steps.length > 1) setSteps(steps.slice(0, -1));
    else setPick(null);
    setMode(steps.length > 1 ? 'review' : 'first');
  };

  const button = 'min-h-11 px-3 rounded-[12px] text-[13px] font-semibold cursor-pointer disabled:opacity-50 sm:min-h-9';
  const plain = `${button} bg-panel-2 text-fg hover:bg-panel-3`;
  const strong = `${button} bg-caramel text-panel hover:bg-caramel-hi`;
  const picked = pick !== null ? (step?.outlines.find((o) => o.choice === pick) ?? null) : null;
  const canCorrect = corrections < (state.data?.max_corrections ?? 0);

  return (
    <div
      className={`absolute bottom-28 left-1/2 z-30 flex -translate-x-1/2 flex-col gap-2 rounded-[18px] border border-caramel/30 bg-panel p-3 text-fg shadow-lg ${
        open ? 'w-[min(92vw,460px)]' : 'w-[min(92vw,340px)]'
      }`}
    >
      {selected && (
        <div className="flex flex-col gap-2">
          <div className="flex items-center justify-between gap-3">
            <span className="text-[14px] font-semibold">
              Field {selected.number} · {ha(selected.area_ha)}
              {selected.by_hand ? ' · by hand' : ''}
            </span>
            {!removing && (
              <button type="button" className={link} onClick={() => setRemoving(true)}>
                Remove
              </button>
            )}
          </div>
          {removing && (
            <div className="flex flex-wrap items-center gap-2">
              <span className="text-[13px]">Remove Field {selected.number}? Its number will not be used again.</span>
              <button
                type="button"
                className={plain}
                disabled={remove.isPending}
                onClick={() =>
                  remove.mutate(selected.id, {
                    onSuccess: () => {
                      setSelectedId(null);
                      setRemoving(false);
                      setProblem(null);
                    },
                    onError: (e) => setProblem(e.message),
                  })
                }
              >
                Remove
              </button>
              <button type="button" className={strong} onClick={() => setRemoving(false)}>
                Keep
              </button>
            </div>
          )}
          {problem && (
            <p role="alert" className="m-0 text-[13px] text-caramel-hi">
              {problem}
            </p>
          )}
          {onAskSage && (
            <form
              className="flex gap-2"
              onSubmit={(e) => {
                e.preventDefault();
                const asked = question.trim();
                if (!asked) return;
                const [lon, lat] = selected.centre;
                onAskSage(
                  `About Field ${selected.number} on the drone photo "${photo?.name ?? ''}" (plot ${selected.number} of my outlined fields: ${ha(selected.area_ha)}, centred at latitude ${lat}, longitude ${lon}): ${asked}`,
                );
                setQuestion('');
              }}
            >
              <input
                className="min-h-11 sm:min-h-9 flex-1 rounded-[12px] border border-white/10 bg-panel-2 px-3 text-[13px] text-fg"
                placeholder={`Ask Sage about Field ${selected.number}…`}
                aria-label={`Ask Sage about Field ${selected.number}`}
                value={question}
                onChange={(e) => setQuestion(e.target.value)}
              />
              <button type="submit" className={strong} disabled={!question.trim()}>
                Ask
              </button>
            </form>
          )}
        </div>
      )}
      {mode === 'off' ? (
        <div className="flex items-center justify-between gap-3">
          {kept.length ? (
            <button
              type="button"
              className="inline-flex min-h-11 sm:min-h-9 cursor-pointer items-center text-[13px] text-fg-soft underline"
              onClick={() => setListed(!listed)}
            >
              {kept.length} field{kept.length === 1 ? '' : 's'} outlined · {listed ? 'hide' : 'show'}
            </button>
          ) : (
            <span className="text-[13px] text-fg-soft">Outline a field by tapping it</span>
          )}
          <button type="button" className={strong} onClick={() => startAgain('first')}>
            Outline a field
          </button>
        </div>
      ) : (
        <>
          <p role="status" aria-live="polite" className="m-0 text-[14px] font-semibold">
            {busy
              ? slow
                ? 'Getting the outline ready. The first tap after a restart can take about 10 seconds.'
                : 'Working…'
              : !step
                ? 'Tap inside the field.'
                : pick === null
                  ? 'Which outline is your field?'
                  : mode === 'add'
                    ? 'Tap a part of the field the outline missed.'
                    : mode === 'remove'
                      ? 'Tap a part the outline should not cover.'
                      : mode === 'move'
                        ? 'Pull the line to the true edge of the field.'
                        : ring
                          ? `Your field: ${ringArea === null ? 'not an outline' : ha(ringArea)} · line moved by hand`
                          : `Your field: ${picked ? ha(picked.area_ha) : 'no outline'}`}
          </p>
          {step && pick === null && (
            <div className="flex flex-wrap gap-2">
              {step.outlines.map((o, i) => (
                <button
                  key={o.choice}
                  type="button"
                  className={plain}
                  style={{ borderBottom: `3px solid ${CHOICE_COLOURS[i]}` }}
                  onClick={() => {
                    setPick(o.choice);
                    setMode('review');
                  }}
                >
                  {CHOICE_NAMES[i]} · {ha(o.area_ha)}
                </button>
              ))}
              <button type="button" className={plain} onClick={() => giveUp('none_of_these', 'first')}>
                None of these
              </button>
            </div>
          )}
          {step && pick !== null && mode === 'move' && (
            <div className="flex flex-wrap items-center gap-2">
              <button type="button" className={strong} onClick={() => setMode('review')}>
                Done
              </button>
              <button type="button" className={plain} onClick={back}>
                Undo my changes
              </button>
              {ringArea !== null && <span className="text-[13px] text-fg-soft">{ha(ringArea)}</span>}
            </div>
          )}
          {step && pick !== null && mode !== 'move' && (
            <div className="flex flex-wrap gap-2">
              <button type="button" className={plain} disabled={busy || !canCorrect || !!ring} onClick={() => setMode('add')}>
                This part too
              </button>
              <button type="button" className={plain} disabled={busy || !canCorrect || !!ring} onClick={() => setMode('remove')}>
                Not this part
              </button>
              <button
                type="button"
                className={plain}
                disabled={busy || !picked}
                onClick={() => {
                  if (!ring && picked) {
                    setRing(picked.geometry.coordinates[0].slice(0, -1) as Corner[]);
                    setRingArea(picked.area_ha);
                  }
                  setMode('move');
                }}
              >
                Move the line
              </button>
              <button type="button" className={plain} disabled={busy} onClick={back}>
                Back
              </button>
              <button
                type="button"
                className={strong}
                disabled={busy || keep.isPending || !picked || (!!ring && ringArea === null)}
                onClick={() =>
                  keep.mutate(
                    { taps: step.taps, pick, ...(ring ? { moved: ring } : {}) },
                    { onSuccess: () => startAgain('off'), onError: (e) => setProblem(e.message) },
                  )
                }
              >
                Keep
              </button>
            </div>
          )}
          {step && pick !== null && !canCorrect && !ring && mode !== 'move' && (
            <p className="m-0 text-[12px] text-fg-muted">No corrections left for this field. Keep it, go back, or start again.</p>
          )}
          {problem && (
            <p role="alert" className="m-0 text-[13px] text-caramel-hi">
              {problem}
            </p>
          )}
          <div className="flex items-center justify-between gap-3">
            <button type="button" className={link} onClick={() => giveUp('started_again', 'first')}>
              Start again
            </button>
            <button type="button" className={link} onClick={() => giveUp('closed', 'off')}>
              Close
            </button>
          </div>
        </>
      )}
      {kept.length > 0 && mode === 'off' && listed && (
        <ul className="m-0 flex max-h-40 list-none flex-col gap-1 overflow-y-auto p-0">
          {kept.map((k) => (
            <li key={k.id} className="text-[13px]">
              <button
                type="button"
                className="flex min-h-11 sm:min-h-9 w-full cursor-pointer items-center text-left underline-offset-2 hover:underline"
                onClick={() => {
                  setSelectedId(k.id);
                  setRemoving(false);
                  setListed(false);
                  // the field lands above this panel, not under it
                  map?.easeTo({ center: k.centre, offset: [0, -140] });
                }}
              >
                Field {k.number} · {ha(k.area_ha)}
                {k.by_hand ? ' · by hand' : ''}
              </button>
            </li>
          ))}
        </ul>
      )}
      {open && (
        <details className="text-[12.5px] leading-snug text-fg-soft">
          <summary className="flex min-h-11 sm:min-h-9 cursor-pointer items-center">What was measured, and what was not</summary>
          <p className="m-0 mt-1">{state.data?.measured}</p>
        </details>
      )}
    </div>
  );
}
