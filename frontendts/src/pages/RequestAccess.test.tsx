import { MemoryRouter } from 'react-router-dom';
import { afterEach, describe, expect, it, vi } from 'vitest';
import RequestAccess from '@/pages/RequestAccess';
import { byButton, byText, click, findText, mount, unmountAll, unnamedControls } from '@/test/dom';

// Someone without an invitation asks for one (src/services/access_requests.py). The page says what an email must
// look like before the button is pressed, and every field keeps a name a screen reader can say.
const session = vi.hoisted(() => ({ status: 'signedOut' as 'signedOut' | 'signedIn' }));
vi.mock('@/components/auth/WorkOSSession', () => ({
  useWorkOSSession: () => ({ status: session.status, me: null }),
  signInUrl: (to: string) => `/auth/login?return_to=${encodeURIComponent(to)}`,
}));

function typeInto(field: HTMLInputElement | HTMLTextAreaElement, text: string) {
  const proto = field instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
  Object.getOwnPropertyDescriptor(proto, 'value')?.set?.call(field, text);
  field.dispatchEvent(new Event('input', { bubbles: true }));
}

function field(label: string): HTMLInputElement {
  const el = document.querySelector<HTMLInputElement>(`[aria-label="${label}"]`);
  if (!el) throw new Error(`No field ${label}`);
  return el;
}

function show() {
  mount(
    <MemoryRouter>
      <RequestAccess />
    </MemoryRouter>,
  );
  return findText('Request access', 'h1');
}

afterEach(() => {
  session.status = 'signedOut';
  unmountAll();
  vi.unstubAllGlobals();
});

describe('RequestAccess', () => {
  it('gives every field, button and link a name', async () => {
    await show();
    expect(unnamedControls()).toEqual([]);
  });

  it('says what a wrong email should look like when the field is left, and keeps the button off', async () => {
    await show();
    typeInto(field('Your name'), 'Alain');
    const email = field('Your email');
    typeInto(email, 'alain.example.rw');
    expect(byText('Use an email like you@company.rw')).toBeNull(); // not while still typing
    email.dispatchEvent(new FocusEvent('focusout', { bubbles: true }));
    await findText('Use an email like you@company.rw');
    expect(email.getAttribute('aria-invalid')).toBe('true');
    expect(email.getAttribute('aria-describedby')).toBe('email-problem');
    expect(byButton('Request access')?.disabled).toBe(true);

    typeInto(email, 'alain@example.rw');
    await vi.waitFor(() => expect(byText('Use an email like you@company.rw')).toBeNull());
    expect(byButton('Request access')?.disabled).toBe(false);
  });

  it('sends the request and says where the invitation will arrive', async () => {
    const fetchMock = vi.fn(() => Promise.resolve(new Response('{}', { status: 200 })));
    vi.stubGlobal('fetch', fetchMock);
    await show();
    typeInto(field('Your name'), 'Alain');
    typeInto(field('Your email'), ' alain@example.rw ');
    await vi.waitFor(() => expect(byButton('Request access')?.disabled).toBe(false));
    click(byButton('Request access') as HTMLButtonElement);
    await findText('Your invitation will arrive at alain@example.rw.');
    const body = JSON.parse((fetchMock.mock.calls[0] as unknown as [string, RequestInit])[1].body as string);
    expect(body).toMatchObject({ email: 'alain@example.rw', name: 'Alain', organization: null, reason: null });
  });

  it('sends someone already signed in to their maps instead of asking again', async () => {
    session.status = 'signedIn';
    mount(
      <MemoryRouter>
        <RequestAccess />
      </MemoryRouter>,
    );
    await findText("You're already signed in");
    expect(document.querySelector('a[href="/"]')?.textContent).toBe('Open your maps');
    expect(document.querySelector('form')).toBeNull();
    expect(unnamedControls()).toEqual([]);
  });

  it("shows the server's reason when a request is refused", async () => {
    vi.stubGlobal('fetch', () => Promise.resolve(new Response(JSON.stringify({ detail: 'Too many requests.' }), { status: 429 })));
    await show();
    typeInto(field('Your name'), 'Alain');
    typeInto(field('Your email'), 'alain@example.rw');
    await vi.waitFor(() => expect(byButton('Request access')?.disabled).toBe(false));
    click(byButton('Request access') as HTMLButtonElement);
    await findText('Too many requests.', '[role="alert"]');
  });
});
