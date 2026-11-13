import { apiFetch } from '@mundi/ee';
import type { Map as MLMap } from 'maplibre-gl';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { FieldTaps } from '@/components/FieldTaps';
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
        kept,
        turned_down: turnedDown.length,
      });
    if (url.endsWith('/moved-line')) {
      measured.push(body);
      // the fake's rule: a line dragged east of longitude 40 is refused, as the server refuses a crossed line
      if (body.moved.some((c: [number, number]) => c[0] > 40))
        return json({ detail: 'The line crosses itself. Move that corner back.' }, 422);
      return json({ area_ha: 0.42 });
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
  return { kept, asked, turnedDown, measured };
}

/** The few things the component asks of the map; `tap` plays a reader's tap on the photo. */
function fakeMap() {
  const handlers = new Map<string, ((e: unknown) => void)[]>();
  let drawn: GeoJSON.FeatureCollection = { type: 'FeatureCollection', features: [] };
  const source = {
    setData: (d: GeoJSON.FeatureCollection) => {
      drawn = d;
    },
  };
  let added = false;
  let under: string | null = null;
  const canvas = { style: { cursor: '' } as CSSStyleDeclaration };
  const map = {
    queryRenderedFeatures: () => (under ? [{ properties: { id: under } }] : []),
    easeTo: vi.fn(),
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
    getSource: () => (added ? source : undefined),
    addSource: () => {
      added = true;
    },
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
    /** Press at one place, drag to another, let go. True when the press took hold of the line (the map held still). */
    pull: (from: [number, number], to: [number, number]) => {
      const preventDefault = vi.fn();
      const event = (c: [number, number]) => ({
        lngLat: { lng: c[0], lat: c[1] },
        point: { x: from[0] * 1000, y: -from[1] * 1000 },
        preventDefault,
      });
      for (const fn of handlers.get('mousedown') ?? []) fn(event(from));
      for (const fn of handlers.get('mousemove') ?? []) fn(event(to));
      for (const fn of handlers.get('mouseup') ?? []) fn(event(to));
      return preventDefault.mock.calls.length > 0;
    },
    /** A click on the map, on the kept field with this id or on bare ground (null). */
    clickOn: (id: string | null) => {
      under = id;
      for (const fn of handlers.get('click') ?? []) fn({ lngLat: { lng: 30.5, lat: -1.5 }, point: { x: 0, y: 0 } });
    },
    drawn: () => drawn,
  };
}

const photo = { id: LAYER, type: 'raster' } as MapLayer;
afterEach(() => {
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

  it('lets the reader pull the line of the picked outline, and keeps it marked as moved by hand', async () => {
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
    await vi.waitFor(() => expect(byText('Pull the line to the true edge of the field.')).not.toBeNull());
    const has = (ring: number[][], lng: number, lat: number) =>
      ring.some((c) => Math.abs(c[0] - lng) < 1e-9 && Math.abs(c[1] - lat) < 1e-9);

    // a press away from the line moves nothing and leaves the map free to pan
    expect(m.pull([30.5, -1.5], [30.6, -1.5])).toBe(false);
    expect(server.measured).toEqual([]);

    // pull the south-east corner east: the map holds still, the line bends there, the server measures it
    expect(m.pull([31, -2], [31.5, -2])).toBe(true);
    await vi.waitFor(() => expect(server.measured).toHaveLength(1));
    const moved = server.measured[0].moved;
    expect(has(moved, 31.5, -2)).toBe(true); // the grabbed corner went all the way
    expect(has(moved, 30, -1)).toBe(true); // the far corner stayed
    expect(moved.some((c) => c[0] > 31.01 && c[0] < 31.49)).toBe(true); // its neighbours went part of the way
    expect(server.measured[0].first_tap.lon).toBe(30.1);
    await vi.waitFor(() => expect(byText('0.42 ha')).not.toBeNull());

    // a line the server refuses cannot be kept, and its reason is shown word for word
    m.pull([30, -2], [41, -2]);
    await vi.waitFor(() => expect(byText('The line crosses itself. Move that corner back.')).not.toBeNull());
    click(byButton('Done') as HTMLButtonElement);
    await vi.waitFor(() => expect((byButton('Keep') as HTMLButtonElement).disabled).toBe(true));
    // corrections by tap are for the model's outline, not for a hand-moved one
    expect((byButton('This part too') as HTMLButtonElement).disabled).toBe(true);

    // Back drops the hand-moved line and returns the outline as the model drew it
    click(byButton('Back') as HTMLButtonElement);
    await vi.waitFor(() => expect(byText('Your field: 0.10 ha')).not.toBeNull());

    click(byButton('Move the line') as HTMLButtonElement);
    await vi.waitFor(() => expect(byText('Pull the line to the true edge of the field.')).not.toBeNull());
    m.pull([31, -1], [31.25, -0.75]);
    await vi.waitFor(() => expect(server.measured).toHaveLength(3));
    click(byButton('Done') as HTMLButtonElement);
    await vi.waitFor(() => expect(byText('Your field: 0.42 ha · line moved by hand')).not.toBeNull());
    click(byButton('Keep') as HTMLButtonElement);
    await vi.waitFor(() => expect(server.kept).toHaveLength(1));
    expect(has((server.kept[0] as KeptOutline & { moved: number[][] }).moved, 31.25, -0.75)).toBe(true);
    click((await vi.waitFor(() => byButton(/^1 field outlined · show$/))) as HTMLButtonElement);
    await vi.waitFor(() => expect(byText(/Field 1 · 0\.42 ha · by hand/)).not.toBeNull());
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
