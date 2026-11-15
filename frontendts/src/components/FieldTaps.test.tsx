import { apiFetch } from '@mundi/ee';
import type { Map as MLMap } from 'maplibre-gl';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { FieldTaps } from '@/components/FieldTaps';
import { timing } from '@/components/MyCrop';
import type { KeptOutline, Tap } from '@/hooks/useFieldTaps';
import type { MapLayer } from '@/lib/types';
import { byButton, byText, click, mount, unmountAll } from '@/test/dom';

// The server is a fake of src/routes/field_tap_routes.py, its rules copied from that file and
// src/services/field_taps.py: one tap gives up to three outlines (choices 0, 1, 2); more taps need a pick and give
// that one outline; a kept outline is computed from the taps and the pick; at most `max_corrections` corrections.
vi.mock('@mundi/ee', () => ({ apiFetch: vi.fn() }));
const fetchMock = vi.mocked(apiFetch);

const LAYER = 'L1';
const MEASURED = 'Measured on one photo: 25 of 26 fields.';
const square = (size: number): GeoJSON.Polygon => ({
  type: 'Polygon',
  coordinates: [
    [
      [30, -2],
      [30 + size, -2],
      [30 + size, -2 + size],
      [30, -2 + size],
      [30, -2],
    ],
  ],
});

function fakeServer(prepared: boolean) {
  const kept: KeptOutline[] = [];
  const asked: { taps: Tap[]; pick: number | null }[] = [];
  const turnedDown: { taps: Tap[]; pick: number | null; reason: string }[] = [];
  const measured: { first_tap: Tap; moved: [number, number][] }[] = [];
  // only my crop: what was asked, and the answers the server gives in turn (the last one is given again after that)
  const cropAsked: { url: string; crop: { lon: number; lat: number }[]; other: { lon: number; lat: number }[] }[] = [];
  const cropAnswers: { status?: number; body: unknown }[] = [];
  // check a square: what was asked, and the checks kept
  const checkAsked: { url: string; seen?: [number, number][]; square?: [number, number]; wrong?: number[]; missed?: unknown[] }[] = [];
  const checks: { app: number; you: number; wrong: number; missed: number }[] = [];
  const outlines = (taps: Tap[], pick: number | null) =>
    taps.length === 1
      ? [0, 1, 2].map((choice) => ({ choice, geometry: square(choice + 1), area_ha: 0.1 * (choice + 1) }))
      : [{ choice: pick as number, geometry: square(9), area_ha: 0.1 * ((pick as number) + 1) + 0.05 * (taps.length - 1) }];
  const json = (body: unknown, status = 200) =>
    new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } });
  fetchMock.mockImplementation(async (input, init) => {
    const url = String(input);
    const body = init?.body ? JSON.parse(String(init.body)) : null;
    if (url === `/api/layer/${LAYER}/field-taps`)
      return json({
        prepared,
        windows: prepared ? 4 : 0,
        max_corrections: 2,
        measured: MEASURED,
        cover_measured: 'Green cover is the share of the field under living green plants of any kind.',
        kept,
        turned_down: turnedDown.length,
      });
    if (url.endsWith('/moved-line')) {
      measured.push(body);
      // the fake's rules: a line pulled east of longitude 31.5 is refused, as the server refuses one that leaves the
      // first tap outside; a line with a corner north of latitude -0.75 comes back tidied, as a crossed one does
      if (body.moved.some((c: [number, number]) => c[0] > 31.5))
        return json({ detail: 'The outline no longer covers the spot you first tapped. Use Undo last.' }, 422);
      if (body.moved.some((c: [number, number]) => c[1] > -0.75)) return json({ area_ha: 0.33, geometry: square(3), tidied: true });
      return json({ area_ha: 0.42, geometry: { type: 'Polygon', coordinates: [[...body.moved, body.moved[0]]] }, tidied: false });
    }
    if (url.endsWith('/my-crop')) {
      cropAsked.push({ url, ...body });
      const answer = cropAnswers.length > 1 ? cropAnswers.shift() : cropAnswers[0];
      return answer ? json(answer.body, answer.status ?? 200) : json({ detail: 'Not Found' }, 404);
    }
    if (url.endsWith('/count-check/square')) {
      checkAsked.push({ url, ...body });
      // the fake's rule: one part to show; once it was passed over there is no other
      if (body.seen.length) return json({ detail: 'Every part of this field where plants are counted has been shown.' }, 422);
      return json({
        square: [4, 2],
        plants: [
          [30.2, -1.5],
          [30.4, -1.5],
          [30.6, -1.5],
        ],
        app: 3,
        part: square(1),
        side_m: 12.5,
        squares_in_field: 13,
        checked: null,
        measured: 'Your own count, on a part the app chose.',
      });
    }
    if (url.endsWith('/count-check') && init?.method === 'POST') {
      checkAsked.push({ url, ...body });
      if (body.missed.length > 2) return json({ detail: 'A plant you added lies outside this part of the field.' }, 422);
      const here = { app: 3, wrong: body.wrong.length, missed: body.missed.length, you: 3 - body.wrong.length + body.missed.length };
      checks.push(here);
      const said = `In the 1 of 13 parts you checked, the app counted 3 plants and you counted ${here.you}.`;
      return json({ here, checked: { parts: 1, ...here, said }, measured: 'Your own count, on a part the app chose.' });
    }
    if (url.endsWith('/count-check')) {
      const last = checks.at(-1);
      return json({ checked: last ? { parts: 1, ...last, said: `You counted ${last.you} where the app counted 3.` } : null, measured: '' });
    }
    if (url.endsWith('/turned-down')) {
      turnedDown.push(body);
      return json({ id: `t${turnedDown.length}`, ...body });
    }
    if (url.endsWith('/outline')) {
      asked.push(body);
      if (body.taps.length > 1 && body.pick === null) return json({ detail: 'Pick one of the outlines before correcting it.' }, 422);
      return json({ outlines: outlines(body.taps, body.pick) });
    }
    if (url.endsWith('/kept') && init?.method === 'POST') {
      const row = {
        id: `k${kept.length + 1}`,
        number: kept.length + 1,
        centre: [30.5, -1.5],
        ...outlines(body.taps, body.pick).find((o) => o.choice === body.pick),
        ...(body.moved ? { area_ha: 0.42, by_hand: true, moved: body.moved } : {}),
        corrections: body.taps.length - 1,
        kept_at: 'now',
      } as KeptOutline;
      kept.push(row);
      return json(row);
    }
    return json({ detail: 'Not Found' }, 404);
  });
  return { kept, asked, turnedDown, measured, cropAsked, cropAnswers, checkAsked };
}

/** The few things the component asks of the map; `tap` plays a reader's tap on the photo. */
function fakeMap() {
  const handlers = new Map<string, ((e: unknown) => void)[]>();
  // what each source of the map was last given, by the source's name
  const sources = new Map<string, GeoJSON.FeatureCollection>();
  const source = (id: string) => ({ setData: (d: GeoJSON.FeatureCollection) => void sources.set(id, d) });
  let under: string | null = null;
  const canvas = { style: { cursor: '' } as CSSStyleDeclaration };
  const map = {
    queryRenderedFeatures: () => (under ? [{ properties: { id: under } }] : []),
    easeTo: vi.fn(),
    fitBounds: vi.fn(),
    // one degree is 1000 px on this map
    project: (c: [number, number]) => ({ x: c[0] * 1000, y: -c[1] * 1000 }),
    unproject: (p: [number, number]) => ({ lng: p[0] / 1000, lat: -p[1] / 1000 }),
    on: (name: string, fn: (e: unknown) => void) => handlers.set(name, [...(handlers.get(name) ?? []), fn]),
    off: (name: string, fn: (e: unknown) => void) =>
      handlers.set(
        name,
        (handlers.get(name) ?? []).filter((h) => h !== fn),
      ),
    once: vi.fn(),
    isStyleLoaded: () => true,
    getSource: (id: string) => (sources.has(id) ? source(id) : undefined),
    addSource: (id: string) => void sources.set(id, { type: 'FeatureCollection', features: [] }),
    addLayer: vi.fn(),
    getCanvas: () => canvas,
  };
  return {
    map: map as unknown as MLMap,
    tap: (lng: number, lat: number) => {
      for (const fn of handlers.get('click') ?? []) fn({ lngLat: { lng, lat } });
    },
    /** True while the screen waits for a tap on the photo (it shows the crosshair then). */
    listening: () => canvas.style.cursor === 'crosshair',
    /** Press at the first place, draw through the others, let go. True when the press took hold of the line (the
     * map held still). */
    draw: (...path: [number, number][]) => {
      const preventDefault = vi.fn();
      const event = (c: [number, number]) => ({
        lngLat: { lng: c[0], lat: c[1] },
        point: { x: c[0] * 1000, y: -c[1] * 1000 },
        preventDefault,
      });
      for (const fn of handlers.get('mousedown') ?? []) fn(event(path[0]));
      for (const c of path.slice(1)) for (const fn of handlers.get('mousemove') ?? []) fn(event(c));
      for (const fn of handlers.get('mouseup') ?? []) fn(event(path[path.length - 1]));
      return preventDefault.mock.calls.length > 0;
    },
    /** One map event of this kind at this place; `buttons` is how many mouse buttons are held down. */
    fire: (name: string, c: [number, number], buttons = 1) => {
      const e = {
        lngLat: { lng: c[0], lat: c[1] },
        point: { x: c[0] * 1000, y: -c[1] * 1000 },
        preventDefault: vi.fn(),
        originalEvent: { buttons },
      };
      for (const fn of handlers.get(name) ?? []) fn(e);
    },
    /** A click on the map, on the kept field with this id or on bare ground (null). */
    clickOn: (id: string | null) => {
      under = id;
      for (const fn of handlers.get('click') ?? []) fn({ lngLat: { lng: 30.5, lat: -1.5 }, point: { x: 0, y: 0 } });
    },
    drawn: () => sources.get('field-taps') ?? { type: 'FeatureCollection', features: [] },
    /** What the only-my-crop step has on the map: its taps and the plants it found. */
    cropDrawn: () => sources.get('my-crop')?.features ?? [],
    /** What the check of the count has on the map: the part, the app's rings and the reader's marks. */
    checkDrawn: () => sources.get('count-check')?.features ?? [],
    fitted: () => map.fitBounds.mock.calls.length,
  };
}

const photo = { id: LAYER, type: 'raster' } as MapLayer;
afterEach(() => {
  timing.askAgainMs = 4000;
  unmountAll();
  fetchMock.mockReset();
});

describe('outlining a field by tapping it', () => {
  it('shows nothing on a photo that was not prepared', async () => {
    fakeServer(false);
    const { map } = fakeMap();
    mount(<FieldTaps map={map} layers={[photo]} hiddenLayerIDs={[]} />);
    await vi.waitFor(() => expect(fetchMock).toHaveBeenCalled());
    await new Promise((r) => setTimeout(r, 20));
    expect(byButton('Outline a field')).toBeNull();
  });

  it('offers three outlines, corrects the picked one, goes back, and keeps what the server computed', async () => {
    const server = fakeServer(true);
    const m = fakeMap();
    mount(<FieldTaps map={m.map} layers={[photo]} hiddenLayerIDs={[]} />);
    await vi.waitFor(() => expect(byButton('Outline a field')).not.toBeNull());
    // closed, the panel is one small row: what was measured shows once it is opened
    expect(byText(MEASURED)).toBeNull();
    expect(m.listening()).toBe(false);

    click(byButton('Outline a field') as HTMLButtonElement);
    await vi.waitFor(() => expect(m.listening()).toBe(true));
    expect(byText(MEASURED)).not.toBeNull();
    m.tap(30.1, -1.9);
    await vi.waitFor(() => expect(byButton(/^B · 0\.20 ha$/)).not.toBeNull());
    expect(m.drawn().features.filter((f) => f.geometry.type === 'Polygon')).toHaveLength(3);
    // while the three outlines show, a tap on the map does nothing: the reader picks first
    expect(m.listening()).toBe(false);

    click(byButton(/^B · /) as HTMLButtonElement);
    await vi.waitFor(() => expect(byText('Your field: 0.20 ha')).not.toBeNull());
    expect(m.drawn().features.filter((f) => f.geometry.type === 'Polygon')).toHaveLength(1);

    click(byButton('Not this part') as HTMLButtonElement);
    await vi.waitFor(() => expect(m.listening()).toBe(true));
    m.tap(30.2, -1.9);
    await vi.waitFor(() => expect(byText('Your field: 0.25 ha')).not.toBeNull());
    expect(server.asked.at(-1)).toEqual({
      taps: [
        { lon: 30.1, lat: -1.9, add: true },
        { lon: 30.2, lat: -1.9, add: false },
      ],
      pick: 1,
    });

    click(byButton('Back') as HTMLButtonElement);
    await vi.waitFor(() => expect(byText('Your field: 0.20 ha')).not.toBeNull());

    click(byButton('Keep') as HTMLButtonElement);
    // the kept fields fold away behind their count, so a long list never covers the photo
    await vi.waitFor(() => expect(byButton(/^1 field outlined · show$/)).not.toBeNull());
    expect(byText(/Field 1 · 0\.20 ha/)).toBeNull();
    click(byButton(/^1 field outlined · show$/) as HTMLButtonElement);
    await vi.waitFor(() => expect(byText(/Field 1 · 0\.20 ha/)).not.toBeNull());
    expect(server.kept).toHaveLength(1);
    expect(server.kept[0].corrections).toBe(0);
    // a kept field is not a turned-down one, and going back a step is not giving up
    expect(server.turnedDown).toEqual([]);
  });

  it('lets the reader pull the line to the true edge, undo a pull, and keeps it marked as moved by hand', async () => {
    const server = fakeServer(true);
    const m = fakeMap();
    mount(<FieldTaps map={m.map} layers={[photo]} hiddenLayerIDs={[]} />);
    await vi.waitFor(() => expect(byButton('Outline a field')).not.toBeNull());
    click(byButton('Outline a field') as HTMLButtonElement);
    await vi.waitFor(() => expect(m.listening()).toBe(true));
    m.tap(30.1, -1.9);
    await vi.waitFor(() => expect(byButton(/^A · /)).not.toBeNull());
    click(byButton(/^A · /) as HTMLButtonElement); // the square from (30, -2) to (31, -1)
    await vi.waitFor(() => expect(byButton('Move the line')).not.toBeNull());
    click(byButton('Move the line') as HTMLButtonElement);
    await vi.waitFor(() => expect(byText('Press on the line and pull it to the true edge. Let go anywhere.')).not.toBeNull());
    const has = (ring: number[][], lng: number, lat: number, within = 1e-6) =>
      ring.some((c) => Math.abs(c[0] - lng) < within && Math.abs(c[1] - lat) < within);
    const east = (ring: number[][]) => Math.max(...ring.map((c) => c[0]));
    expect((byButton('Undo last') as HTMLButtonElement).disabled).toBe(true);

    // a press away from the line moves nothing and leaves the map free to pan
    expect(m.draw([30.5, -1.5], [30.6, -1.5])).toBe(false);
    expect(server.measured).toEqual([]);

    // press the east side and pull it 100 px out, letting go in open ground: the map holds still, that stretch of
    // the side follows the finger, the far side stays, and the server measures the new line
    expect(m.draw([31, -1.5], [31.05, -1.5], [31.1, -1.5])).toBe(true);
    await vi.waitFor(() => expect(server.measured).toHaveLength(1));
    const moved = server.measured[0].moved;
    expect(has(moved, 31.1, -1.5)).toBe(true); // the pressed place went where the finger went
    expect(east(moved)).toBeCloseTo(31.1, 6); // and nothing went further
    // the west side's corners are exactly where they were, to the last digit
    expect(moved.some((c) => c[0] === 30 && c[1] === -2) && moved.some((c) => c[0] === 30 && c[1] === -1)).toBe(true);
    expect(server.measured[0].first_tap.lon).toBe(30.1);
    await vi.waitFor(() => expect(byText('0.42 ha')).not.toBeNull());

    // a press a finger's width off the line still takes hold of it (20 px away), and pulls it inwards
    expect(m.draw([30.5, -2.02], [30.5, -1.95])).toBe(true);
    await vi.waitFor(() => expect(server.measured).toHaveLength(2));
    expect(Math.min(...server.measured[1].moved.map((c) => c[1]))).toBeCloseTo(-2, 6); // the corners of that side stayed
    expect(has(server.measured[1].moved, 30.5, -1.93)).toBe(true); // its middle came in by the 70 px of the pull

    // "Undo last" takes back one pull, and the server measures the line that is back
    click(byButton('Undo last') as HTMLButtonElement);
    await vi.waitFor(() => expect(server.measured).toHaveLength(3));
    expect(server.measured[2].moved).toEqual(moved);

    // a line the server refuses cannot be kept, and its reason is shown word for word
    m.draw([31.1, -1.5], [31.7, -1.5]);
    await vi.waitFor(() => expect(byText('The outline no longer covers the spot you first tapped. Use Undo last.')).not.toBeNull());
    click(byButton('Done') as HTMLButtonElement);
    await vi.waitFor(() => expect((byButton('Keep') as HTMLButtonElement).disabled).toBe(true));
    // corrections by tap are for the model's outline, not for a hand-moved one
    expect((byButton('This part too') as HTMLButtonElement).disabled).toBe(true);

    // Back drops the pulled line and returns the outline as the model drew it
    click(byButton('Back') as HTMLButtonElement);
    await vi.waitFor(() => expect(byText('Your field: 0.10 ha')).not.toBeNull());

    click(byButton('Move the line') as HTMLButtonElement);
    await vi.waitFor(() => expect(byText('Press on the line and pull it to the true edge. Let go anywhere.')).not.toBeNull());
    m.draw([30.5, -1], [30.5, -0.8]);
    await vi.waitFor(() => expect(server.measured).toHaveLength(5));
    click(byButton('Done') as HTMLButtonElement);
    await vi.waitFor(() => expect(byText('Your field: 0.42 ha · line moved by hand')).not.toBeNull());
    click(byButton('Keep') as HTMLButtonElement);
    await vi.waitFor(() => expect(server.kept).toHaveLength(1));
    expect(has((server.kept[0] as KeptOutline & { moved: number[][] }).moved, 30.5, -0.8)).toBe(true);
    click((await vi.waitFor(() => byButton(/^1 field outlined · show$/))) as HTMLButtonElement);
    await vi.waitFor(() => expect(byText(/Field 1 · 0\.42 ha · by hand/)).not.toBeNull());
  });

  it('keeps a pull wherever it is let go, ignores a press with no pull, and ends a pull when the button is let go off the map', async () => {
    const server = fakeServer(true);
    const m = fakeMap();
    mount(<FieldTaps map={m.map} layers={[photo]} hiddenLayerIDs={[]} />);
    await vi.waitFor(() => expect(byButton('Outline a field')).not.toBeNull());
    click(byButton('Outline a field') as HTMLButtonElement);
    await vi.waitFor(() => expect(m.listening()).toBe(true));
    m.tap(30.1, -1.9);
    await vi.waitFor(() => expect(byButton(/^A · /)).not.toBeNull());
    click(byButton(/^A · /) as HTMLButtonElement); // the square from (30, -2) to (31, -1)
    await vi.waitFor(() => expect(byButton('Move the line')).not.toBeNull());
    click(byButton('Move the line') as HTMLButtonElement);
    await vi.waitFor(() => expect(byText('Press on the line and pull it to the true edge. Let go anywhere.')).not.toBeNull());
    const sq = square; // the fake server's square of a given size
    const drawnNow = () => m.drawn().features.find((f) => f.geometry.type === 'Polygon')?.geometry as GeoJSON.Polygon;
    const before = JSON.stringify(drawnNow());

    // a press on the line that does not move is not a pull: nothing changes and nothing is measured
    m.draw([31, -1.7], [31.001, -1.7]);
    expect(JSON.stringify(drawnNow())).toBe(before);
    expect(server.measured).toEqual([]);
    expect((byButton('Undo last') as HTMLButtonElement).disabled).toBe(true);

    // let go 200 px from the line: the pull stands, there is no place it has to end
    m.draw([31, -1.7], [31.1, -1.6], [31.2, -1.5]);
    await vi.waitFor(() => expect(server.measured).toHaveLength(1));
    expect(document.querySelector('[role="alert"]')).toBeNull();
    expect((byButton('Undo last') as HTMLButtonElement).disabled).toBe(false);

    // the button let go over a panel: the map sees no release, only the next move with no button held
    m.fire('mousedown', [30.5, -1]);
    m.fire('mousemove', [30.5, -0.9]);
    m.fire('mousemove', [35, -5], 0); // the pointer wanders with no button down: the pull is over, not carried on
    await vi.waitFor(() => expect(server.measured).toHaveLength(2));
    expect(server.measured[1].moved.some((c) => c[0] > 34)).toBe(false);
    m.fire('mousemove', [36, -6], 0);
    expect(server.measured).toHaveLength(2);
    // a pull that made the line cross itself comes back tidied: the tidied line is drawn, its area shown, Keep is
    // on, and the reader is told of no problem
    m.draw([30.5, -0.9], [30.5, -0.6]); // the north side sits at -0.9 here since the pull before
    await vi.waitFor(() => expect(byText('0.33 ha')).not.toBeNull());
    // the drawn line is the server's tidied one (drawn closed, as every outline is)
    await vi.waitFor(() => expect(JSON.stringify(drawnNow().coordinates[0])).toBe(JSON.stringify(sq(3).coordinates[0])));
    expect(document.querySelector('[role="alert"]')).toBeNull();
    click(byButton('Undo last') as HTMLButtonElement); // and the pull can still be taken back
    await vi.waitFor(() => expect(server.measured).toHaveLength(4));
    // and a release on the window itself ends a pull too
    m.fire('mousedown', [30, -1.5]);
    m.fire('mousemove', [29.9, -1.5]);
    window.dispatchEvent(new Event('mouseup'));
    await vi.waitFor(() => expect(server.measured).toHaveLength(5));
  });

  it('sits at the bottom left while idle, clear of where Sage answers, and takes the centre once a field is outlined or picked', async () => {
    const server = fakeServer(true);
    server.kept.push({ id: 'a', number: 3, centre: [30.5, -1.5], geometry: square(1), area_ha: 0.18, corrections: 0, kept_at: 'then' });
    const m = fakeMap();
    mount(<FieldTaps map={m.map} layers={[photo]} hiddenLayerIDs={[]} />);
    await vi.waitFor(() => expect(byButton('Outline a field')).not.toBeNull());
    const panel = () => (byButton('Outline a field') ?? byButton('Close'))?.closest('[data-docked]') as HTMLElement;
    expect(panel().dataset.docked).toBe('true');
    expect(panel().className).toContain('z-40'); // above Sage's answer, which a narrow screen stretches to the corner
    expect(panel().className).toContain('left-4');
    m.clickOn('a'); // a picked field brings its row and the box to ask Sage: centre
    await vi.waitFor(() => expect(panel().dataset.docked).toBe('false'));
    m.clickOn(null);
    await vi.waitFor(() => expect(panel().dataset.docked).toBe('true'));
    click(byButton('Outline a field') as HTMLButtonElement);
    await vi.waitFor(() => expect(m.listening()).toBe(true));
    expect(panel().dataset.docked).toBe('false');
    expect(panel().className).toContain('left-1/2');
  });

  it('folds the panel to its one line of buttons and opens it again', async () => {
    fakeServer(true);
    const m = fakeMap();
    localStorage.removeItem('ingabe.fieldTapsFolded');
    mount(<FieldTaps map={m.map} layers={[photo]} hiddenLayerIDs={[]} />);
    await vi.waitFor(() => expect(byButton('Outline a field')).not.toBeNull());
    click(byButton('Outline a field') as HTMLButtonElement);
    await vi.waitFor(() => expect(m.listening()).toBe(true));
    expect(byText(MEASURED)).not.toBeNull();
    click(byButton('Fold the panel') as HTMLButtonElement);
    await vi.waitFor(() => expect(byText(MEASURED)).toBeNull());
    expect(byText('Tap inside the field.')).not.toBeNull(); // what to do stays
    m.tap(30.1, -1.9);
    await vi.waitFor(() => expect(byButton(/^A · /)).not.toBeNull()); // and so do the choices
    click(byButton('Show the whole panel') as HTMLButtonElement);
    await vi.waitFor(() => expect(byText(MEASURED)).not.toBeNull());
    expect(byButton('Close')).not.toBeNull();
  });

  it('names a kept field by its number and size when it is clicked, and sends a question about it to Sage', async () => {
    const server = fakeServer(true);
    server.kept.push(
      { id: 'a', number: 3, centre: [30.5, -1.5], geometry: square(1), area_ha: 0.18, corrections: 0, kept_at: 'then' },
      { id: 'b', number: 7, centre: [30.25, -1.75], geometry: square(2), area_ha: 0.02, corrections: 0, by_hand: true, kept_at: 'then' },
    );
    const m = fakeMap();
    const asked: string[] = [];
    const named = { ...photo, name: 'Cyampirita' } as MapLayer;
    mount(<FieldTaps map={m.map} layers={[named]} hiddenLayerIDs={[]} onAskSage={(prompt) => asked.push(prompt)} />);
    await vi.waitFor(() => expect(byButton(/^2 fields outlined · show$/)).not.toBeNull());
    // each kept field carries its number on the map
    const labels = () =>
      m
        .drawn()
        .features.filter((f) => f.properties?.kind === 'label')
        .map((f) => f.properties?.label);
    await vi.waitFor(() => expect(labels()).toEqual(['3', '7']));

    m.clickOn('b');
    await vi.waitFor(() => expect(byText('Field 7 · 200 m² · by hand')).not.toBeNull());
    const box = document.querySelector('input[aria-label="Ask Sage about Field 7"]') as HTMLInputElement;
    const setValue = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')?.set as (v: string) => void;
    setValue.call(box, 'what is planted here?');
    box.dispatchEvent(new Event('input', { bubbles: true }));
    await vi.waitFor(() => expect((byButton('Ask') as HTMLButtonElement).disabled).toBe(false));
    click(byButton('Ask') as HTMLButtonElement);
    await vi.waitFor(() => expect(asked).toHaveLength(1));
    expect(asked[0]).toBe(
      'About Field 7 on the drone photo "Cyampirita" (plot 7 of my outlined fields: 200 m², centred at latitude -1.75, longitude 30.25): what is planted here?',
    );

    // removing asks first, and "Keep" leaves the field where it is
    click(byButton('Remove') as HTMLButtonElement);
    await vi.waitFor(() => expect(byText('Remove Field 7? Its number will not be used again.')).not.toBeNull());
    click(byButton('Keep') as HTMLButtonElement);
    await vi.waitFor(() => expect(byText('Remove Field 7? Its number will not be used again.')).toBeNull());
    expect(server.kept).toHaveLength(2);

    // a click on bare ground lets the field go
    m.clickOn(null);
    await vi.waitFor(() => expect(byText('Field 7 · 200 m² · by hand')).toBeNull());
  });

  it('counts only the crop the reader taps in a kept field, waits while the field is read, and shows the rest apart', async () => {
    timing.askAgainMs = 200; // long enough for each "still reading" answer to be seen
    const server = fakeServer(true);
    server.kept.push({ id: 'a', number: 3, centre: [30.5, -1.5], geometry: square(1), area_ha: 0.18, corrections: 0, kept_at: 'then' });
    const point = (lon: number, kind_code: number) => ({
      type: 'Feature',
      geometry: { type: 'Point', coordinates: [lon, -1.5] },
      properties: { kind_code },
    });
    const found = {
      my_crop: 1240,
      other_plants: 310,
      other_green: 95,
      counted_share: 0.86,
      per_ha: 8020,
      measured: 'An estimate, measured on one photo.',
      points: { type: 'FeatureCollection', features: [point(30.2, 0), point(30.3, 1), point(30.4, 2)] },
    };
    server.cropAnswers.push(
      { body: { working: true, done: 0, total: 0 } },
      { body: { working: true, done: 2, total: 9 } },
      { status: 422, body: { detail: 'A tap lies on ground where single plants cannot be told apart.' } },
      { body: found },
    );
    const m = fakeMap();
    const tapping: boolean[] = [];
    mount(<FieldTaps map={m.map} layers={[photo]} hiddenLayerIDs={[]} onTapping={(on) => tapping.push(on)} />);
    await vi.waitFor(() => expect(byButton(/^1 field outlined · show$/)).not.toBeNull());
    m.clickOn('a');
    await vi.waitFor(() => expect(byButton('Count my crop')).not.toBeNull());
    click(byButton('Count my crop') as HTMLButtonElement);
    await vi.waitFor(() => expect(byText('Tap 3 plants of your crop in Field 3. 0 tapped.')).not.toBeNull());
    // the other cards step aside while the crop is tapped, as they do while a field is outlined
    expect(tapping.at(-1)).toBe(true);

    // a tap outside the field is not taken; three inside it let the reader go on
    m.tap(35, -1.5);
    await vi.waitFor(() => expect(byText('That tap is outside Field 3. Tap inside its line.')).not.toBeNull());
    expect((byButton('Next') as HTMLButtonElement).disabled).toBe(true);
    for (const lon of [30.2, 30.4, 30.6, 30.8]) m.tap(lon, -1.5);
    await vi.waitFor(() => expect(byText('Tap 3 plants of your crop in Field 3. 4 tapped.')).not.toBeNull());
    click(byButton('Undo tap') as HTMLButtonElement); // a slip of the finger is taken back
    await vi.waitFor(() => expect(byText('Tap 3 plants of your crop in Field 3. 3 tapped.')).not.toBeNull());
    // the field stays picked: a tap on it is a tap on a plant, not a click that lets the field go
    expect(byText('That tap is outside Field 3. Tap inside its line.')).toBeNull();
    click(byButton('Next') as HTMLButtonElement);
    await vi.waitFor(() => expect(byText(/^Now tap 3 green things that are not your crop/)).not.toBeNull());
    for (const lon of [30.3, 30.5, 30.7]) m.tap(lon, -1.7);
    // each tap is on the map, crop and not-crop told apart by sign as well as colour
    const signs = () =>
      m
        .cropDrawn()
        .filter((f) => f.properties?.part === 'label')
        .map((f) => f.properties?.label);
    await vi.waitFor(() => expect(signs()).toEqual(['+', '+', '+', '×', '×', '×']));

    click(byButton('Count my crop') as HTMLButtonElement);
    // while the server reads the field it says how far it is, and the same taps are asked again
    await vi.waitFor(() => expect(byText(/^Reading Field 3: 2 of 9 parts\./)).not.toBeNull());
    // the server's reason for refusing a tap is shown word for word, and the taps are still there to correct
    await vi.waitFor(() => expect(byText('A tap lies on ground where single plants cannot be told apart.')).not.toBeNull());
    expect(signs()).toHaveLength(6);
    click(byButton('Count my crop') as HTMLButtonElement);
    await vi.waitFor(() => expect(byText('About 1,240 plants of your crop in Field 3 (about 8,020 a hectare).')).not.toBeNull());
    expect(server.cropAsked).toHaveLength(4);
    expect(server.cropAsked[0].url).toBe(`/api/layer/${LAYER}/field-taps/kept/a/my-crop`);
    expect(server.cropAsked[3].crop.map((p) => p.lon)).toEqual([30.2, 30.4, 30.6]);
    expect(server.cropAsked[3].other.map((p) => p.lat)).toEqual([-1.7, -1.7, -1.7]);
    // an estimate, with what was not counted and how sure it is, in the server's words
    expect(byText('Your crop: about 1,240')).not.toBeNull();
    expect(byText('Other plants: about 310')).not.toBeNull();
    expect(byText('Other green (weeds, grass or bush; which is not known): 95')).not.toBeNull();
    expect(byText(/^86% of the field was counted\./)).not.toBeNull();
    expect(byText('An estimate, measured on one photo.')).not.toBeNull();
    expect(m.cropDrawn().filter((f) => f.properties?.kind_code !== undefined)).toHaveLength(3);

    // Close takes the dots off the map and returns to the field
    click(byButton('Close') as HTMLButtonElement);
    await vi.waitFor(() => expect(byText('Field 3 · 0.18 ha')).not.toBeNull());
    expect(m.cropDrawn()).toHaveLength(0);
    expect(tapping.at(-1)).toBe(false);
  });

  it('lets the reader check the count on a part the app chose: rings that are not plants, plants with no ring', async () => {
    const server = fakeServer(true);
    server.kept.push({ id: 'a', number: 3, centre: [30.5, -1.5], geometry: square(1), area_ha: 0.18, corrections: 0, kept_at: 'then' });
    const m = fakeMap();
    const tapping: boolean[] = [];
    mount(<FieldTaps map={m.map} layers={[photo]} hiddenLayerIDs={[]} onTapping={(on) => tapping.push(on)} />);
    await vi.waitFor(() => expect(byButton(/^1 field outlined · show$/)).not.toBeNull());
    m.clickOn('a');
    await vi.waitFor(() => expect(byButton('Check the count')).not.toBeNull());
    click(byButton('Check the count') as HTMLButtonElement);
    await vi.waitFor(() => expect(byText('The app ringed 3 plants in this part of Field 3. Your count: 3.')).not.toBeNull());
    expect(tapping.at(-1)).toBe(true);
    expect(m.fitted()).toBe(1); // the map goes to the part
    expect(server.checkAsked[0]).toEqual({ url: `/api/layer/${LAYER}/field-taps/kept/a/count-check/square`, seen: [] });
    const signs = () =>
      m
        .checkDrawn()
        .filter((f) => f.geometry.type === 'Point')
        .map((f) => f.properties?.label);
    expect(signs()).toEqual(['', '', '']);

    // a tap on a ring: not a plant; the same ring again: a plant after all
    m.tap(30.4, -1.505);
    await vi.waitFor(() => expect(signs()).toEqual(['', '×', '']));
    m.tap(30.4, -1.5);
    await vi.waitFor(() => expect(signs()).toEqual(['', '', '']));
    m.tap(30.2, -1.5);
    // a tap away from every ring adds a plant; outside the part it is not taken
    m.tap(30.5, -1.2);
    m.tap(30.7, -1.2);
    m.tap(35, -1.5);
    await vi.waitFor(() => expect(byText('That tap is outside the part being checked. Tap inside the dashed line.')).not.toBeNull());
    await vi.waitFor(() => expect(signs()).toEqual(['×', '', '', '+', '+']));
    expect(byText('The app ringed 3 plants in this part of Field 3. Your count: 4.')).not.toBeNull();
    // a tap on a plant the reader added takes it away, and Undo takes back the last mark
    m.tap(30.7, -1.21);
    await vi.waitFor(() => expect(signs()).toEqual(['×', '', '', '+']));
    m.tap(30.8, -1.8);
    await vi.waitFor(() => expect(signs()).toHaveLength(5));
    click(byButton('Undo') as HTMLButtonElement);
    await vi.waitFor(() => expect(signs()).toEqual(['×', '', '', '+']));

    click(byButton('Save my count') as HTMLButtonElement);
    await vi.waitFor(() =>
      expect(
        byText('In this part the app counted 3 plants and you counted 3: 1 of its rings was not a plant, and it missed 1.'),
      ).not.toBeNull(),
    );
    expect(server.checkAsked[1]).toEqual({
      url: `/api/layer/${LAYER}/field-taps/kept/a/count-check`,
      square: [4, 2],
      wrong: [0],
      missed: [{ lon: 30.5, lat: -1.2 }],
    });
    expect(byText('Your own count, on a part the app chose.')).not.toBeNull();
    // there is no other part on this field: the server's words are shown
    click(byButton('Check another part') as HTMLButtonElement);
    await vi.waitFor(() => expect(byText('Every part of this field where plants are counted has been shown.')).not.toBeNull());
    expect(server.checkAsked[2].seen).toEqual([[4, 2]]);

    // Close takes the rings off the map; the field now says what was checked
    click(byButton('Close') as HTMLButtonElement);
    await vi.waitFor(() => expect(byText('You counted 3 where the app counted 3.')).not.toBeNull());
    expect(m.checkDrawn()).toHaveLength(0);
    expect(tapping.at(-1)).toBe(false);
  });

  it("shows a field's green cover as the server measured it, or why it was not measured", async () => {
    const server = fakeServer(true);
    server.kept.push(
      {
        id: 'a',
        number: 3,
        centre: [30.5, -1.5],
        geometry: square(1),
        area_ha: 0.18,
        corrections: 0,
        kept_at: 'then',
        green_cover: { share: 0.624, why_not: null },
      },
      {
        id: 'b',
        number: 4,
        centre: [30.25, -1.75],
        geometry: square(2),
        area_ha: 0.9,
        corrections: 0,
        kept_at: 'then',
        green_cover: { share: null, why_not: 'Not measured for fields over 0.7 ha: it was tested on smaller fields.' },
      },
    );
    const m = fakeMap();
    const asked: string[] = [];
    mount(
      <FieldTaps
        map={m.map}
        layers={[{ ...photo, name: 'Cyampirita' } as MapLayer]}
        hiddenLayerIDs={[]}
        onAskSage={(prompt) => asked.push(prompt)}
      />,
    );
    await vi.waitFor(() => expect(byButton(/^2 fields outlined · show$/)).not.toBeNull());
    m.clickOn('a');
    // whole per cent: the test put a typical field within about 3 points
    await vi.waitFor(() => expect(byText('Field 3 · 0.18 ha · green cover 62%')).not.toBeNull());
    const box = document.querySelector('input[aria-label="Ask Sage about Field 3"]') as HTMLInputElement;
    const setValue = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')?.set as (v: string) => void;
    setValue.call(box, 'is it weedy?');
    box.dispatchEvent(new Event('input', { bubbles: true }));
    await vi.waitFor(() => expect((byButton('Ask') as HTMLButtonElement).disabled).toBe(false));
    click(byButton('Ask') as HTMLButtonElement);
    await vi.waitFor(() => expect(asked).toHaveLength(1));
    expect(asked[0]).toContain('(plot 3 of my outlined fields: 0.18 ha, green cover 62%, centred');
    // a field it was not measured on shows no figure, and says why
    m.clickOn('b');
    await vi.waitFor(() => expect(byText('Field 4 · 0.90 ha')).not.toBeNull());
    expect(byText('Green cover: Not measured for fields over 0.7 ha: it was tested on smaller fields.')).not.toBeNull();
  });

  it('asks again while the server is still measuring green cover, and stops once it is done', async () => {
    const server = fakeServer(true);
    server.kept.push({ id: 'a', number: 3, centre: [30.5, -1.5], geometry: square(1), area_ha: 0.18, corrections: 0, kept_at: 'then' });
    const plain = fetchMock.getMockImplementation() as typeof fetch;
    let asked = 0;
    fetchMock.mockImplementation(async (input, init) => {
      const res = await plain(input, init);
      if (String(input) !== `/api/layer/${LAYER}/field-taps`) return res;
      asked += 1;
      const body = await res.json();
      if (asked === 1)
        return new Response(JSON.stringify({ ...body, cover_filling: true }), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        });
      body.kept[0].green_cover = { share: 0.62, why_not: null };
      return new Response(JSON.stringify({ ...body, cover_filling: false }), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      });
    });
    const m = fakeMap();
    vi.useFakeTimers({ shouldAdvanceTime: true });
    try {
      mount(<FieldTaps map={m.map} layers={[photo]} hiddenLayerIDs={[]} />);
      await vi.waitFor(() => expect(byButton(/^1 field outlined · show$/)).not.toBeNull());
      m.clickOn('a');
      await vi.waitFor(() => expect(byText('Field 3 · 0.18 ha')).not.toBeNull()); // no figure yet
      await vi.advanceTimersByTimeAsync(3_100);
      await vi.waitFor(() => expect(byText('Field 3 · 0.18 ha · green cover 62%')).not.toBeNull());
      await vi.advanceTimersByTimeAsync(10_000);
      expect(asked).toBe(2); // done: no more asking
    } finally {
      vi.useRealTimers();
    }
  });

  it('shows a small field in square metres and marks added and removed parts by sign, not colour alone', async () => {
    const server = fakeServer(true);
    server.kept.push({ id: 'a', number: 1, centre: [30.5, -1.5], geometry: square(1), area_ha: 0.004, corrections: 0, kept_at: 'then' });
    const m = fakeMap();
    mount(<FieldTaps map={m.map} layers={[photo]} hiddenLayerIDs={[]} />);
    await vi.waitFor(() => expect(byButton(/^1 field outlined · show$/)).not.toBeNull());
    m.clickOn('a');
    await vi.waitFor(() => expect(byText('Field 1 · 40 m²')).not.toBeNull()); // not "0.00 ha"

    click(byButton('Outline a field') as HTMLButtonElement);
    await vi.waitFor(() => expect(m.listening()).toBe(true));
    m.tap(30.1, -1.9);
    await vi.waitFor(() => expect(byButton(/^A · /)).not.toBeNull());
    click(byButton(/^A · /) as HTMLButtonElement);
    await vi.waitFor(() => expect((byButton('Not this part') as HTMLButtonElement).disabled).toBe(false));
    click(byButton('Not this part') as HTMLButtonElement);
    await vi.waitFor(() => expect(m.listening()).toBe(true));
    m.tap(30.4, -1.9);
    // wait for the marks themselves: the crosshair goes as soon as the tap is sent, before the answer is drawn
    const signs = () =>
      m
        .drawn()
        .features.filter((f) => f.properties?.kind === 'label' && ['+', '−'].includes(f.properties?.label))
        .map((f) => f.properties?.label);
    await vi.waitFor(() => expect(signs()).toEqual(['+', '−']));
  });

  it('says so when the server left out a choice that ran past the edge of what was read', async () => {
    fakeServer(true);
    const answer = fetchMock.getMockImplementation() as NonNullable<ReturnType<typeof fetchMock.getMockImplementation>>;
    fetchMock.mockImplementation(async (input, init) => {
      const response = await answer(input, init);
      if (!String(input).endsWith('/outline')) return response;
      const body = await response.json();
      return new Response(JSON.stringify({ outlines: body.outlines.slice(0, 2), left_out: 1 }), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      });
    });
    const m = fakeMap();
    mount(<FieldTaps map={m.map} layers={[photo]} hiddenLayerIDs={[]} />);
    await vi.waitFor(() => expect(byButton('Outline a field')).not.toBeNull());
    click(byButton('Outline a field') as HTMLButtonElement);
    await vi.waitFor(() => expect(m.listening()).toBe(true));
    m.tap(30.1, -1.9);
    await vi.waitFor(() => expect(byButton(/^B · /)).not.toBeNull());
    expect(byButton(/^C · /)).toBeNull();
    expect(byText(/A larger outline was left out/)).not.toBeNull();
  });

  it('drops an answer that arrives after the reader closed the panel', async () => {
    const server = fakeServer(true);
    const m = fakeMap();
    mount(<FieldTaps map={m.map} layers={[photo]} hiddenLayerIDs={[]} />);
    await vi.waitFor(() => expect(byButton('Outline a field')).not.toBeNull());
    click(byButton('Outline a field') as HTMLButtonElement);
    await vi.waitFor(() => expect(m.listening()).toBe(true));
    // the outline answer is held back until the reader has closed the panel
    const gate: { release: (() => void) | null } = { release: null };
    const held = new Promise<void>((resolve) => {
      gate.release = resolve;
    });
    const answer = fetchMock.getMockImplementation() as NonNullable<ReturnType<typeof fetchMock.getMockImplementation>>;
    fetchMock.mockImplementation(async (input, init) => {
      if (String(input).endsWith('/outline')) await held;
      return answer(input, init);
    });
    m.tap(30.1, -1.9);
    await vi.waitFor(() => expect(byText('Working…')).not.toBeNull());
    click(byButton('Close') as HTMLButtonElement);
    await vi.waitFor(() => expect(byButton('Outline a field')).not.toBeNull());
    gate.release?.();
    await new Promise((r) => setTimeout(r, 30));
    expect(byButton('Outline a field')).not.toBeNull(); // still closed: the late answer did not reopen it
    expect(byButton(/^A · /)).toBeNull();
    expect(server.turnedDown).toEqual([]); // nothing had been offered, so nothing was turned down
  });

  it('says why when a field could not be removed', async () => {
    const server = fakeServer(true);
    server.kept.push({ id: 'a', number: 4, centre: [30.5, -1.5], geometry: square(1), area_ha: 0.18, corrections: 0, kept_at: 'then' });
    const m = fakeMap();
    mount(<FieldTaps map={m.map} layers={[photo]} hiddenLayerIDs={[]} />);
    await vi.waitFor(() => expect(byButton(/^1 field outlined · show$/)).not.toBeNull());
    m.clickOn('a');
    await vi.waitFor(() => expect(byText('Field 4 · 0.18 ha')).not.toBeNull());
    click(byButton('Remove') as HTMLButtonElement);
    await vi.waitFor(() => expect(byText('Remove Field 4? Its number will not be used again.')).not.toBeNull());
    // the fake server knows no DELETE route, so it answers 404 "Not Found"
    click(byButton('Remove') as HTMLButtonElement);
    await vi.waitFor(() => expect(byText('Not Found')).not.toBeNull());
    expect(byText('Field 4 · 0.18 ha')).not.toBeNull(); // the field is still there and still selected
  });

  it('tells the server each time offered outlines are not kept, with the reason', async () => {
    const server = fakeServer(true);
    const m = fakeMap();
    mount(<FieldTaps map={m.map} layers={[photo]} hiddenLayerIDs={[]} />);
    await vi.waitFor(() => expect(byButton('Outline a field')).not.toBeNull());
    click(byButton('Outline a field') as HTMLButtonElement);
    await vi.waitFor(() => expect(m.listening()).toBe(true));

    // leaving before any tap is nothing to record
    click(byButton('Start again') as HTMLButtonElement);
    m.tap(30.1, -1.9);
    await vi.waitFor(() => expect(byButton('None of these')).not.toBeNull());
    click(byButton('None of these') as HTMLButtonElement);
    await vi.waitFor(() => expect(server.turnedDown).toHaveLength(1));

    await vi.waitFor(() => expect(m.listening()).toBe(true));
    m.tap(30.2, -1.9);
    await vi.waitFor(() => expect(byButton(/^B · /)).not.toBeNull());
    click(byButton(/^B · /) as HTMLButtonElement);
    await vi.waitFor(() => expect(byText('Your field: 0.20 ha')).not.toBeNull());
    click(byButton('Close') as HTMLButtonElement);
    await vi.waitFor(() => expect(server.turnedDown).toHaveLength(2));

    expect(server.turnedDown.map((t) => [t.reason, t.pick, t.taps.length, t.taps[0].lon])).toEqual([
      ['none_of_these', null, 1, 30.1],
      ['closed', 1, 1, 30.2],
    ]);
    expect(server.kept).toEqual([]);
  });

  it('stops offering corrections after the number the server allows', async () => {
    fakeServer(true);
    const m = fakeMap();
    mount(<FieldTaps map={m.map} layers={[photo]} hiddenLayerIDs={[]} />);
    await vi.waitFor(() => expect(byButton('Outline a field')).not.toBeNull());
    click(byButton('Outline a field') as HTMLButtonElement);
    await vi.waitFor(() => expect(m.listening()).toBe(true));
    m.tap(30.1, -1.9);
    await vi.waitFor(() => expect(byButton(/^A · /)).not.toBeNull());
    click(byButton(/^A · /) as HTMLButtonElement);
    for (const lng of [30.2, 30.3]) {
      await vi.waitFor(() => expect((byButton('This part too') as HTMLButtonElement).disabled).toBe(false));
      click(byButton('This part too') as HTMLButtonElement);
      await vi.waitFor(() => expect(m.listening()).toBe(true));
      m.tap(lng, -1.9);
      await vi.waitFor(() => expect(m.listening()).toBe(false));
    }
    await vi.waitFor(() => expect(byText(/No corrections left/)).not.toBeNull());
    expect((byButton('This part too') as HTMLButtonElement).disabled).toBe(true);
  });
});
