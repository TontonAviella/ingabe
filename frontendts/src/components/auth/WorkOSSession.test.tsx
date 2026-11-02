import { afterEach, describe, expect, it, vi } from 'vitest';
import { WorkOSRequireAuth, WorkOSSessionProvider } from '@/components/auth/WorkOSSession';
import { byText, findText, mount, unmountAll, unnamedControls } from '@/test/dom';

// After a sign-out or a failed sign-in the app stays put (bouncing back to WorkOS would loop or look like nothing
// happened) and offers one way back in.

function show(search: string) {
  window.history.replaceState(null, '', `/${search}`);
  vi.stubGlobal('fetch', () => Promise.resolve(new Response('{}', { status: 401 })));
  mount(
    <WorkOSSessionProvider>
      <WorkOSRequireAuth>
        <p>Your maps</p>
      </WorkOSRequireAuth>
    </WorkOSSessionProvider>,
  );
}

afterEach(() => {
  unmountAll();
  vi.unstubAllGlobals();
  window.history.replaceState(null, '', '/');
});

describe('WorkOSRequireAuth', () => {
  it('after a sign-out, offers to sign in again and nothing else', async () => {
    show('?signed_out=1');
    await findText("You're signed out", 'h1');
    const action = document.querySelector<HTMLAnchorElement>('a[href^="/auth/login"]');
    expect(action?.textContent).toBe('Sign in');
    expect(unnamedControls()).toEqual([]);
    expect(byText('Your maps')).toBeNull();
  });

  it('after a failed sign-in, offers to try again without blaming the person', async () => {
    show('?sign_in_error=1');
    await findText("Sign-in didn't finish", 'h1');
    expect(document.querySelector('a[href^="/auth/login"]')?.textContent).toBe('Try again');
    expect(document.body.textContent).not.toMatch(/server log/);
    expect(unnamedControls()).toEqual([]);
  });
});
