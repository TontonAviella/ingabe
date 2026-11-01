import { afterEach, describe, expect, it, vi } from 'vitest';
import Companies from '@/pages/Companies';
import { byButton, click, findButton, findText, mount, unmountAll } from '@/test/dom';

// People asking to join (src/services/access_requests.py): staff see each open request on the Companies page and
// approve it with an invitation into a company they pick, or decline it.
vi.mock('@/components/auth/WorkOSSession', () => ({
  useWorkOSSession: () => ({ me: { is_staff: true, is_owner: false, user: { email: 'staff@ingabe.rw' } } }),
}));
vi.mock('sonner', () => ({ toast: Object.assign(vi.fn(), { success: vi.fn(), error: vi.fn() }) }));

const REQUEST = {
  id: 7,
  email: 'alain@example.rw',
  name: 'Alain',
  organization: 'Youth Hub',
  reason: 'Help promote Ingabe',
  source: 'sign_up_attempt',
  attempts: 2,
  created_at: '2026-11-01T04:23:17+00:00',
  status: 'open',
  decided_by: null,
  decided_at: null,
  decided_how: null,
};

function respond(body: unknown) {
  return Promise.resolve(new Response(JSON.stringify(body), { status: 200, headers: { 'Content-Type': 'application/json' } }));
}

function choose(select: HTMLSelectElement, value: string) {
  const setter = Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype, 'value')?.set;
  setter?.call(select, value);
  select.dispatchEvent(new Event('change', { bubbles: true }));
}

afterEach(() => {
  unmountAll();
  vi.unstubAllGlobals();
});

describe('Companies: people asking to join', () => {
  it('shows who asked and how, and approves with an invitation into the chosen company', async () => {
    const calls: { url: string; init?: RequestInit }[] = [];
    vi.stubGlobal(
      'fetch',
      vi.fn((url: string, init?: RequestInit) => {
        calls.push({ url, init });
        if (url.endsWith('/access-requests')) return respond({ requests: [REQUEST] });
        if (url.endsWith('/blocked')) return respond({ blocked: [] });
        if (url.includes('/approve')) return respond({ id: 7, status: 'approved', decided_how: 'Invited to BK Insurance as member' });
        return respond({
          companies: [
            {
              id: 'org_bk',
              name: 'BK Insurance',
              created_at: '',
              active_members: 1,
              invitations: [],
              industry: 'agriculture',
              status: { code: 'active', text: 'Active: 1 person' },
            },
          ],
          industries: [{ key: 'agriculture', label: 'Agriculture' }],
        });
      }),
    );
    mount(<Companies />);
    await findText('People asking to join');
    await findText('Tried to sign up (no code was sent: sign-up is invite-only)');
    await findText('From: Youth Hub');
    expect(byButton('Approve')?.disabled).toBe(true); // a company must be chosen first

    const select = document.querySelector<HTMLSelectElement>('select[aria-label="Company to invite Alain into"]');
    if (!select) throw new Error('no company picker');
    choose(select, 'org_bk');
    const approve = await findButton('Approve');
    await vi.waitFor(() => expect(approve.disabled).toBe(false));
    click(approve);
    await vi.waitFor(() => expect(calls.some((c) => c.url.includes('/approve'))).toBe(true));
    const sent = calls.find((c) => c.url.includes('/approve'));
    expect(sent?.url).toBe('/api/admin/companies/access-requests/7/approve');
    expect(JSON.parse(String(sent?.init?.body))).toEqual({ organization_id: 'org_bk', role: 'member' });
  });
});

// Removing access (src/services/access_removal.py): staff take someone out of a company or block them from Ingabe.
describe('Companies: removing access', () => {
  const COMPANY = {
    id: 'org_bk',
    name: 'BK Insurance',
    created_at: '',
    active_members: 2,
    invitations: [],
    industry: 'agriculture',
    status: { code: 'active', text: 'Active: 2 people' },
  };
  const MEMBER = (id: string, email: string, blocked = false) => ({
    id,
    user_id: `user_${id}`,
    email,
    name: null,
    role: 'member',
    status: 'active',
    last_sign_in_at: '2026-10-30T08:00:00Z',
    blocked,
  });

  it('removes a member and blocks then unblocks someone, and never offers either on oneself', async () => {
    const calls: { url: string; method: string }[] = [];
    let blocked: { user_id: string; email: string; blocked_at: string; blocked_by: string; reason: null }[] = [];
    vi.stubGlobal('confirm', () => true);
    vi.stubGlobal(
      'fetch',
      vi.fn((url: string, init?: RequestInit) => {
        const method = init?.method ?? 'GET';
        calls.push({ url, method });
        if (url.endsWith('/access-requests')) return respond({ requests: [] });
        if (url.endsWith('/blocked')) return respond({ blocked });
        if (url.endsWith('/org_bk/people'))
          return respond({
            members: [MEMBER('om_me', 'staff@ingabe.rw'), MEMBER('om_2', 'jean@bk.rw', blocked.length > 0)],
            invitations: [],
          });
        if (url.endsWith('/members/om_2') && method === 'DELETE')
          return respond({ removed: 'om_2', user_id: 'user_om_2', email: 'jean@bk.rw', sessions_ended: 1 });
        if (url.endsWith('/people/user_om_2/block') && method === 'POST') {
          blocked = [
            { user_id: 'user_om_2', email: 'jean@bk.rw', blocked_at: '2026-11-01T08:00:00Z', blocked_by: 'staff@ingabe.rw', reason: null },
          ];
          return respond({ user_id: 'user_om_2', email: 'jean@bk.rw', blocked: true, sessions_ended: 1 });
        }
        if (url.endsWith('/people/user_om_2/block') && method === 'DELETE') {
          blocked = [];
          return respond({ user_id: 'user_om_2', blocked: false });
        }
        return respond({ companies: [COMPANY], industries: [{ key: 'agriculture', label: 'Agriculture' }] });
      }),
    );
    mount(<Companies />);
    click(await findButton('People (2)'));
    await findText(/^jean@bk\.rw/);
    await findText('You');
    // One person besides oneself: exactly one Remove and one Block button.
    expect(document.querySelectorAll('button').length).toBeGreaterThan(0);
    expect([...document.querySelectorAll('button')].filter((b) => b.textContent?.includes('Remove from company')).length).toBe(1);

    click(await findButton('Remove from company'));
    await vi.waitFor(() =>
      expect(calls.some((c) => c.url === '/api/admin/companies/org_bk/members/om_2' && c.method === 'DELETE')).toBe(true),
    );

    // One action at a time: Block is clickable again once the removal has finished.
    await vi.waitFor(() => expect(byButton('Block')?.disabled).toBe(false));
    click(byButton('Block') as HTMLButtonElement);
    await vi.waitFor(() => expect(calls.some((c) => c.url.endsWith('/people/user_om_2/block') && c.method === 'POST')).toBe(true));
    await findText('Blocked people');

    const unblock = await findButton('Unblock');
    click(unblock);
    await vi.waitFor(() => expect(calls.some((c) => c.url.endsWith('/people/user_om_2/block') && c.method === 'DELETE')).toBe(true));
  });
});
