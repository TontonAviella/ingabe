import type { ExpressionSpecification, GeoJSONSource, MapMouseEvent, Map as MLMap } from 'maplibre-gl';
import { useEffect, useRef, useState } from 'react';
import { countMyCrop, type KeptOutline, type MyCropFound, type Place } from '@/hooks/useFieldTaps';
import type { ServerError } from '@/lib/serverError';
import { inside } from '@/lib/traceEdge';

// Only my crop, in one kept field: the reader taps a few plants of their crop, then a few green things that are
// not, and the server sorts the plants it found by which taps they look like (src/services/my_crop.py). Every
// count, limit and word about what was measured comes from the server; this component takes the taps, waits while
// the field is read, and draws the answer.

const SOURCE = 'my-crop';
const LAYERS = { points: 'my-crop-points', taps: 'my-crop-taps', label: 'my-crop-label' };
const LABEL_FONT = ['Open Sans Semibold'];
const EMPTY: GeoJSON.FeatureCollection = { type: 'FeatureCollection', features: [] };
export const MIN_TAPS = 3; // of each sort; the server's rule (my_crop.MIN_TAPS), which refuses fewer
export const MAX_TAPS = 10;
/** How long to wait before asking the server again while it reads a field; a test shortens it. */
export const timing = { askAgainMs: 4000 };
// my crop, other plants, other green: apart by colour and by size (my crop the largest ring)
const COLOURS = ['#FF4D1A', '#FFFFFF', '#2F80ED'];
const KIND_COLOUR: ExpressionSpecification = ['match', ['get', 'kind_code'], 0, COLOURS[0], 1, COLOURS[1], COLOURS[2]];

type Stage = 'crop' | 'other' | 'reading' | 'done';

function drawn(crop: Place[], other: Place[], found: MyCropFound | null): GeoJSON.FeatureCollection {
  const tap = (p: Place, mine: boolean): GeoJSON.Feature[] => [
    {
      type: 'Feature',
      geometry: { type: 'Point', coordinates: [p.lon, p.lat] },
      properties: { part: 'tap', colour: mine ? '#22c55e' : '#ef4444' },
    },
    // not by colour alone: a crop tap carries a plus, a not-my-crop tap a cross
    { type: 'Feature', geometry: { type: 'Point', coordinates: [p.lon, p.lat] }, properties: { part: 'label', label: mine ? '+' : '×' } },
  ];
  return {
    type: 'FeatureCollection',
    features: [...(found?.points.features ?? []), ...crop.flatMap((p) => tap(p, true)), ...other.flatMap((p) => tap(p, false))],
  };
}

function useDrawn(map: MLMap | null, data: GeoJSON.FeatureCollection) {
  useEffect(() => {
    if (!map) return;
    const apply = () => {
      if (!map.getSource(SOURCE)) {
        map.addSource(SOURCE, { type: 'geojson', data: EMPTY });
        map.addLayer({
          id: LAYERS.points,
          type: 'circle',
          source: SOURCE,
          filter: ['has', 'kind_code'],
          // rings, not dots: the plant shows inside its ring, so the reader can check each one against the photo
          paint: {
            'circle-color': KIND_COLOUR,
            'circle-opacity': 0.12,
            'circle-radius': [
              'interpolate',
              ['exponential', 2],
              ['zoom'],
              16,
              ['match', ['get', 'kind_code'], 0, 1, 0.6],
              20,
              ['match', ['get', 'kind_code'], 0, 7, 3.5],
              22,
              ['match', ['get', 'kind_code'], 0, 22, 10],
            ],
            'circle-stroke-color': KIND_COLOUR,
            'circle-stroke-width': ['interpolate', ['linear'], ['zoom'], 16, 0.6, 20, ['match', ['get', 'kind_code'], 0, 2.5, 1.5]],
          },
        });
        map.addLayer({
          id: LAYERS.taps,
          type: 'circle',
          source: SOURCE,
          filter: ['==', ['get', 'part'], 'tap'],
          paint: { 'circle-radius': 7, 'circle-color': ['get', 'colour'], 'circle-stroke-color': '#ffffff', 'circle-stroke-width': 2 },
        });
        map.addLayer({
          id: LAYERS.label,
          type: 'symbol',
          source: SOURCE,
          filter: ['==', ['get', 'part'], 'label'],
          layout: { 'text-field': ['get', 'label'], 'text-font': LABEL_FONT, 'text-size': 13, 'text-allow-overlap': true },
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
  // leaving the step takes its dots off the map
  useEffect(
    () => () => {
      (map?.getSource(SOURCE) as GeoJSONSource | undefined)?.setData(EMPTY);
    },
    [map],
  );
}

const count = (n: number) => n.toLocaleString('en');

export function MyCrop({ map, layerId, field, onClose }: { map: MLMap | null; layerId: string; field: KeptOutline; onClose: () => void }) {
  const [stage, setStage] = useState<Stage>('crop');
  const [crop, setCrop] = useState<Place[]>([]);
  const [other, setOther] = useState<Place[]>([]);
  const [found, setFound] = useState<MyCropFound | null>(null);
  const [progress, setProgress] = useState<{ done: number; total: number } | null>(null);
  const [problem, setProblem] = useState<string | null>(null);
  const alive = useRef(true);
  useEffect(
    () => () => {
      alive.current = false;
    },
    [],
  );
  useDrawn(map, drawn(crop, other, found));

  // Taps on the map while taps are being asked for. One outside the field is not taken: the server would refuse it.
  const taps = stage === 'crop' ? crop : other;
  useEffect(() => {
    if (!map || (stage !== 'crop' && stage !== 'other') || taps.length >= MAX_TAPS) return;
    const ring = field.geometry.coordinates[0].map(([x, y]) => ({ x, y }));
    const onClick = (e: MapMouseEvent) => {
      if (!inside(ring, { x: e.lngLat.lng, y: e.lngLat.lat })) {
        setProblem(`That tap is outside Field ${field.number}. Tap inside its line.`);
        return;
      }
      setProblem(null);
      (stage === 'crop' ? setCrop : setOther)((before) => [...before, { lon: e.lngLat.lng, lat: e.lngLat.lat }]);
    };
    map.on('click', onClick);
    map.getCanvas().style.cursor = 'crosshair';
    return () => {
      map.off('click', onClick);
      map.getCanvas().style.cursor = '';
    };
  }, [map, stage, taps.length, field]);

  // Ask, and while the server is still reading the field's squares ask again with the same taps.
  useEffect(() => {
    if (stage !== 'reading') return;
    let timer: ReturnType<typeof setTimeout> | undefined;
    let stopped = false;
    const ask = () =>
      countMyCrop(layerId, field.id, crop, other)
        .then((answer) => {
          if (stopped || !alive.current) return;
          if ('working' in answer) {
            setProgress(answer.total ? { done: answer.done, total: answer.total } : null);
            timer = setTimeout(ask, timing.askAgainMs);
            return;
          }
          setFound(answer);
          setStage('done');
        })
        .catch((error: ServerError) => {
          if (stopped || !alive.current) return;
          setProblem(error.message);
          setStage('other'); // the taps stay: the reader can take one back and tap again
        });
    void ask();
    return () => {
      stopped = true;
      clearTimeout(timer);
    };
  }, [stage, layerId, field.id, crop, other]);

  const button = 'min-h-11 px-3 rounded-[12px] text-[13px] font-semibold cursor-pointer disabled:opacity-50 sm:min-h-9';
  const plain = `${button} bg-panel-2 text-fg hover:bg-panel-3`;
  const strong = `${button} bg-caramel text-panel hover:bg-caramel-hi`;
  const again = () => {
    setCrop([]);
    setOther([]);
    setFound(null);
    setProblem(null);
    setProgress(null);
    setStage('crop');
  };

  return (
    <div className="flex flex-col gap-2" data-my-crop={stage}>
      <p role="status" aria-live="polite" className="m-0 text-[14px] font-semibold">
        {stage === 'crop' && `Tap ${MIN_TAPS} plants of your crop in Field ${field.number}. ${crop.length} tapped.`}
        {stage === 'other' &&
          `Now tap ${MIN_TAPS} green things that are not your crop: weeds, grass, other plants. ${other.length} tapped.`}
        {stage === 'reading' &&
          (progress
            ? `Reading Field ${field.number}: ${progress.done} of ${progress.total} parts. The first count of a field takes a few minutes; after that it is quick.`
            : `Reading Field ${field.number}. The first count of a field takes a few minutes; after that it is quick.`)}
        {stage === 'done' &&
          found &&
          `About ${count(found.my_crop)} plants of your crop in Field ${field.number}${found.per_ha ? ` (about ${count(found.per_ha)} a hectare)` : ''}.`}
      </p>
      {stage === 'done' && found && (
        <>
          <ul className="m-0 flex list-none flex-col gap-1 p-0 text-[13px] text-fg-soft">
            <li>
              <span aria-hidden className="mr-2 inline-block size-3 rounded-full align-middle" style={{ background: COLOURS[0] }} />
              Your crop: about {count(found.my_crop)}
            </li>
            <li>
              <span
                aria-hidden
                className="mr-2 inline-block size-2.5 rounded-full border border-black/60 align-middle"
                style={{ background: COLOURS[1] }}
              />
              Other plants: about {count(found.other_plants)}
            </li>
            <li>
              <span aria-hidden className="mr-2 inline-block size-2.5 rounded-full align-middle" style={{ background: COLOURS[2] }} />
              Other green (weeds, grass or bush; which is not known): {count(found.other_green)}
            </li>
          </ul>
          {found.counted_share < 0.995 && (
            <p className="m-0 text-[12.5px] text-fg-soft">
              {Math.round(found.counted_share * 100)}% of the field was counted. In the rest single plants cannot be told apart from above.
            </p>
          )}
          <details className="text-[12.5px] leading-snug text-fg-soft">
            <summary className="flex min-h-11 cursor-pointer items-center sm:min-h-9">How sure is this</summary>
            <p className="m-0 mt-1">{found.measured}</p>
          </details>
        </>
      )}
      {problem && (
        <p role="alert" className="m-0 text-[13px] text-caramel-hi">
          {problem}
        </p>
      )}
      <div className="flex flex-wrap items-center gap-2">
        {stage === 'crop' && (
          <button type="button" className={strong} disabled={crop.length < MIN_TAPS} onClick={() => setStage('other')}>
            Next
          </button>
        )}
        {stage === 'other' && (
          <button
            type="button"
            className={strong}
            disabled={other.length < MIN_TAPS}
            onClick={() => {
              setProblem(null);
              setStage('reading');
            }}
          >
            Count my crop
          </button>
        )}
        {(stage === 'crop' || stage === 'other') && (
          <button
            type="button"
            className={plain}
            disabled={taps.length === 0}
            onClick={() => {
              setProblem(null);
              (stage === 'crop' ? setCrop : setOther)((before) => before.slice(0, -1));
            }}
          >
            Undo tap
          </button>
        )}
        {stage !== 'crop' && (
          <button type="button" className={plain} onClick={again}>
            Tap again
          </button>
        )}
        <button type="button" className={plain} onClick={onClose}>
          Close
        </button>
      </div>
    </div>
  );
}
