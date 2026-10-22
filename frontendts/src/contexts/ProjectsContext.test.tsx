import { apiFetch } from '@mundi/ee';
import { type QueryClient, useQueryClient } from '@tanstack/react-query';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { INDUSTRY_KEY, type IndustryKey, type IndustryState } from '@/components/IndustryGate';
import IndustryPicker from '@/components/IndustryPicker';
import MapsList from '@/components/MapsList';
import { ProjectsProvider, useProjects } from '@/contexts/ProjectsContext';
import type { ProjectList } from '@/lib/types';
import { byText, click, findButton, findText, mount, unmountAll } from '@/test/dom';

// The home lists the maps of the industry the user works in now; the server decides which (Roger, 2026-10-12). The
// fake server below answers GET /api/projects/ with the rules of list_user_projects (src/routes/project_routes.py)
// and GET /api/user/industry with the shape of _industry_payload (src/routes/profile_routes.py).
vi.mock('@mundi/ee', () => ({ apiFetch: vi.fn(), useIsReady: () => true }));
vi.mock('@/lib/miniworld', () => ({ buildScene: () => ({}), renderWorld: vi.fn() })); // the picker's drawing needs a real canvas
const fetchMock = vi.mocked(apiFetch);

const INDUSTRIES: { key: IndustryKey; label: string; note: string }[] = [
  { key: 'agriculture', label: 'Agriculture', note: 'Farms, plots and crops' },
  { key: 'power_grid', label: 'Power Grid', note: 'Transmission and distribution lines' },
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

const heading = () =>
  byText(/maps \(/, 'h1')
    ?.textContent?.replace(/\s+/g, ' ')
    .trim();
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
    await findText('Your Telecom Towers maps (1 project)', 'h1');
    expect(listCalls().length).toBeGreaterThan(0);
  });

  it('names the industry, lists its maps only, and has a tab per industry with its count', async () => {
    world.projects = [...projects('telecom', 1, 'T'), ...projects('agriculture', 1, 'A')];
    renderHome();
    await findText('Your Telecom Towers maps (1 project)', 'h1');
    expect(byText('T1')).not.toBeNull();
    expect(byText('A1')).toBeNull();
    await vi.waitFor(() => expect(tabs()).toEqual(['Agriculture 1', 'Power Grid', '*Telecom Towers 1']));
    // The server decides the industry: the client never sends one with a list.
    expect(calls.some((u) => u.includes('industry='))).toBe(false);
  });

  it('switches industry in one tap on its tab, without the full-screen picker (Roger, 2026-10-22)', async () => {
    world.projects = [...projects('telecom', 1, 'T'), ...projects('agriculture', 2, 'A')];
    renderHome();
    await findText('Your Telecom Towers maps (1 project)', 'h1');
    await vi.waitFor(() => expect(tabs()).toContain('Agriculture 2'));

    click(tab('Agriculture'));
    await findText('Your Agriculture maps (2 projects)', 'h1');
    expect(byText('A1')).not.toBeNull();
    expect(byText('T1')).toBeNull();
    const puts = fetchMock.mock.calls.filter(([url, init]) => url === '/api/user/industry' && init?.method === 'PUT');
    expect(puts.map(([, init]) => JSON.parse(String(init?.body)).industry)).toEqual(['agriculture']);
    expect(document.querySelector('[role="dialog"]')).toBeNull();
    await vi.waitFor(() => expect(tabs()).toEqual(['*Agriculture 2', 'Power Grid', 'Telecom Towers 1']));
    // The sidebar's recent maps follow the switch too.
    await vi.waitFor(() => expect(document.querySelector('[data-testid="recent"]')?.textContent).toBe('A1,A2'));
  });

  it('counts several other industries in their tabs', async () => {
    world.projects = [...projects('telecom', 1, 'T'), ...projects('agriculture', 2, 'A'), ...projects('power_grid', 1, 'G')];
    renderHome();
    await vi.waitFor(() => expect(tabs()).toEqual(['Agriculture 2', 'Power Grid 1', '*Telecom Towers 1']));
  });

  it('goes back to page 1 when the industry changes, and never asks for page 2 of the new one', async () => {
    world.projects = [...projects('telecom', 13, 'T'), ...projects('agriculture', 2, 'A')];
    renderHome();
    await findText('Your Telecom Towers maps (13 projects)', 'h1');
    click(pageLink(2));
    await findText('T13');
    expect(listCalls().some((u) => u.includes('page=2&'))).toBe(true);

    const before = calls.length;
    switchTo('agriculture');
    await findText('Your Agriculture maps (2 projects)', 'h1');
    const after = calls.slice(before).filter((u) => u.startsWith('/api/projects/?'));
    expect(after.length).toBeGreaterThan(0);
    expect(after.every((u) => u.includes('page=1&'))).toBe(true);
    // The sidebar's recent maps follow the switch too.
    await vi.waitFor(() => expect(document.querySelector('[data-testid="recent"]')?.textContent).toBe('A1,A2'));
  });

  it('opens page 1 again when switching back to an industry left on page 2', async () => {
    world.projects = [...projects('telecom', 13, 'T'), ...projects('agriculture', 2, 'A')];
    renderHome();
    await findText('Your Telecom Towers maps (13 projects)', 'h1');
    click(pageLink(2));
    await findText('T13');
    switchTo('agriculture');
    await findText('Your Agriculture maps (2 projects)', 'h1');

    const before = calls.length;
    switchTo('telecom');
    await findText('Your Telecom Towers maps (13 projects)', 'h1');
    await findText('T1');
    const after = calls.slice(before).filter((u) => u.startsWith('/api/projects/?'));
    expect(after.length).toBeGreaterThan(0);
    expect(after.every((u) => u.includes('page=1&'))).toBe(true);
    expect(byText('T13')).toBeNull();
  });

  it('keeps naming the industry while a page or another industry loads', async () => {
    world.projects = [...projects('telecom', 13, 'T'), ...projects('agriculture', 2, 'A')];
    renderHome();
    await findText('Your Telecom Towers maps (13 projects)', 'h1');
    await vi.waitFor(() => expect(tabs()).toContain('Agriculture 2'));

    // Another page of the same industry: the current one stays, with its header and the other industries' counts.
    let release = holdLists();
    click(pageLink(2));
    await vi.waitFor(() => expect(pageCalls().some((u) => u.includes('page=2&'))).toBe(true));
    expect(title()).toBe('Your Telecom Towers maps (13 projects)');
    expect(tabs()).toContain('Agriculture 2');
    release();
    await findText('T13');

    // Another industry: its name while it loads, without a count (unknown, not 0), never the every-industry header.
    release = holdLists();
    const before = pageCalls().length;
    switchTo('agriculture');
    await vi.waitFor(() => expect(pageCalls().length).toBeGreaterThan(before));
    expect(title()).toBe('Your Agriculture maps');
    release();
    await findText('Your Agriculture maps (2 projects)', 'h1');
  });

  it('follows the server when the industry was switched in another tab', async () => {
    world.projects = [...projects('telecom', 1, 'T'), ...projects('agriculture', 2, 'A')];
    renderHome();
    await findText('Your Telecom Towers maps (1 project)', 'h1');
    const industryCalls = () => calls.filter((u) => u === '/api/user/industry').length;
    const asked = industryCalls();

    world.industry = 'agriculture'; // saved in another tab: this tab still holds Telecom Towers
    client.invalidateQueries({ queryKey: ['projects'] }); // any later reload of the lists (New Map, going home)
    await findText('Your Agriculture maps (2 projects)', 'h1');
    await vi.waitFor(() => expect(client.getQueryData<IndustryState>(INDUSTRY_KEY)?.industry).toBe('agriculture'));
    expect(industryCalls()).toBe(asked + 1);
  });

  it('reloads the lists when the picker saves, even the industry they already show', async () => {
    world.projects = projects('telecom', 1, 'T');
    renderHome(true);
    await findText('Your Telecom Towers maps (1 project)', 'h1');
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
    await findText('Your Telecom Towers maps (1 project)', 'h1');
    const checkbox = document.querySelector('[role="checkbox"]');
    expect(checkbox).not.toBeNull();
    click(checkbox as Element);
    await findText('Recently deleted Telecom Towers maps (2 projects)', 'h1');
    expect(listCalls().some((u) => u.includes('include_deleted=true'))).toBe(true);
    expect(byText('A1')).toBeNull();
  });

  it('lists every industry when nobody has chosen', async () => {
    world.industry = null;
    world.projects = [...projects('telecom', 1, 'T'), ...projects('agriculture', 1, 'A')];
    renderHome();
    await findText('Your maps (2 projects)', 'h1');
    expect(heading()).toBe('Your maps (2 projects)');
    expect(byText(/in another industry|in other industries/, 'p')).toBeNull();
  });

  it('loads the list again from "Try Again" after an error', async () => {
    world.projects = projects('telecom', 1, 'T');
    listFailures = 1;
    renderHome();
    await findText('Error Loading Maps');
    click(await findButton('Try Again'));
    await findText('Your Telecom Towers maps (1 project)', 'h1');
    expect(byText('T1')).not.toBeNull();
  });
});
