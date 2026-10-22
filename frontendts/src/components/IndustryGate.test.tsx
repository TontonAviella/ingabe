import { apiFetch } from '@mundi/ee';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { toast } from 'sonner';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { INDUSTRY_KEY, type IndustryState, useFollowProjectIndustry } from '@/components/IndustryGate';
import { findBy, mount, unmountAll } from '@/test/dom';

// One industry at a time (Roger, 2026-10-13): opening a project of another industry switches the user into it, so the
// sidebar, the lists, the cards and Sage never disagree about where they are.
vi.mock('@mundi/ee', () => ({ apiFetch: vi.fn() }));
vi.mock('sonner', () => ({ toast: Object.assign(vi.fn(), { error: vi.fn() }) }));
const fetchMock = vi.mocked(apiFetch);

const OPTIONS: IndustryState['options'] = [
  { key: 'agriculture', label: 'Agriculture', note: 'Farms, plots and crops' },
  { key: 'power_grid', label: 'Power Grid', note: 'Transmission and distribution lines' },
  { key: 'telecom', label: 'Telecom Towers', note: 'Masts, antennas and sites' },
];
const state = (industry: IndustryState['industry']): IndustryState => ({
  industry,
  source: 'you',
  company: null,
  can_choose: true,
  options: OPTIONS,
});

function Project({ industry }: { industry?: 'agriculture' | 'power_grid' | 'telecom' }) {
  useFollowProjectIndustry(industry);
  return null;
}

function open(user: IndustryState, project?: 'agriculture' | 'power_grid' | 'telecom', copies = 1) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  client.setQueryData(INDUSTRY_KEY, user);
  mount(
    <QueryClientProvider client={client}>
      {Array.from({ length: copies }, (_, i) => (
        <Project key={i} industry={project} />
      ))}
    </QueryClientProvider>,
  );
  return client;
}

afterEach(() => {
  unmountAll();
  fetchMock.mockReset();
  vi.mocked(toast).mockReset();
});

describe('one industry at a time', () => {
  it('opening a Telecom project while in Power Grid switches the user into Telecom and says so', async () => {
    fetchMock.mockImplementation(async (_url, init) => {
      if (init?.method === 'PUT') return new Response(JSON.stringify(state('telecom')), { status: 200 });
      return new Response(JSON.stringify(state('power_grid')), { status: 200 });
    });
    const client = open(state('power_grid'), 'telecom');
    await findBy(() => (vi.mocked(toast).mock.calls.length ? true : null), 'the switch notice');
    const put = fetchMock.mock.calls.find(([, init]) => init?.method === 'PUT');
    expect(put?.[0]).toBe('/api/user/industry');
    expect(JSON.parse(String(put?.[1]?.body))).toEqual({ industry: 'telecom' });
    expect(client.getQueryData<IndustryState>(INDUSTRY_KEY)?.industry).toBe('telecom');
    expect(vi.mocked(toast).mock.calls[0][0]).toBe('This project is Telecom Towers: you are working in Telecom Towers now.');
  });

  it('a project without an industry is Agriculture, and nothing is switched when the user is already there', async () => {
    fetchMock.mockResolvedValue(new Response(JSON.stringify(state('agriculture')), { status: 200 }));
    open(state('agriculture'));
    await new Promise((r) => setTimeout(r, 20));
    expect(fetchMock.mock.calls.some(([, init]) => init?.method === 'PUT')).toBe(false);
    expect(toast).not.toHaveBeenCalled();
  });

  it('a map that mounts twice sends the switch once and says so once (2026-10-22: two PUTs within a second)', async () => {
    let release!: () => void;
    const held = new Promise<void>((resolve) => {
      release = resolve;
    });
    fetchMock.mockImplementation(async (_url, init) => {
      if (init?.method === 'PUT') {
        await held;
        return new Response(JSON.stringify(state('telecom')), { status: 200 });
      }
      return new Response(JSON.stringify(state('agriculture')), { status: 200 });
    });
    open(state('agriculture'), 'telecom', 2);
    await new Promise((r) => setTimeout(r, 20));
    release();
    await findBy(() => (vi.mocked(toast).mock.calls.length ? true : null), 'the switch notice');
    await new Promise((r) => setTimeout(r, 20));
    expect(fetchMock.mock.calls.filter(([, init]) => init?.method === 'PUT')).toHaveLength(1);
    expect(toast).toHaveBeenCalledTimes(1);
  });
});
