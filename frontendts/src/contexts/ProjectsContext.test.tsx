import { apiFetch } from '@mundi/ee';
import { type QueryClient, useQueryClient } from '@tanstack/react-query';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { INDUSTRY_KEY, type IndustryKey, type IndustryState, OPEN_INDUSTRY_PICKER } from '@/components/IndustryGate';
import MapsList from '@/components/MapsList';
import { ProjectsProvider, useProjects } from '@/contexts/ProjectsContext';
import type { ProjectList } from '@/lib/types';
import { byText, click, findButton, findText, mount, unmountAll } from '@/test/dom';

// The home lists the maps of the industry the user works in now; the server decides which (Roger, 2026-10-12). The
// fake server below answers GET /api/projects/ with the rules of list_user_projects (src/routes/project_routes.py)
// and GET /api/user/industry with the shape of _industry_payload (src/routes/profile_routes.py).
vi.mock('@mundi/ee', () => ({ apiFetch: vi.fn(), useIsReady: () => true }));
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

function renderHome() {
  mount(
    <MemoryRouter>
      <ProjectsProvider>
        <MapsList />
        <Probe />
      </ProjectsProvider>
    </MemoryRouter>,
  );
}

const heading = () =>
  byText(/maps \(/, 'h1')
    ?.textContent?.replace(/\s+/g, ' ')
    .trim();
const listCalls = () => calls.filter((u) => u.startsWith('/api/projects/'));

/** Switches industry the way the picker does once the server saved it: it replaces the industry query's data. */
function switchTo(key: IndustryKey) {
  world.industry = key;
  client.setQueryData(INDUSTRY_KEY, industryState());
}

beforeEach(() => {
  world = { industry: 'telecom', projects: [] };
  calls = [];
  industryGate = null;
  listFailures = 0;
  vi.stubGlobal(
    'ResizeObserver',
    class {
      observe = vi.fn();
      unobserve = vi.fn();
      disconnect = vi.fn();
    },
  );
  fetchMock.mockImplementation(async (input) => {
    const path = String(input);
    calls.push(path);
    const url = new URL(path, 'http://ingabe.test');
    if (url.pathname === '/api/user/industry') {
      if (industryGate) await industryGate;
      return json(200, industryState());
    }
    if (url.pathname === '/api/projects/') {
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

  it('names the industry and counts its maps only, with one line for the other industries', async () => {
    world.projects = [...projects('telecom', 1, 'T'), ...projects('agriculture', 1, 'A')];
    renderHome();
    await findText('Your Telecom Towers maps (1 project)', 'h1');
    expect(byText('T1')).not.toBeNull();
    expect(byText('A1')).toBeNull();
    await findText('1 Agriculture map is in another industry. Switch industry to see it.', 'p');
    // The server decides the industry: the client never sends one.
    expect(calls.some((u) => u.includes('industry='))).toBe(false);

    const opened = vi.fn();
    window.addEventListener(OPEN_INDUSTRY_PICKER, opened);
    click(await findButton('Switch industry'));
    window.removeEventListener(OPEN_INDUSTRY_PICKER, opened);
    expect(opened).toHaveBeenCalledTimes(1);
  });

  it('words several other industries in one line', async () => {
    world.projects = [...projects('telecom', 1, 'T'), ...projects('agriculture', 2, 'A'), ...projects('power_grid', 1, 'G')];
    renderHome();
    await findText('2 Agriculture maps and 1 Power Grid map are in other industries. Switch industry to see them.', 'p');
  });

  it('goes back to page 1 when the industry changes, and never asks for page 2 of the new one', async () => {
    world.projects = [...projects('telecom', 13, 'T'), ...projects('agriculture', 2, 'A')];
    renderHome();
    await findText('Your Telecom Towers maps (13 projects)', 'h1');
    const pageTwo = [...document.querySelectorAll('a[data-slot="pagination-link"]')].find((a) => a.textContent === '2');
    expect(pageTwo).toBeDefined();
    click(pageTwo as Element);
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
