import { apiFetch } from '@mundi/ee';
import { type QueryClient, useQueryClient } from '@tanstack/react-query';
import { MemoryRouter } from 'react-router-dom';
import { toast } from 'sonner';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { INDUSTRY_KEY, type IndustryKey, type IndustryState } from '@/components/IndustryGate';
import IndustryPicker from '@/components/IndustryPicker';
import MapsList from '@/components/MapsList';
import { ProjectsProvider, useProjects } from '@/contexts/ProjectsContext';
import type { ProjectList } from '@/lib/types';
import { byButton, byText, click, findButton, findText, mount, unmountAll, unnamedControls } from '@/test/dom';

// The home lists the maps of the industry the user works in now; the server decides which (Roger, 2026-10-12). The
// fake server below answers GET /api/projects/ with the rules of list_user_projects (src/routes/project_routes.py)
// and GET /api/user/industry with the shape of _industry_payload (src/routes/profile_routes.py).
vi.mock('@mundi/ee', () => ({ apiFetch: vi.fn(), useIsReady: () => true }));
vi.mock('@/lib/miniworld', () => ({ buildScene: () => ({}), renderWorld: vi.fn() })); // the picker's drawing needs a real canvas
vi.mock('sonner', () => ({ toast: Object.assign(vi.fn(), { success: vi.fn(), error: vi.fn() }) }));
const fetchMock = vi.mocked(apiFetch);

const INDUSTRIES: { key: IndustryKey; label: string; note: string }[] = [
  { key: 'agriculture', label: 'Agriculture', note: 'Farms, plots and crops' },
  { key: 'telecom', label: 'Telecom Towers', note: 'Masts, antennas and sites' },
];
const label = (key: IndustryKey) => INDUSTRIES.find((i) => i.key === key)?.label ?? key;

interface FakeProject {
  id: string;
  industry: IndustryKey;
  deleted?: boolean;
}

let world: { industry: IndustryKey | null; projects: FakeProject[] };
let calls: string[];
let industryGate: Promise<void> | null; // holds /api/user/industry until released
let listGate: Promise<void> | null; // holds the main-list requests until released
let listFailures: number; // the next N main-list requests answer 500

function industryState(): IndustryState {
  return {
    industry: world.industry,
    source: world.industry ? 'you' : null,
    company: null,
    can_choose: true,
    locked: false,
    options: INDUSTRIES,
  };
}

function projectList(url: URL): ProjectList {
  const page = Number(url.searchParams.get('page') ?? 1);
  const limit = Number(url.searchParams.get('limit') ?? 12);
  const includeDeleted = url.searchParams.get('include_deleted') === 'true';
  const shown = world.industry;
  const listable = world.projects.filter((p) => includeDeleted || !p.deleted);
  const listed = listable.filter((p) => shown === null || p.industry === shown);
  return {
    projects: listed.slice((page - 1) * limit, page * limit).map((p) => ({
      id: p.id,
      title: p.id,
      maps: [],
      created_on: '2026-10-12T08:00:00Z',
      soft_deleted_at: p.deleted ? '2026-10-12T09:00:00Z' : undefined,
      industry: p.industry,
    })),
    total_pages: Math.ceil(listed.length / limit),
    total_items: listed.length,
    industry: shown,
    industry_label: shown ? label(shown) : null,
    other_industries:
      shown === null
        ? []
        : INDUSTRIES.filter((i) => i.key !== shown)
            .map((i) => ({ industry: i.key, label: i.label, count: listable.filter((p) => p.industry === i.key).length }))
            .filter((o) => o.count > 0),
  };
}

function json(status: number, body: unknown) {
  return new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } });
}

function projects(industry: IndustryKey, n: number, prefix: string): FakeProject[] {
  return Array.from({ length: n }, (_, i) => ({ id: `${prefix}${i + 1}`, industry }));
}

let client: QueryClient;
function Probe() {
  client = useQueryClient();
  const { allProjects } = useProjects();
  return <div data-testid="recent">{allProjects.map((p) => p.id).join(',')}</div>;
}

function renderHome(picker = false) {
  mount(
    <MemoryRouter>
      <ProjectsProvider>
        <MapsList />
        <Probe />
        {picker && <IndustryPicker data={industryState()} onClose={vi.fn()} />}
      </ProjectsProvider>
    </MemoryRouter>,
  );
}

/** Waits until the home lists `label`'s maps: the title, and that industry's tab lit with its count. */
async function showing(label: string, count: number) {
  await findText('Maps', 'h1');
  await vi.waitFor(() => expect(tabs()).toContain(`*${label} ${count}`));
}
const title = () => document.querySelector('h1')?.textContent?.replace(/\s+/g, ' ').trim();
const listCalls = () => calls.filter((u) => u.startsWith('/api/projects/'));
const pageCalls = () => calls.filter((u) => u.startsWith('/api/projects/?'));

function pageLink(n: number): Element {
  const link = [...document.querySelectorAll('a[data-slot="pagination-link"]')].find((a) => a.textContent === String(n));
  if (!link) throw new Error(`no link to page ${n}`);
  return link;
}

/** The home's industry tabs as "label count" (count left out when the tab shows none), the current one starred. */
const tabs = () =>
  [...document.querySelectorAll('[role="tab"]')].map((t) => {
    const count = t.querySelector('span')?.textContent;
    const name = (t.textContent ?? '').replace(count ?? '', '').trim();
    return `${t.getAttribute('aria-selected') === 'true' ? '*' : ''}${name}${count ? ` ${count}` : ''}`;
  });
const tab = (name: string) => {
  const found = [...document.querySelectorAll('[role="tab"]')].find((t) => t.textContent?.startsWith(name));
  if (!found) throw new Error(`no tab ${name}`);
  return found;
};

/** Holds the main-list requests until the returned function is called. */
function holdLists(): () => void {
  let release!: () => void;
  listGate = new Promise<void>((resolve) => {
    release = resolve;
  });
  return () => {
    listGate = null;
    release();
  };
}

/** Switches industry the way the picker does once the server saved it: it replaces the industry query's data. */
function switchTo(key: IndustryKey) {
  world.industry = key;
  client.setQueryData(INDUSTRY_KEY, industryState());
}

beforeEach(() => {
  world = { industry: 'telecom', projects: [] };
  calls = [];
  industryGate = null;
  listGate = null;
  listFailures = 0;
  vi.stubGlobal(
    'ResizeObserver',
    class {
      observe = vi.fn();
      unobserve = vi.fn();
      disconnect = vi.fn();
    },
  );
  fetchMock.mockImplementation(async (input, init) => {
    const path = String(input);
    calls.push(path);
    const url = new URL(path, 'http://ingabe.test');
    if (url.pathname === '/api/user/industry') {
      if (init?.method === 'PUT') {
        world.industry = JSON.parse(String(init.body)).industry;
        return json(200, industryState());
      }
      if (industryGate) await industryGate;
      return json(200, industryState());
    }
    if (url.pathname === '/api/projects/') {
      if (url.searchParams.has('page') && listGate) await listGate;
      if (url.searchParams.has('page') && listFailures > 0) {
        listFailures -= 1;
        return json(500, { detail: 'boom' });
      }
      return json(200, projectList(url));
    }
    return json(404, { detail: `No route ${path}` });
  });
});

afterEach(() => {
  unmountAll();
  fetchMock.mockReset();
  vi.unstubAllGlobals();
});

describe('the home lists the maps of the industry the user works in now', () => {
  it('waits for the industry before asking for any list', async () => {
    world.projects = projects('telecom', 1, 'T');
    let release!: () => void;
    industryGate = new Promise<void>((resolve) => {
      release = resolve;
    });
    renderHome();
    await vi.waitFor(() => expect(calls).toContain('/api/user/industry'));
    await new Promise((resolve) => setTimeout(resolve, 50));
    expect(listCalls()).toEqual([]);
    expect(byText('No maps yet')).toBeNull(); // still loading, not an empty home

    release();
    await showing('Telecom Towers', 1);
    expect(listCalls().length).toBeGreaterThan(0);
  });

  it('names the industry, lists its maps only, and has a tab per industry with its count', async () => {
    world.projects = [...projects('telecom', 1, 'T'), ...projects('agriculture', 1, 'A')];
    renderHome();
    await showing('Telecom Towers', 1);
    expect(byText('T1')).not.toBeNull();
    expect(byText('A1')).toBeNull();
    await vi.waitFor(() => expect(tabs()).toEqual(['Agriculture 1', '*Telecom Towers 1']));
    // The server decides the industry: the client never sends one with a list.
    expect(calls.some((u) => u.includes('industry='))).toBe(false);
  });

  it('switches industry in one tap on its tab, without the full-screen picker (Roger, 2026-10-22)', async () => {
    world.projects = [...projects('telecom', 1, 'T'), ...projects('agriculture', 2, 'A')];
    renderHome();
    await showing('Telecom Towers', 1);
    await vi.waitFor(() => expect(tabs()).toContain('Agriculture 2'));

    click(tab('Agriculture'));
    await showing('Agriculture', 2);
    expect(byText('A1')).not.toBeNull();
    expect(byText('T1')).toBeNull();
    const puts = fetchMock.mock.calls.filter(([url, init]) => url === '/api/user/industry' && init?.method === 'PUT');
    expect(puts.map(([, init]) => JSON.parse(String(init?.body)).industry)).toEqual(['agriculture']);
    expect(document.querySelector('[role="dialog"]')).toBeNull();
    await vi.waitFor(() => expect(tabs()).toEqual(['*Agriculture 2', 'Telecom Towers 1']));
    // The sidebar's recent maps follow the switch too.
    await vi.waitFor(() => expect(document.querySelector('[data-testid="recent"]')?.textContent).toBe('A1,A2'));
  });

  it('goes back to page 1 when the industry changes, and never asks for page 2 of the new one', async () => {
    world.projects = [...projects('telecom', 13, 'T'), ...projects('agriculture', 2, 'A')];
    renderHome();
    await showing('Telecom Towers', 13);
    click(pageLink(2));
    await findText('T13');
    expect(listCalls().some((u) => u.includes('page=2&'))).toBe(true);

    const before = calls.length;
    switchTo('agriculture');
    await showing('Agriculture', 2);
    const after = calls.slice(before).filter((u) => u.startsWith('/api/projects/?'));
    expect(after.length).toBeGreaterThan(0);
    expect(after.every((u) => u.includes('page=1&'))).toBe(true);
    // The sidebar's recent maps follow the switch too.
    await vi.waitFor(() => expect(document.querySelector('[data-testid="recent"]')?.textContent).toBe('A1,A2'));
  });

  it('opens page 1 again when switching back to an industry left on page 2', async () => {
    world.projects = [...projects('telecom', 13, 'T'), ...projects('agriculture', 2, 'A')];
    renderHome();
    await showing('Telecom Towers', 13);
    click(pageLink(2));
    await findText('T13');
    switchTo('agriculture');
    await showing('Agriculture', 2);

    const before = calls.length;
    switchTo('telecom');
    await showing('Telecom Towers', 13);
    await findText('T1');
    const after = calls.slice(before).filter((u) => u.startsWith('/api/projects/?'));
    expect(after.length).toBeGreaterThan(0);
    expect(after.every((u) => u.includes('page=1&'))).toBe(true);
    expect(byText('T13')).toBeNull();
  });

  it('keeps naming the industry while a page or another industry loads', async () => {
    world.projects = [...projects('telecom', 13, 'T'), ...projects('agriculture', 2, 'A')];
    renderHome();
    await showing('Telecom Towers', 13);
    await vi.waitFor(() => expect(tabs()).toContain('Agriculture 2'));

    // Another page of the same industry: the current one stays, with its header and the other industries' counts.
    let release = holdLists();
    click(pageLink(2));
    await vi.waitFor(() => expect(pageCalls().some((u) => u.includes('page=2&'))).toBe(true));
    expect(title()).toBe('Maps');
    expect(tabs()).toContain('*Telecom Towers 13');
    expect(tabs()).toContain('Agriculture 2');
    release();
    await findText('T13');

    // Another industry: its name while it loads, without a count (unknown, not 0), never the every-industry header.
    release = holdLists();
    const before = pageCalls().length;
    switchTo('agriculture');
    await vi.waitFor(() => expect(pageCalls().length).toBeGreaterThan(before));
    expect(title()).toBe('Maps');
    expect(tabs()).toContain('*Agriculture'); // no count while it loads: unknown, not 0
    release();
    await showing('Agriculture', 2);
  });

  it('follows the server when the industry was switched in another tab', async () => {
    world.projects = [...projects('telecom', 1, 'T'), ...projects('agriculture', 2, 'A')];
    renderHome();
    await showing('Telecom Towers', 1);
    const industryCalls = () => calls.filter((u) => u === '/api/user/industry').length;
    const asked = industryCalls();

    world.industry = 'agriculture'; // saved in another tab: this tab still holds Telecom Towers
    client.invalidateQueries({ queryKey: ['projects'] }); // any later reload of the lists (New Map, going home)
    await showing('Agriculture', 2);
    await vi.waitFor(() => expect(client.getQueryData<IndustryState>(INDUSTRY_KEY)?.industry).toBe('agriculture'));
    expect(industryCalls()).toBe(asked + 1);
  });

  it('reloads the lists when the picker saves, even the industry they already show', async () => {
    world.projects = projects('telecom', 1, 'T');
    renderHome(true);
    await showing('Telecom Towers', 1);
    const before = pageCalls().length;
    click(await findButton(/Continue with Telecom Towers/));
    await vi.waitFor(() => expect(calls).toContain('/api/user/industry'));
    await vi.waitFor(() => expect(pageCalls().length).toBeGreaterThan(before), { timeout: 3000 });
  });

  it('keeps the industry when showing recently deleted maps', async () => {
    world.projects = [
      { id: 'T1', industry: 'telecom' },
      { id: 'T2', industry: 'telecom', deleted: true },
      { id: 'A1', industry: 'agriculture', deleted: true },
    ];
    renderHome();
    await showing('Telecom Towers', 1);
    click(await findButton('Recently deleted'));
    await findText('Recently deleted', 'h1');
    await vi.waitFor(() => expect(tabs()).toContain('*Telecom Towers 2'));
    expect(listCalls().some((u) => u.includes('include_deleted=true'))).toBe(true);
    expect(byText('A1')).toBeNull();
  });

  it('lists every industry when nobody has chosen', async () => {
    world.industry = null;
    world.projects = [...projects('telecom', 1, 'T'), ...projects('agriculture', 1, 'A')];
    renderHome();
    await findText('T1');
    expect(title()).toBe('Maps');
    expect(byText('A1')).not.toBeNull();
    expect(tabs()).toEqual([]); // nothing chosen: no industry to light
    expect(byText(/in another industry|in other industries/, 'p')).toBeNull();
  });

  it('says the maps could not load, without the server words, and loads again from "Try again"', async () => {
    world.projects = projects('telecom', 1, 'T');
    listFailures = 1;
    renderHome();
    await findText("Couldn't load your maps", '[role="alert"] p');
    expect(document.body.textContent).not.toMatch(/500|Failed to fetch/);
    click(await findButton('Try again'));
    await showing('Telecom Towers', 1);
    expect(byText('T1')).not.toBeNull();
  });
});

describe('the home is plain to use', () => {
  it('gives every button, link and field a name, the delete buttons included', async () => {
    world.projects = projects('telecom', 13, 'T');
    renderHome();
    await showing('Telecom Towers', 13);
    expect(document.querySelector('button[aria-label="Delete T1"]')).not.toBeNull();
    expect(unnamedControls()).toEqual([]);
  });

  it('says "New map", not "Creating", while the list is only loading', async () => {
    world.projects = projects('telecom', 1, 'T');
    const release = holdLists();
    renderHome();
    await vi.waitFor(() => expect(pageCalls().length).toBeGreaterThan(0));
    expect(byButton('New map')).not.toBeNull();
    expect(document.body.textContent).not.toMatch(/Creating/);
    release();
    await showing('Telecom Towers', 1);
  });

  it('tells the person when a new map could not be made', async () => {
    renderHome(); // the fake server has no /api/maps/create: the request fails
    await findText('No maps yet');
    click(await findButton('New map'));
    await vi.waitFor(() => expect(toast.error).toHaveBeenCalledWith("Couldn't make a new map. Try again."));
  });
});
