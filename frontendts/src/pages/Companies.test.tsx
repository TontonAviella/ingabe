import { afterEach, describe, expect, it, vi } from 'vitest';
import Companies from '@/pages/Companies';
import { byButton, click, findButton, findText, mount, unmountAll } from '@/test/dom';

// People asking to join (src/services/access_requests.py): staff see each open request on the Companies page and
// approve it with an invitation into a company they pick, or decline it.
vi.mock('@/components/auth/WorkOSSession', () => ({ useWorkOSSession: () => ({ me: { is_staff: true, is_owner: false } }) }));
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
