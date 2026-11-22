import type { GeoJSONSource, MapMouseEvent, Map as MLMap } from 'maplibre-gl';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { drawerPadding } from '@/components/MapDrawer';
import {
  type CheckedSquare,
  type CountChecked,
  type KeptOutline,
  keepCountCheck,
  type Place,
  type SquareToCheck,
  squareToCheck,
} from '@/hooks/useFieldTaps';
import { boundsOf } from '@/lib/outlineBounds';
import type { ServerError } from '@/lib/serverError';
import { inside } from '@/lib/traceEdge';

// Check a square: the reader checks the plant count on their own field. The server picks one part of the field
// (src/services/count_check.py; the reader does not choose it) and gives the plants the count holds there. The
// reader taps a ring that is not a plant, and a plant that got no ring. What the app counted, what the reader
// counted and every word about what that means come from the server; this component takes the taps and draws them.

const SOURCE = 'count-check';
const LAYERS = { part: 'count-check-part', rings: 'count-check-rings', label: 'count-check-label' };
const LABEL_FONT = ['Open Sans Semibold'];
const EMPTY: GeoJSON.FeatureCollection = { type: 'FeatureCollection', features: [] };
/** A tap this close to a ring (screen pixels) is a tap on that ring. */
export const HIT_PX = 18;
// the app's ring, a ring the reader says is not a plant, a plant the reader added
const COLOURS = { app: '#FF4D1A', struck: '#9CA3AF', added: '#FFFFFF' };

type Mark = { wrong: number } | { added: Place };
type Stage = 'loading' | 'marking' | 'saving' | 'saved';

/** The polygons of the part, each as its rings (the outer line first, then any holes). */
function polygons(part: SquareToCheck['part']): [number, number][][][] {
  return (part.type === 'Polygon' ? [part.coordinates] : part.coordinates) as [number, number][][][];
}

/** Whether a place lies in the part: inside a polygon's outer line and outside its holes. */
function within(part: SquareToCheck['part'], lon: number, lat: number): boolean {
  const at = { x: lon, y: lat };
  const holds = (ring: [number, number][]) =>
    inside(
      ring.map(([x, y]) => ({ x, y })),
      at,
    );
  return polygons(part).some(([outer, ...holes]) => outer && holds(outer) && !holes.some(holds));
}

function drawn(shown: SquareToCheck | null, struck: Set<number>, added: Place[]): GeoJSON.FeatureCollection {
  if (!shown) return EMPTY;
  const point = (lon: number, lat: number, properties: Record<string, unknown>): GeoJSON.Feature => ({
    type: 'Feature',
    geometry: { type: 'Point', coordinates: [lon, lat] },
    properties,
  });
  return {
    type: 'FeatureCollection',
    features: [
      { type: 'Feature', geometry: shown.part, properties: { part: true } },
      // not by colour alone: a ring that is not a plant carries a cross, an added plant a plus
      ...shown.plants.map(([lon, lat], n) =>
        struck.has(n) ? point(lon, lat, { colour: COLOURS.struck, label: '×' }) : point(lon, lat, { colour: COLOURS.app, label: '' }),
      ),
      ...added.map((p) => point(p.lon, p.lat, { colour: COLOURS.added, label: '+' })),
    ],
  };
}

function useDrawn(map: MLMap | null, data: GeoJSON.FeatureCollection) {
  useEffect(() => {
    if (!map) return;
    const apply = () => {
      if (!map.getSource(SOURCE)) {
        map.addSource(SOURCE, { type: 'geojson', data: EMPTY });
        map.addLayer({
          id: LAYERS.part,
          type: 'line',
          source: SOURCE,
          filter: ['has', 'part'],
          paint: { 'line-color': '#FFFFFF', 'line-width': 2.5, 'line-dasharray': [2, 1.5] },
        });
        map.addLayer({
          id: LAYERS.rings,
          type: 'circle',
          source: SOURCE,
          filter: ['has', 'colour'],
          // rings, not dots: the plant shows inside its ring, which is what the reader judges
          paint: {
            'circle-color': ['get', 'colour'],
            'circle-opacity': 0.08,
            'circle-radius': ['interpolate', ['exponential', 2], ['zoom'], 18, 3, 20, 8, 22, 22],
            'circle-stroke-color': ['get', 'colour'],
            'circle-stroke-width': 2.5,
          },
        });
        map.addLayer({
          id: LAYERS.label,
          type: 'symbol',
          source: SOURCE,
          filter: ['has', 'colour'],
          layout: { 'text-field': ['get', 'label'], 'text-font': LABEL_FONT, 'text-size': 15, 'text-allow-overlap': true },
          paint: { 'text-color': '#FFF7EC', 'text-halo-color': '#0B0908', 'text-halo-width': 1.6 },
        });
      }
      (map.getSource(SOURCE) as GeoJSONSource).setData(data);
    };
    if (map.isStyleLoaded()) apply();
    else map.once('idle', apply);
    map.on('style.load', apply);
    return () => {
      map.off('style.load', apply);
      map.off('idle', apply);
    };
  }, [map, data]);
  // leaving the check takes its rings off the map
  useEffect(
    () => () => {
      (map?.getSource(SOURCE) as GeoJSONSource | undefined)?.setData(EMPTY);
    },
    [map],
  );
}

const count = (n: number) => n.toLocaleString('en');
const plants = (n: number) => `${count(n)} plant${n === 1 ? '' : 's'}`;

export function CountCheck({
  map,
  layerId,
  field,
  onSaved,
  onClose,
}: {
  map: MLMap | null;
  layerId: string;
  field: KeptOutline;
  /** A check was kept on the server. */
  onSaved: () => void;
  onClose: () => void;
}) {
  const [stage, setStage] = useState<Stage>('loading');
  const [shown, setShown] = useState<SquareToCheck | null>(null);
  const [marks, setMarks] = useState<Mark[]>([]); // in the order tapped, so Undo takes the last one back
  const [saved, setSaved] = useState<{ here: CheckedSquare; checked: CountChecked | null; measured: string } | null>(null);
  const [problem, setProblem] = useState<string | null>(null);
  const asking = useRef(0);

  const struck = useMemo(() => new Set(marks.flatMap((m) => ('wrong' in m ? [m.wrong] : []))), [marks]);
  const added = useMemo(() => marks.flatMap((m) => ('added' in m ? [m.added] : [])), [marks]);
  const data = useMemo(() => drawn(stage === 'loading' ? null : shown, struck, added), [stage, shown, struck, added]);
  useDrawn(map, data);

  // Ask the server for the part to check next. There is no passing over a part: the next one comes when this
  // one's check is kept.
  const ask = useCallback(() => {
    const mine = ++asking.current;
    setProblem(null);
    squareToCheck(layerId, field.id)
      .then((answer) => {
        if (mine !== asking.current) return;
        setShown(answer);
        setMarks([]);
        setSaved(null);
        setStage('marking');
      })
      .catch((error: ServerError) => {
        if (mine === asking.current) setProblem(error.message); // what is on screen stays as it was
      });
  }, [layerId, field.id]);
  useEffect(() => {
    setStage('loading');
    setShown(null);
    setMarks([]);
    setSaved(null);
    ask();
    return () => {
      asking.current += 1; // an answer still on its way belongs to a check that was closed
    };
  }, [ask]);

  // The map goes to the part shown, close enough to judge each ring.
  useEffect(() => {
    if (!map || !shown) return;
    const box = boundsOf(polygons(shown.part).flat(2));
    if (!box) return;
    // the panel sits over the lower part of the map: the part being checked is fitted into what is left above it
    const tall = map.getCanvas().clientHeight || 0;
    map.fitBounds(box, {
      padding: drawerPadding() ?? { top: 70, bottom: Math.max(260, Math.round(tall * 0.55)), left: 40, right: 40 },
      maxZoom: 23,
      duration: 600,
    });
  }, [map, shown]);

  // A tap on a ring says "not a plant" (again: it is one after all); a tap on a plant the reader added takes it
  // away; a tap anywhere else in the part adds a plant.
  useEffect(() => {
    if (!map || stage !== 'marking' || !shown) return;
    const onClick = (e: MapMouseEvent) => {
      const at = map.project([e.lngLat.lng, e.lngLat.lat]);
      const near = (lon: number, lat: number) => {
        const p = map.project([lon, lat]);
        return Math.hypot(p.x - at.x, p.y - at.y);
      };
      let best: { d: number; mark: Mark } | null = null;
      shown.plants.forEach(([lon, lat], n) => {
        const d = near(lon, lat);
        if (d <= HIT_PX && (!best || d < best.d)) best = { d, mark: { wrong: n } };
      });
      for (const p of added) {
        const d = near(p.lon, p.lat);
        if (d <= HIT_PX && (!best || d < best.d)) best = { d, mark: { added: p } };
      }
      setProblem(null);
      const hit = (best as { d: number; mark: Mark } | null)?.mark;
      if (hit && 'wrong' in hit) {
        const n = hit.wrong;
        setMarks((before) =>
          before.some((m) => 'wrong' in m && m.wrong === n) ? before.filter((m) => !('wrong' in m && m.wrong === n)) : [...before, hit],
        );
        return;
      }
      if (hit && 'added' in hit) {
        const p = hit.added;
        setMarks((before) => before.filter((m) => !('added' in m && m.added === p)));
        return;
      }
      if (!within(shown.part, e.lngLat.lng, e.lngLat.lat)) {
        setProblem('That tap is outside the part being checked. Tap inside the dashed line.');
        return;
      }
      setMarks((before) => [...before, { added: { lon: e.lngLat.lng, lat: e.lngLat.lat } }]);
    };
    map.on('click', onClick);
    map.getCanvas().style.cursor = 'crosshair';
    return () => {
      map.off('click', onClick);
      map.getCanvas().style.cursor = '';
    };
  }, [map, stage, shown, added]);

  const save = () => {
    if (!shown) return;
    const mine = ++asking.current;
    setStage('saving');
    setProblem(null);
    keepCountCheck(layerId, field.id, shown.square, shown.plants_mark, [...struck], added)
      .then((answer) => {
        onSaved(); // kept on the server, whatever happens to this panel
        if (mine !== asking.current) return;
        setSaved(answer);
        setStage('saved');
      })
      .catch((error: ServerError) => {
        if (mine !== asking.current) return;
        setProblem(error.message);
        setStage('marking'); // the marks stay: the reader can change one and save again
      });
  };

  const button = 'min-h-11 px-3 rounded-[12px] text-[13px] font-semibold cursor-pointer disabled:opacity-50 sm:min-h-9';
  const plain = `${button} bg-panel-2 text-fg hover:bg-panel-3`;
  const strong = `${button} bg-caramel text-panel hover:bg-caramel-hi`;

  return (
    <div className="flex flex-col gap-2" data-count-check={stage}>
      <p role="status" aria-live="polite" className="m-0 text-[14px] font-semibold">
        {stage === 'loading' && !problem && `Choosing a part of Field ${field.number} for you to check. This can take half a minute.`}
        {(stage === 'marking' || stage === 'saving') &&
          shown &&
          `The app ringed ${plants(shown.app)} in this part of Field ${field.number}. Is every ring on a plant, and every plant in a ring?`}
        {stage === 'saved' &&
          saved &&
          `In this part the app counted ${plants(saved.here.app)} and you counted ${count(saved.here.you)}: ${count(saved.here.wrong)} of its rings ${
            saved.here.wrong === 1 ? 'was not a plant' : 'were not plants'
          }, and it missed ${count(saved.here.missed)}.`}
      </p>
      {(stage === 'marking' || stage === 'saving') && shown && (
        <ul className="m-0 flex list-none flex-col gap-1 p-0 text-[13px] text-fg-soft">
          <li>Tap a ring that is not on a plant of your crop: it goes grey, with a ×. {count(struck.size)} marked.</li>
          <li>Tap the middle of a plant that has no ring: it gets a white ring, with a +. {count(added.length)} added.</li>
          <li>Only inside the dashed line. The app chose this part, 1 of {shown.squares_in_field} in the field.</li>
        </ul>
      )}
      {stage === 'saved' && saved && (
        <>
          {saved.checked && saved.checked.parts > 1 && <p className="m-0 text-[13px] text-fg-soft">{saved.checked.said}</p>}
          <details className="text-[12.5px] leading-snug text-fg-soft">
            <summary className="flex min-h-11 cursor-pointer items-center sm:min-h-9">What this tells you</summary>
            <p className="m-0 mt-1">{saved.measured}</p>
          </details>
        </>
      )}
      {problem && (
        <p role="alert" className="m-0 text-[13px] text-caramel-hi">
          {problem}
        </p>
      )}
      <div className="flex flex-wrap items-center gap-2">
        {(stage === 'marking' || stage === 'saving') && (
          <>
            <button type="button" className={strong} disabled={stage === 'saving'} onClick={save}>
              Save my count
            </button>
            <button
              type="button"
              className={plain}
              disabled={marks.length === 0 || stage === 'saving'}
              onClick={() => {
                setProblem(null);
                setMarks((before) => before.slice(0, -1));
              }}
            >
              Undo
            </button>
          </>
        )}
        {stage === 'saved' && (
          <button type="button" className={strong} onClick={ask}>
            Check the next part
          </button>
        )}
        <button type="button" className={plain} onClick={onClose}>
          Close
        </button>
      </div>
    </div>
  );
}
