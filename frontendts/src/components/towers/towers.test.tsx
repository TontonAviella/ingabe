import { apiFetch } from '@mundi/ee';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { PhotoMarking } from '@/components/towers/PhotoMarking';
import { TowerCards } from '@/components/towers/TowerCards';
import type { Box } from '@/lib/photoView';
import type { InspectionPhoto, MarksPayload, PartWork, PhotoWork, ReviewPayload, TowerPart } from '@/lib/towerContract';
import type { MapLayer } from '@/lib/types';
import { allByText, byButton, byText, click, findButton, findRole, findText, mount, pointer, unmountAll } from '@/test/dom';

// The server is mocked route by route with the shapes of the shared tower API contract.
vi.mock('@mundi/ee', () => ({ apiFetch: vi.fn() }));
const fetchMock = vi.mocked(apiFetch);

const LAYER = 'LtowerAB12';
const SHA = 'c'.repeat(64);
const PHOTO: InspectionPhoto = {
  sha256: SHA,
  url: `/api/layer/${LAYER}/photos/${SHA}`,
  width: 2000,
  height: 1400,
  taken_at: '2026-10-09T10:31:00+02:00',
  lat: -1.9441,
  lon: 30.0619,
  relative_altitude_m: 42.3,
  gimbal_pitch: -12.5,
  gimbal_yaw: 88,
};
const D: Box[] = [
  [100, 120, 220, 240],
  [400, 110, 515, 228],
  [700, 130, 812, 245],
];
const ANTENNA: Box = [1500, 300, 1530, 520];
const SUGGESTIONS = [
  { id: 's1', box: [900, 100, 1010, 215] as Box, score: 0.61 },
  { id: 's2', box: [1100, 90, 1205, 200] as Box, score: 0.33 },
  { id: 's3', box: [40, 40, 60, 60] as Box, score: 0.02 },
];

function part(over: Partial<PartWork> = {}): PartWork {
  return {
    marks: [],
    lookalike_marks: [],
    job: null,
    suggestions: [],
    reviewed: { kept: [], rejected: [], fixed: [], added: [] },
    counted_by_you: null,
    waiting: 0,
    ...over,
  };
}

function photoWork(over: Partial<PhotoWork['parts']> = {}): PhotoWork {
  return { parts: { tower_dish: part(), tower_antenna: part(), tower_radio_unit: part(), ...over } };
}

type Handler = (body: unknown) => { status: number; body: unknown };
let routes: Record<string, Handler>;
let calls: { method: string; url: string; body: unknown }[];

function json(status: number, body: unknown) {
  return new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } });
}

// A viewport of 1000 x 700 CSS pixels: the 2000 x 1400 photo fits at half size from the top-left corner.
function viewport() {
  vi.spyOn(HTMLElement.prototype, 'getBoundingClientRect').mockReturnValue({
    x: 0,
    y: 0,
    left: 0,
    top: 0,
    right: 1000,
    bottom: 700,
    width: 1000,
    height: 700,
    toJSON: () => ({}),
  });
}

beforeEach(() => {
  routes = {};
  calls = [];
  fetchMock.mockImplementation(async (input, init) => {
    const url = String(input);
    const method = init?.method ?? 'GET';
    const body = typeof init?.body === 'string' ? JSON.parse(init.body) : undefined;
    calls.push({ method, url, body });
    const handler = routes[`${method} ${url}`];
    if (!handler) return json(404, { detail: `No route ${method} ${url}` });
    const answer = handler(body);
    return json(answer.status, answer.body);
  });
});

afterEach(() => {
  unmountAll();
  fetchMock.mockReset();
  vi.restoreAllMocks();
});

const WORK = `/api/towers/layer/${LAYER}/photos/${SHA}/work`;
const MARKS = `/api/towers/layer/${LAYER}/photos/${SHA}/marks`;
const FIND = `/api/towers/layer/${LAYER}/photos/${SHA}/find`;
const REVIEW = `/api/towers/layer/${LAYER}/photos/${SHA}/review`;

const savedMarks: Handler = (body) => ({ status: 200, body: part({ marks: (body as { marks: Box[] }).marks }) });

/** A server that keeps the photo's work: saved marks and started jobs show in the next GET, as on the real one. */
function keepWork(start: PhotoWork) {
  const state = structuredClone(start);
  routes[`GET ${WORK}`] = () => ({ status: 200, body: state });
  routes[`PUT ${MARKS}`] = (body) => {
    const { part: p, marks, lookalike_marks } = body as MarksPayload;
    state.parts[p] = { ...state.parts[p], marks, lookalike_marks };
    return { status: 200, body: state.parts[p] };
  };
  routes[`POST ${FIND}`] = (body) => {
    const job = { job_id: 'f'.repeat(64), status: 'queued' };
    state.parts[(body as { part: TowerPart }).part].job = job;
    return { status: 200, body: job };
  };
  return state;
}

function openScreen() {
  mount(<PhotoMarking layerId={LAYER} layerName="Mast 12" photos={[PHOTO]} startIndex={0} onClose={vi.fn()} />);
}

function called(method: string, url: string) {
  return calls.find((c) => c.method === method && c.url === url);
}

describe('marking screen: finding the rest', () => {
  it("sends the marks with the other parts' marks as look-alikes, and shows the server's 422 reason word for word", async () => {
    const reason =
      'The marked microwave dishes are 18 px across as the finder reads this photo; it was measured on dishes of 31 px or more, so it would not find them reliably.';
    routes[`GET ${WORK}`] = () => ({
      status: 200,
      body: photoWork({ tower_dish: part({ marks: D }), tower_antenna: part({ marks: [ANTENNA] }) }),
    });
    routes[`PUT ${MARKS}`] = savedMarks;
    routes[`POST ${FIND}`] = () => ({ status: 422, body: { detail: reason } });
    openScreen();

    await findText('3 marked');
    click(await findButton(/Find the rest/));

    const alert = await findRole('alert');
    expect(alert.textContent).toBe(reason);
    expect(called('PUT', MARKS)?.body).toEqual({ part: 'tower_dish', marks: D, lookalike_marks: [ANTENNA] });
    expect(called('POST', FIND)?.body).toEqual({ part: 'tower_dish' });
  });

  it("shows the finder line's own words when it is full", async () => {
    routes[`GET ${WORK}`] = () => ({ status: 200, body: photoWork({ tower_dish: part({ marks: D }) }) });
    routes[`PUT ${MARKS}`] = savedMarks;
    routes[`POST ${FIND}`] = () => ({ status: 429, body: { detail: 'You already have 3 finds waiting; wait for one to end.' } });
    openScreen();
    await findText('3 marked');
    click(await findButton(/Find the rest/));
    expect((await findRole('alert')).textContent).toBe('You already have 3 finds waiting; wait for one to end.');
  });

  it('asks for at least 3 marks before finding the rest', async () => {
    routes[`GET ${WORK}`] = () => ({ status: 200, body: photoWork({ tower_dish: part({ marks: D.slice(0, 2) }) }) });
    openScreen();
    await findText('2 marked · 1 more to find the rest');
    expect(byButton(/Find the rest/)).toBeDisabled();
  });

  it('draws a mark with the pointer, in the original photo pixels, then waits for the find', async () => {
    viewport();
    const state = keepWork(photoWork({ tower_dish: part({ marks: D.slice(0, 2) }) }));
    openScreen();
    await findText(/^2 marked/);

    const img = await findRole('application').then((canvas) => canvas.querySelector('img') as HTMLImageElement);
    Object.defineProperty(img, 'naturalWidth', { value: 2000 });
    Object.defineProperty(img, 'naturalHeight', { value: 1400 });
    img.dispatchEvent(new Event('load'));

    const canvas = await findRole('application');
    await vi.waitFor(() => expect(byText('Loading the photo…')).toBeNull());
    pointer(canvas, 'pointerdown', 600, 100);
    pointer(canvas, 'pointermove', 650, 160);
    pointer(canvas, 'pointerup', 650, 160);
    await findText('3 marked');

    // A tap is not a box.
    pointer(canvas, 'pointerdown', 300, 300);
    pointer(canvas, 'pointerup', 302, 303);

    click(await findButton(/Find the rest/));
    await findText('Waiting in line for the finder…', 'p');
    expect(state.parts.tower_dish.marks).toEqual([D[0], D[1], [1200, 200, 1300, 320]]);
    expect(byButton(/Find the rest/)).toBeDisabled();
  });
});

describe('marking screen: checking suggestions', () => {
  it("says only how many wait until the check is saved, then shows the server's count", async () => {
    viewport();
    // This server also sends a count while suggestions wait: the screen must not show it.
    const waiting = part({
      marks: D,
      suggestions: SUGGESTIONS,
      waiting: 3,
      counted_by_you: 9,
      job: { job_id: 'f'.repeat(64), status: 'done' },
    });
    routes[`GET ${WORK}`] = () => ({ status: 200, body: photoWork({ tower_dish: waiting }) });
    routes[`PUT ${REVIEW}`] = (body) => {
      const { part: _part, ...reviewed } = body as ReviewPayload;
      return { status: 200, body: { ...waiting, reviewed, waiting: 0, counted_by_you: 5 } };
    };
    openScreen();

    await findText('3 suggestions waiting for your check', 'p');
    expect(byText(/counted by you/)).toBeNull();
    for (const line of allByText(/waiting for your check/)) expect(line.textContent).not.toMatch(/\b9\b/);

    click(await findButton('Next to check'));
    click(await findButton('Keep'));
    await findText('2 suggestions waiting for your check', 'p');
    click(await findButton('Reject'));
    await findText('1 suggestion waiting for your check', 'p');
    click(await findButton('Keep'));
    await findText('All checked: save your review to count them', 'p');
    expect(byText(/counted by you/)).toBeNull();

    click(await findButton(/Save review/));
    await findText('5 counted by you', 'p');
    expect(called('PUT', REVIEW)?.body).toEqual({ part: 'tower_dish', kept: ['s1', 's3'], rejected: ['s2'], fixed: [], added: [] });
  });

  it("shows the server's words when saving the check fails", async () => {
    viewport();
    routes[`GET ${WORK}`] = () => ({
      status: 200,
      body: photoWork({ tower_dish: part({ marks: D, suggestions: SUGGESTIONS, waiting: 3 }) }),
    });
    routes[`PUT ${REVIEW}`] = () => ({
      status: 409,
      body: { detail: 'These suggestions were replaced by a newer find; check the new ones.' },
    });
    openScreen();
    await findText('3 suggestions waiting for your check', 'p');
    click(await findButton('Next to check'));
    click(await findButton('Reject'));
    click(await findButton(/Save review/));
    expect((await findRole('alert')).textContent).toBe('These suggestions were replaced by a newer find; check the new ones.');
  });
});

describe('tower cards panel', () => {
  const layers: MapLayer[] = [{ id: LAYER, name: 'Mast 12', type: 'vector', metadata: { original_format: 'photo_zip' } }];
  const CARDS = `/api/towers/layer/${LAYER}/cards`;
  const PHOTOS = `/api/towers/layer/${LAYER}/photos`;
  const panel = (list: MapLayer[]) => <TowerCards map={null} layers={list} hiddenLayerIDs={[]} historyOpen={false} onAskSage={vi.fn()} />;

  it('shows nothing when the server has no towers here (404)', async () => {
    routes[`GET ${CARDS}?audience=owner`] = () => ({ status: 404, body: { detail: 'Not Found' } });
    routes[`GET ${PHOTOS}`] = () => ({ status: 404, body: { detail: 'Not Found' } });
    const host = mount(panel(layers));
    await vi.waitFor(() => expect(calls.length).toBe(2));
    await vi.waitFor(() => expect(host).toBeEmptyDOMElement());
  });

  it('asks nothing for layers that are not photo inspections', async () => {
    const host = mount(panel([{ id: 'Lsites', name: 'Sites', type: 'vector', metadata: { original_format: 'geojson' } }]));
    await new Promise((resolve) => setTimeout(resolve, 50));
    expect(calls).toEqual([]);
    expect(host).toBeEmptyDOMElement();
  });

  it('lists the cards for the chosen reader and opens an answer', async () => {
    routes[`GET ${PHOTOS}`] = () => ({ status: 200, body: { photos: [PHOTO] } });
    routes[`GET ${CARDS}?audience=owner`] = () => ({
      status: 200,
      body: { cards: [{ id: 'equipment', title: 'What is on this mast?', status: 'partly', preview: '2 of 3 parts checked on 1 photo' }] },
    });
    routes[`GET ${CARDS}?audience=technician`] = () => ({
      status: 200,
      body: { cards: [{ id: 'to_check', title: 'Which photos still need a check?', status: 'ready', preview: '1 photo waits' }] },
    });
    routes[`GET ${CARDS}/to_check?audience=technician`] = () => ({
      status: 200,
      body: {
        id: 'to_check',
        title: 'Which photos still need a check?',
        status: 'ready',
        preview: '1 photo waits',
        what: 'One photo has suggestions waiting for your check.',
        why: 'Nothing is counted until a person has checked it.',
        todo: 'Open the photo and keep or reject each suggestion.',
        how_sure: { level: 'high', label: 'High', bars: 3, because: ['Counted by you'], surer: null },
        facts: [{ label: 'Photos', value: '1' }],
        items: [],
        progress: null,
        terms: [],
        downloads: [],
        ask_sage: [],
      },
    });
    mount(panel(layers));

    await findText('What is on this mast?');
    expect(byText('1 photo')).not.toBeNull();
    click(await findButton('Technician'));
    click(await findText('Which photos still need a check?'));
    await findText('One photo has suggestions waiting for your check.');
    expect(byText('Open the photo and keep or reject each suggestion.')).not.toBeNull();
    expect(byText('High')).not.toBeNull();
  });

  it('"Check photos" opens the marking screen on the first photo that needs a check', async () => {
    const other = 'd'.repeat(64);
    const second: InspectionPhoto = { ...PHOTO, sha256: other, url: `/api/layer/${LAYER}/photos/${other}` };
    routes[`GET ${PHOTOS}`] = () => ({ status: 200, body: { photos: [PHOTO, second] } });
    routes[`GET ${CARDS}?audience=owner`] = () => ({ status: 200, body: { cards: [] } });
    routes[`GET ${WORK}`] = () => ({ status: 200, body: photoWork({ tower_dish: part({ counted_by_you: 4 }) }) });
    routes[`GET /api/towers/layer/${LAYER}/photos/${other}/work`] = () => ({
      status: 200,
      body: photoWork({ tower_antenna: part({ waiting: 1, suggestions: [{ id: 'x', box: [1, 1, 9, 9], score: 0.5 }] }) }),
    });
    mount(panel(layers));
    click(await findButton(/Check photos/));
    await findText(/^Photo 2 of 2/);
  });
});
