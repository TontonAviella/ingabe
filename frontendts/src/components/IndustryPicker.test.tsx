import { apiFetch } from '@mundi/ee';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { IndustryState } from '@/components/IndustryGate';
import IndustryPicker from '@/components/IndustryPicker';
import { byButton, byRole, click, findButton, mount, unmountAll } from '@/test/dom';

// The picker saves the user's own industry: a switch changes what they see and where their new maps go, never their
// company's industry (Roger, 2026-10-12), and nobody is locked out of switching.
vi.mock('@mundi/ee', () => ({ apiFetch: vi.fn() }));
vi.mock('@/lib/miniworld', () => ({ buildScene: () => ({}), renderWorld: vi.fn() })); // drawing needs a real canvas
const fetchMock = vi.mocked(apiFetch);

const OPTIONS: IndustryState['options'] = [
  { key: 'agriculture', label: 'Agriculture', note: 'Farms, plots and crops' },
  { key: 'power_grid', label: 'Power Grid', note: 'Transmission and distribution lines' },
  { key: 'telecom', label: 'Telecom Towers', note: 'Masts, antennas and sites' },
];

// A member of a company whose default is Telecom Towers, who never chose their own (as the server answers).
const MEMBER: IndustryState = {
  industry: 'telecom',
  source: 'company',
  company: { name: 'Mast Co', industry: 'telecom' },
  can_choose: true,
  options: OPTIONS,
};

function json(status: number, body: unknown) {
  return new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } });
}

beforeEach(() => {
  vi.stubGlobal(
    'ResizeObserver',
    class {
      observe = vi.fn();
      disconnect = vi.fn();
    },
  );
});

afterEach(() => {
  unmountAll();
  fetchMock.mockReset();
  vi.unstubAllGlobals();
});

describe('IndustryPicker', () => {
  it('says a switch changes only what you see and where your new maps go, not the company', async () => {
    mount(<IndustryPicker data={MEMBER} onClose={vi.fn()} />);
    await findButton('Keep Telecom Towers');
    const text = document.body.textContent ?? '';
    expect(text).toContain('Your industry');
    expect(text).toContain(
      "Each map belongs to one industry. Switching changes what you see and where your new maps go, only for you: Mast Co's industry stays as it is.",
    );
    expect(text).not.toMatch(/owners and admins|Ask an owner/);
  });

  it('lets a company member switch, and saves it as their own choice', async () => {
    fetchMock.mockResolvedValue(json(200, { ...MEMBER, industry: 'agriculture', source: 'you' }));
    mount(<IndustryPicker data={MEMBER} onClose={vi.fn()} />);
    const agriculture = await vi.waitFor(() => {
      const card = byRole('radio', /Agriculture/);
      if (!card) throw new Error('no Agriculture card');
      return card;
    });
    expect(agriculture.getAttribute('aria-disabled')).toBeNull();
    click(agriculture);
    click(await findButton(/Continue with Agriculture/));
    await vi.waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));
    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe('/api/user/industry');
    expect(init?.method).toBe('PUT');
    expect(JSON.parse(String(init?.body))).toEqual({ industry: 'agriculture' });
  });

  it('asks a new user once, and says they can switch later', async () => {
    mount(<IndustryPicker data={{ ...MEMBER, industry: null, source: null, company: null }} onClose={vi.fn()} />);
    await findButton(/^Choose your industry/);
    expect(byButton(/^Keep /)).toBeNull();
    expect(document.body.textContent).toContain('Each map belongs to one industry; you can switch from the sidebar at any time.');
  });
});
