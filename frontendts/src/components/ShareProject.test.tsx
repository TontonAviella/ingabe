import { apiFetch } from '@mundi/ee';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { toast } from 'sonner';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { type ProjectPeople, ShareProject } from '@/components/ShareProject';
import { byButton, byText, click, findButton, findText, mount, unmountAll } from '@/test/dom';

// Sharing a project from its map (Roger, 2026-10-22: Arnold was given Roger's projects by hand in the database).
vi.mock('@mundi/ee', () => ({ apiFetch: vi.fn() }));
vi.mock('sonner', () => ({ toast: Object.assign(vi.fn(), { error: vi.fn() }) }));
const fetchMock = vi.mocked(apiFetch);

const OWNER = { user_id: 'u-roger', email: 'roger@example.rw', role: null };
const ARNOLD = { user_id: 'u-arnold', email: 'arnold@example.rw', role: null };

function json(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } });
}

function choose(label: string, value: string) {
  const select = document.querySelector(`select[aria-label="${label}"]`) as HTMLSelectElement | null;
  if (!select) throw new Error(`no select ${label}`);
  const setter = Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype, 'value')?.set;
  setter?.call(select, value);
  select.dispatchEvent(new Event('change', { bubbles: true }));
}

/** The dialog's own Share button (the one that opened it is also labelled Share). */
function submit(): Element {
  const button = document.querySelector('form button[type="submit"]');
  if (!button) throw new Error('no submit button');
  return button;
}

async function openShare() {
  mount(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <ShareProject projectId="P1" />
    </QueryClientProvider>,
  );
  click(await findButton(/^Share$/));
}

afterEach(() => {
  unmountAll();
  fetchMock.mockReset();
  vi.mocked(toast).mockReset();
});

describe('sharing a project', () => {
  it('the owner shares with a colleague as editor', async () => {
    let people: ProjectPeople = { owner: OWNER, people: [], can_share: true, colleagues: [ARNOLD] };
    fetchMock.mockImplementation(async (url, init) => {
      if (init?.method === 'PUT') {
        people = { ...people, people: [{ ...ARNOLD, role: JSON.parse(String(init.body)).role }] };
      }
      expect(String(url)).toMatch(/^\/api\/projects\/P1\/people/);
      return json(people);
    });
    await openShare();
    await findText('roger@example.rw');
    choose('Colleague', 'arnold@example.rw');
    click(submit());

    await vi.waitFor(() => expect(toast).toHaveBeenCalled());
    const put = fetchMock.mock.calls.find(([, init]) => init?.method === 'PUT');
    expect(JSON.parse(String(put?.[1]?.body))).toEqual({ email: 'arnold@example.rw', role: 'editor' });
    expect(vi.mocked(toast).mock.calls[0][0]).toBe('Shared with arnold@example.rw.');
    await findText('It is shared with everyone in your organization.');
  });

  it('someone it is shared with sees who has it but cannot change it', async () => {
    fetchMock.mockResolvedValue(json({ owner: OWNER, people: [{ ...ARNOLD, role: 'editor' }], can_share: false, colleagues: [] }));
    await openShare();
    await findText('arnold@example.rw');
    expect(byText('Can edit')).not.toBeNull();
    expect(document.querySelector('select')).toBeNull();
    expect(byButton(/Stop sharing/)).toBeNull();
    expect(byText(/Only its owner can change this/)).not.toBeNull();
  });

  it('says to invite people first when nobody else is in the organization', async () => {
    fetchMock.mockResolvedValue(json({ owner: OWNER, people: [], can_share: true, colleagues: [] }));
    await openShare();
    await findText(/Invite people from People in the sidebar, then share/);
  });

  it('shows the server refusal', async () => {
    fetchMock.mockImplementation(async (_url, init) =>
      init?.method === 'PUT'
        ? json({ detail: 'arnold@example.rw is not in your organization yet. Invite them from People first, then share.' }, 404)
        : json({ owner: OWNER, people: [], can_share: true, colleagues: [ARNOLD] }),
    );
    await openShare();
    await findText('roger@example.rw');
    choose('Colleague', 'arnold@example.rw');
    click(submit());
    await vi.waitFor(() => expect(vi.mocked(toast.error)).toHaveBeenCalled());
    expect(vi.mocked(toast.error).mock.calls[0][0]).toContain('Invite them from People first');
  });
});
