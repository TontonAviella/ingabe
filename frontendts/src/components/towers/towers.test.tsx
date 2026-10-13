import { apiFetch } from '@mundi/ee';
import type { Map as MLMap } from 'maplibre-gl';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { AnswerBody } from '@/components/CardAnswer';
import { PhotoMarking } from '@/components/towers/PhotoMarking';
import { TowerCards } from '@/components/towers/TowerCards';
import type { CardAnswerCore } from '@/lib/cardAnswer';
import type { Box } from '@/lib/photoView';
import type { PartWork, PhotoWork } from '@/lib/towerContract';
import type { MapLayer } from '@/lib/types';
import { allByText, byButton, byText, click, findButton, findRole, findText, mount, pointer, unmountAll } from '@/test/dom';
import { type FakePhoto, towerServer } from '@/test/towerServer';

// The server is a fake of the tower API whose rules are copied from the real backend code (src/test/towerServer.ts);
// a test replaces one route only to answer a refusal, in the real server's words.
vi.mock('@mundi/ee', () => ({ apiFetch: vi.fn() }));
const fetchMock = vi.mocked(apiFetch);

const LAYER = 'LtowerAB12';
const SHA = 'c'.repeat(64);
const PHOTO: FakePhoto = {
  sha256: SHA,
  url: `/api/layer/${LAYER}/photos/${SHA}`,
  filename: 'DJI_0001.JPG',
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
const FINDS = [
  { box: [900, 100, 1010, 215] as Box, score: 0.61 },
  { box: [1100, 90, 1205, 200] as Box, score: 0.33 },
  { box: [40, 40, 60, 60] as Box, score: 0.02 },
];

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

const photoUrl = (sha: string) => `/api/towers/layer/${LAYER}/photos/${sha}`;
const WORK = `${photoUrl(SHA)}/work`;
const MARKS = `${photoUrl(SHA)}/marks`;
const FIND = `${photoUrl(SHA)}/find`;
const REVIEW = `${photoUrl(SHA)}/review`;

/** The fake server with these photos, its routes answering the screens' requests. */
function serve(photos: FakePhoto[] = [PHOTO]) {
  const server = towerServer(LAYER, photos);
  Object.assign(routes, server.routes());
  return server;
}

/** A part marked, found and finished on the server, as a person and the finder's worker would have left it. */
function found(server: ReturnType<typeof towerServer>, sha: string, finds: typeof FINDS) {
  server.setMarks(sha, { part: 'tower_dish', marks: D, lookalike_marks: [] });
  server.startFind(sha, { part: 'tower_dish' });
  server.finish(sha, 'tower_dish', finds);
}

function openScreen(photos: FakePhoto[] = [PHOTO]) {
  mount(<PhotoMarking layerId={LAYER} layerName="Mast 12" photos={photos} startIndex={0} onClose={vi.fn()} />);
}

/** Lets the photo load at its full 2000 x 1400 size and returns the canvas to draw on. */
async function loadedCanvas() {
  const img = await findRole('application').then((canvas) => canvas.querySelector('img') as HTMLImageElement);
  Object.defineProperty(img, 'naturalWidth', { value: 2000 });
  Object.defineProperty(img, 'naturalHeight', { value: 1400 });
  img.dispatchEvent(new Event('load'));
  const canvas = await findRole('application');
  await vi.waitFor(() => expect(byText('Loading the photo…')).toBeNull());
  return canvas;
}

function draw(canvas: HTMLElement, from: [number, number], to: [number, number]) {
  pointer(canvas, 'pointerdown', ...from);
  pointer(canvas, 'pointermove', ...to);
  pointer(canvas, 'pointerup', ...to);
}

function called(method: string, url: string) {
  return calls.find((c) => c.method === method && c.url === url);
}

describe('marking screen: finding the rest', () => {
  it("sends only the part's own marks, and shows the server's 422 reason word for word", async () => {
    const reason =
      'the marked dishes are about 12 px wide as the finder reads them (19 px in this 2000 px picture, read at 1280 px); dishes were measured only from 17 px wide, so this picture is outside the measurement';
    const server = serve();
    server.setMarks(SHA, { part: 'tower_dish', marks: D, lookalike_marks: [] });
    server.setMarks(SHA, { part: 'tower_antenna', marks: [ANTENNA], lookalike_marks: [] });
    routes[`POST ${FIND}`] = () => ({ status: 422, body: { detail: reason } });
    openScreen();

    await findText('3 marked');
    click(await findButton(/Find the rest/));

    expect((await findRole('alert')).textContent).toBe(reason);
    // The real server refuses look-alikes for dishes (422): the antenna is not sent along, the server picks them.
    expect(called('PUT', MARKS)?.body).toEqual({ part: 'tower_dish', marks: D, lookalike_marks: [] });
    expect(called('POST', FIND)?.body).toEqual({ part: 'tower_dish' });
  });

  it("shows the finder line's own words when it is full", async () => {
    const server = serve();
    server.setMarks(SHA, { part: 'tower_dish', marks: D, lookalike_marks: [] });
    const full = 'you already have 3 finder jobs waiting or running; start another when one is done';
    routes[`POST ${FIND}`] = () => ({ status: 429, body: { detail: full } });
    openScreen();
    await findText('3 marked');
    click(await findButton(/Find the rest/));
    expect((await findRole('alert')).textContent).toBe(full);
  });

  it('asks for at least 3 marks before finding the rest', async () => {
    serve().setMarks(SHA, { part: 'tower_dish', marks: D.slice(0, 2), lookalike_marks: [] });
    openScreen();
    await findText('2 marked · 1 more to find the rest');
    expect(byButton(/Find the rest/)).toBeDisabled();
  });

  it('draws a mark with the pointer, in the original photo pixels, then waits for the find', async () => {
    viewport();
    const server = serve();
    server.setMarks(SHA, { part: 'tower_dish', marks: D.slice(0, 2), lookalike_marks: [] });
    openScreen();
    await findText(/^2 marked/);

    const canvas = await loadedCanvas();
    draw(canvas, [600, 100], [650, 160]);
    await findText('3 marked');

    // A tap is not a box.
    pointer(canvas, 'pointerdown', 300, 300);
    pointer(canvas, 'pointerup', 302, 303);

    click(await findButton(/Find the rest/));
    await findText('Waiting in line for the finder…', 'p');
    expect(server.work(SHA).parts.tower_dish?.marks).toEqual([D[0], D[1], [1200, 200, 1300, 320]]);
    expect(byButton(/Find the rest/)).toBeDisabled();
  });

  it("saves another part's unsaved marks before a find, so the server can give them as look-alikes", async () => {
    viewport();
    const server = serve();
    server.setMarks(SHA, { part: 'tower_antenna', marks: D, lookalike_marks: [] });
    openScreen();
    const canvas = await loadedCanvas();

    // A radio unit drawn but not saved, then a find of the antennas.
    click(await findButton(/^Radio unit/));
    await findText('0 marked · 3 more to find the rest');
    draw(canvas, [750, 150], [765, 190]);
    await findText(/^1 marked/);
    click(await findButton(/^Panel antenna/));
    click(await findButton(/Find the rest/));
    await findText('Waiting in line for the finder…', 'p');

    expect(calls.filter((c) => c.method !== 'GET').map((c) => [c.method, c.url, (c.body as { part: string }).part])).toEqual([
      ['PUT', MARKS, 'tower_radio_unit'],
      ['PUT', MARKS, 'tower_antenna'],
      ['POST', FIND, 'tower_antenna'],
    ]);
    for (const c of calls.filter((x) => x.method === 'PUT')) expect((c.body as { lookalike_marks: Box[] }).lookalike_marks).toEqual([]);
    expect(server.work(SHA).parts.tower_antenna?.lookalike_marks).toEqual([[1500, 300, 1530, 380]]);
  });
});

describe('marking screen: checking suggestions', () => {
  it("says only how many wait until the check is saved, then shows the server's count", async () => {
    viewport();
    // Not the real server on purpose: this one also sends a count while suggestions wait, and the screen must not
    // show it. Its review answer counts as the real one does (3 marks + 2 kept).
    const waiting: PartWork = {
      marks: D,
      lookalike_marks: [],
      job: { job_id: 'f'.repeat(64), status: 'done' },
      suggestions: FINDS.map((f, i) => ({ id: `s${i + 1}`, ...f })),
      reviewed: { kept: [], rejected: [], fixed: [], added: [] },
      counted_by_you: 9,
      waiting: 3,
    };
    const empty = { ...waiting, marks: [], job: null, suggestions: [], counted_by_you: null, waiting: 0 };
    const work: PhotoWork = { parts: { tower_dish: waiting, tower_antenna: empty, tower_radio_unit: empty } };
    routes[`GET ${WORK}`] = () => ({ status: 200, body: work });
    routes[`PUT ${REVIEW}`] = (body) => {
      const { part: _part, ...reviewed } = body as { part: string } & PartWork['reviewed'];
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

  it("shows the server's words when the suggestions changed under the check", async () => {
    viewport();
    const server = serve();
    found(server, SHA, FINDS);
    openScreen();
    await findText('3 suggestions waiting for your check', 'p');
    // Someone else changes the marks and finds again: the suggestions on this screen are no longer the server's.
    server.setMarks(SHA, { part: 'tower_dish', marks: D.slice(0, 2), lookalike_marks: [] });
    server.startFind(SHA, { part: 'tower_dish' });
    server.finish(SHA, 'tower_dish', FINDS.slice(0, 1));
    click(await findButton('Next to check'));
    click(await findButton('Reject'));
    click(await findButton(/Save review/));
    expect((await findRole('alert')).textContent).toBe(
      "000000000001-1 is not one of this photo's suggestions for dishes (the suggestions change when the marks do)",
    );
  });

  it('a finder that found no more still opens the review, and saving it gives the count', async () => {
    viewport();
    const server = serve();
    found(server, SHA, []);
    openScreen();

    await findText('The finder found no more: save your review to count them', 'p');
    expect(byText(/The finder found no more microwave dishes\. Draw a box around any it missed/)).not.toBeNull();
    expect(byButton(/Find the rest/)).toBeNull();
    const save = await findButton(/Save review/);
    expect(save).not.toBeDisabled();

    click(save);
    await findText('3 counted by you', 'p');
    expect(called('PUT', REVIEW)?.body).toEqual({ part: 'tower_dish', kept: [], rejected: [], fixed: [], added: [] });
    expect(server.work(SHA).parts.tower_dish?.counted_by_you).toBe(3);
    expect(byButton(/Save review/)).toBeDisabled();
  });

  it('a finder that found no more takes the ones it missed, drawn on the photo', async () => {
    viewport();
    const server = serve();
    found(server, SHA, []);
    openScreen();
    await findText('The finder found no more: save your review to count them', 'p');

    draw(await loadedCanvas(), [600, 100], [650, 160]);
    await findText('All checked: save your review to count them', 'p');
    click(await findButton(/Save review/));
    await findText('4 counted by you', 'p');
    expect(called('PUT', REVIEW)?.body).toEqual({ part: 'tower_dish', kept: [], rejected: [], fixed: [], added: [[1200, 200, 1300, 320]] });
  });
});

describe('marking screen: parts the trained models scan', () => {
  const SCANNED = [
    { id: 'tower_dish' as const, label: 'Microwave dish', plural: 'microwave dishes', scanned: true },
    { id: 'tower_antenna' as const, label: 'Panel antenna', plural: 'panel antennas', scanned: true },
    { id: 'tower_radio_unit' as const, label: 'Radio unit', plural: 'radio units', scanned: true },
  ];
  const none: PartWork = {
    marks: [],
    lookalike_marks: [],
    job: null,
    suggestions: [],
    reviewed: { kept: [], rejected: [], fixed: [], added: [] },
    counted_by_you: null,
    waiting: 0,
  };
  const found = (n: number): PartWork => ({
    ...none,
    job: { job_id: 'e'.repeat(64), status: 'done' },
    suggestions: FINDS.slice(0, n).map((f, i) => ({ id: `s${i + 1}`, ...f })),
    waiting: n,
  });

  it('opens on the part with suggestions waiting and asks only for a check, never for marks', async () => {
    viewport();
    const work: PhotoWork = { parts: { tower_dish: found(0), tower_antenna: found(2), tower_radio_unit: none } };
    routes[`GET ${WORK}`] = () => ({ status: 200, body: work });
    mount(<PhotoMarking layerId={LAYER} layerName="Mast 12" photos={[PHOTO]} parts={SCANNED} scanning startIndex={0} onClose={vi.fn()} />);

    await findText(/The model found 2 panel antennas on this photo/, 'p');
    expect(byButton(/Find the rest/)).toBeNull();
    expect(byButton('Change marks')).toBeNull();
    expect(byText(/Draw a box around at least/)).toBeNull();

    click(await findButton(/Radio unit/));
    await findText(/Looking for radio units on this photo/, 'p');
    click(await findButton(/Microwave dish/));
    await findText(/The model found no microwave dishes on this photo/, 'p');
  });

  it('a photo the scan could not look at falls back to one mark and a find', async () => {
    viewport();
    const work: PhotoWork = { parts: { tower_dish: none, tower_antenna: none, tower_radio_unit: none } };
    routes[`GET ${WORK}`] = () => ({ status: 200, body: work });
    mount(<PhotoMarking layerId={LAYER} layerName="Mast 12" photos={[PHOTO]} parts={SCANNED} startIndex={0} onClose={vi.fn()} />);

    await findText(/This photo was not scanned for microwave dishes/, 'p');
    await findText('0 marked · 1 more to find the rest', 'span');
  });
});

describe('next photo to check', () => {
  const second: FakePhoto = {
    ...PHOTO,
    sha256: 'd'.repeat(64),
    url: `/api/layer/${LAYER}/photos/${'d'.repeat(64)}`,
    filename: 'DJI_0002.JPG',
  };

  it("goes to the photo the server lists, and says the server's words when it is the only one", async () => {
    const server = serve([PHOTO, second]);
    found(server, SHA, []);
    server.setReview(SHA, { part: 'tower_dish', kept: [], rejected: [], fixed: [], added: [] });
    found(server, second.sha256, FINDS);
    openScreen([PHOTO, second]);

    await findText(/^Photo 1 of 2/);
    click(await findButton('Next photo to check'));
    await findText(/^Photo 2 of 2/);
    click(await findButton('Next photo to check'));
    await findText('This is the only photo left to check: 3 suggestions waiting for you.');
    expect(byText(/^Photo 2 of 2/)).not.toBeNull();
  });
});

describe('tower cards panel', () => {
  const layers: MapLayer[] = [{ id: LAYER, name: 'Mast 12', type: 'vector', metadata: { original_format: 'photo_zip' } }];
  const CARDS = `/api/towers/layer/${LAYER}/cards`;
  const PHOTOS = `/api/towers/layer/${LAYER}/photos`;
  const panel = (list: MapLayer[], map: MLMap | null = null) => (
    <TowerCards map={map} layers={list} hiddenLayerIDs={[]} historyOpen={false} onAskSage={vi.fn()} />
  );
  const second: FakePhoto = {
    ...PHOTO,
    sha256: 'd'.repeat(64),
    url: `/api/layer/${LAYER}/photos/${'d'.repeat(64)}`,
    filename: 'DJI_0002.JPG',
  };

  it('shows nothing when the server has no towers here (404)', async () => {
    routes[`GET ${CARDS}?audience=owner`] = () => ({ status: 404, body: { detail: 'Not Found' } });
    routes[`GET ${PHOTOS}`] = () => ({ status: 404, body: { detail: 'Not Found' } });
    const host = mount(panel(layers));
    await vi.waitFor(() => expect(calls.length).toBe(2));
    await vi.waitFor(() => expect(host).toBeEmptyDOMElement());
  });

  it('shows nothing while the server has not answered, nor when the request fails', async () => {
    fetchMock.mockImplementation(
      () =>
        new Promise<Response>(() => {
          // never answers
        }),
    );
    const waiting = mount(panel(layers));
    await vi.waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2));
    expect(waiting).toBeEmptyDOMElement();
    unmountAll();
    fetchMock.mockReset();
    fetchMock.mockImplementation(async () => json(502, { detail: 'Bad Gateway' }));
    const failed = mount(panel(layers));
    await vi.waitFor(() => expect(fetchMock.mock.calls.length).toBeGreaterThanOrEqual(2));
    await new Promise((resolve) => setTimeout(resolve, 50));
    expect(failed).toBeEmptyDOMElement();
  });

  it("shows the server's words when the inspection is on maps of two Telecom projects (409)", async () => {
    const conflict = { status: 409, body: { detail: 'this inspection is on maps of 2 projects; remove it from the maps of the others' } };
    routes[`GET ${CARDS}?audience=owner`] = () => conflict;
    routes[`GET ${PHOTOS}`] = () => conflict;
    const host = mount(panel(layers));
    await vi.waitFor(() => expect(host.textContent).toContain('remove it from the maps of the others'));
  });

  it('asks nothing for layers that are not photo inspections', async () => {
    const host = mount(panel([{ id: 'Lsites', name: 'Sites', type: 'vector', metadata: { original_format: 'geojson' } }]));
    await new Promise((resolve) => setTimeout(resolve, 50));
    expect(calls).toEqual([]);
    expect(host).toBeEmptyDOMElement();
  });

  it('lists the cards for the chosen reader, opens an answer, and opens a listed photo on the marking screen', async () => {
    const noGps: FakePhoto = { ...second, lat: null, lon: null };
    const server = serve([PHOTO, noGps]);
    routes[`GET ${CARDS}?audience=owner`] = () => ({
      status: 200,
      body: {
        cards: [{ id: 'equipment', title: 'What equipment is on this tower?', status: 'cant_tell', preview: 'Nothing counted yet.' }],
      },
    });
    routes[`GET ${CARDS}?audience=technician`] = () => ({
      status: 200,
      body: {
        cards: [{ id: 'photos_to_check', title: 'Which photos still need checking?', status: 'ready', preview: '2 photos to check.' }],
      },
    });
    const map = { flyTo: vi.fn() };
    mount(panel(layers, map as unknown as MLMap));

    await findText('What equipment is on this tower?');
    expect(byText('2 photos')).not.toBeNull();
    click(await findButton('Technician'));
    click(await findText('Which photos still need checking?'));
    await findText(server.photosToCheck().what);
    // The second photo has no GPS position: tapping it opens the photo, never the map at 0, 0.
    click(await findButton(/^DJI_0002\.JPG/));
    await findText(/^Photo 2 of 2/);
    expect(map.flyTo).not.toHaveBeenCalled();
  });

  it('"Check photos" opens the first photo the server lists', async () => {
    // Both photos have counted dishes; the second also has antennas marked and never found. The old client rule
    // (nothing waits and something is counted) said every photo was checked; the server lists the second one.
    const server = serve([PHOTO, second]);
    for (const p of [PHOTO, second]) {
      found(server, p.sha256, []);
      server.setReview(p.sha256, { part: 'tower_dish', kept: [], rejected: [], fixed: [], added: [] });
    }
    server.setMarks(second.sha256, { part: 'tower_antenna', marks: [ANTENNA], lookalike_marks: [] });
    routes[`GET ${CARDS}?audience=owner`] = () => ({ status: 200, body: { cards: [] } });
    mount(panel(layers));
    click(await findButton(/Check photos/));
    await findText(/^Photo 2 of 2/);
    expect(byText(/Every photo/)).toBeNull();
  });

  it('a farm inspection (no tower cards: 404) shows its photos and marks only the parts the server lists', async () => {
    // Agriculture: the server lists maize plants as the only part and has no tower cards there (tower_routes).
    const { filename: _, ...photo } = PHOTO;
    const empty: PartWork = {
      marks: [],
      lookalike_marks: [],
      job: null,
      suggestions: [],
      reviewed: { kept: [], rejected: [], fixed: [], added: [] },
      counted_by_you: null,
      waiting: 0,
    };
    routes[`GET ${PHOTOS}`] = () => ({
      status: 200,
      body: { photos: [photo], parts: [{ id: 'maize_seedling', label: 'Maize plant', plural: 'maize plants' }] },
    });
    routes[`GET ${PHOTOS}/${SHA}/work`] = () => ({ status: 200, body: { parts: { maize_seedling: empty } } });
    routes[`GET ${CARDS}?audience=owner`] = () => ({
      status: 404,
      body: { detail: 'The tower cards are for photo inspections in Telecom Towers projects' },
    });
    mount(panel(layers));
    await findText('Photo checks');
    expect(byText(/tower cards are for/)).toBeNull();
    expect(byButton('Owner')).toBeNull();
    click(await findButton(/Check photos/));
    await findText(/^Photo 1 of 1/);
    expect(await findButton(/Maize plant/)).not.toBeNull();
    expect(byButton(/Microwave dish/)).toBeNull();
    expect(byText(/Check tower photos/)).toBeNull();
  });

  it('"Check photos" says the server\'s words when every photo is checked', async () => {
    const server = serve([PHOTO]);
    found(server, SHA, []);
    server.setReview(SHA, { part: 'tower_dish', kept: [], rejected: [], fixed: [], added: [] });
    routes[`GET ${CARDS}?audience=owner`] = () => ({ status: 200, body: { cards: [] } });
    mount(panel(layers));
    click(await findButton(/Check photos/));
    await findText('Every photo is checked: each photo with marks has its count.');
    expect(byText(/^Photo 1 of 1/)).not.toBeNull();
  });
});

describe('answer places', () => {
  const answer = (items: CardAnswerCore['items']): CardAnswerCore => ({
    what: 'What',
    why: 'Why',
    todo: 'Todo',
    how_sure: { level: 'high', label: 'High', bars: 3, because: [], surer: null },
    facts: [],
    items,
    progress: null,
  });

  it('flies to a place with a position and lists one without a position with no action', async () => {
    const onGoTo = vi.fn();
    mount(
      <AnswerBody
        answer={answer([
          { id: 'a', title: 'Placed', detail: 'here', lon: 30.06, lat: -1.94 },
          { id: 'b', title: 'Unplaced', detail: 'no GPS position', lon: null, lat: null },
        ])}
        onGoTo={onGoTo}
      />,
    );
    click(await findButton(/^Placed/));
    expect(onGoTo).toHaveBeenCalledWith(30.06, -1.94);
    expect(byButton(/^Unplaced/)).toBeNull();
    expect(byText('Unplaced')).not.toBeNull();
  });
});
