import { afterEach, describe, expect, it, vi } from 'vitest';
import SignIn, { sameSitePath } from '@/pages/SignIn';
import { byText, findText, mount, unmountAll, unnamedControls } from '@/test/dom';

// Ingabe's step before WorkOS' page: codes go only to invited addresses, so people without an invitation are told
// to request access instead of waiting (src/routes/auth_routes.py).
const session = vi.hoisted(() => ({ status: 'signedOut' as 'signedOut' | 'signedIn' }));
vi.mock('@/components/auth/WorkOSSession', () => ({
  useWorkOSSession: () => ({ status: session.status, me: null }),
  signInUrl: (to: string, opts: { confirmed?: boolean } = {}) =>
    `/auth/login?return_to=${encodeURIComponent(to)}${opts.confirmed ? '&confirmed=1' : ''}`,
}));

function link(text: string): HTMLAnchorElement {
  const a = [...document.querySelectorAll('a')].find((x) => x.textContent === text);
  if (!a) throw new Error(`No link ${text}`);
  return a;
}

afterEach(() => {
  session.status = 'signedOut';
  window.history.replaceState(null, '', '/');
  unmountAll();
});

describe('SignIn', () => {
  it('says codes go only to invited addresses and offers sign-in or Request access', async () => {
    window.history.replaceState(null, '', '/sign-in?return_to=%2Fproject%2Fabc&lang=en');
    mount(<SignIn />);
    await findText('Sign in to Ingabe', 'h1');
    expect(byText('Ingabe is by invitation. We email a sign-in code only to addresses that have been invited.')).not.toBeNull();
    expect(link('I have an invitation: sign in').getAttribute('href')).toBe('/auth/login?return_to=%2Fproject%2Fabc&confirmed=1');
    expect(link('Request access').getAttribute('href')).toBe('/request-access');
    expect(unnamedControls()).toEqual([]);
  });

  it('speaks French to readers from the French website, and keeps them in French', async () => {
    window.history.replaceState(null, '', '/sign-in?lang=fr');
    mount(<SignIn />);
    await findText('Se connecter à Ingabe', 'h1');
    expect(link("Demander l'accès").getAttribute('href')).toBe('/request-access?lang=fr');
    // Set by an effect just after the first render: wait for it, not for the heading.
    await vi.waitFor(() => expect(document.documentElement.lang).toBe('fr'));
  });

  it('goes back only to a path on this site', () => {
    const here = 'https://gis.nozalabs.rw';
    expect(sameSitePath('/project/abc?tab=2#map', here)).toBe('/project/abc?tab=2#map');
    for (const outside of [
      '/\\evil.example',
      '//evil.example',
      '/\\/evil.example',
      '/.//evil.example',
      '/%2e//evil.example',
      'https://evil.example/x',
      'evil',
      null,
    ]) {
      expect(sameSitePath(outside, here)).toBe('/');
    }
  });

  it('never puts an outside address in the sign-in link', async () => {
    window.history.replaceState(null, '', '/sign-in?return_to=%2F%5Cevil.example');
    mount(<SignIn />);
    await findText('Sign in to Ingabe', 'h1');
    expect(link('I have an invitation: sign in').getAttribute('href')).toBe('/auth/login?return_to=%2F&confirmed=1');
  });

  it('never sends a signed-in person back to this page or to a sign-in or sign-out route', () => {
    const here = 'https://gis.nozalabs.rw';
    for (const loop of [
      '/sign-in',
      '/sign-in?return_to=%2Fsign-in',
      '/auth/logout',
      '/auth/login?confirmed=1',
      '/%61uth/logout',
      '/./auth/logout',
    ]) {
      expect(sameSitePath(loop, here)).toBe('/');
    }
    expect(sameSitePath('/sign-ins-report', here)).toBe('/sign-ins-report');
  });

  it('sends a person who is already signed in where they were heading', async () => {
    session.status = 'signedIn';
    const replace = vi.fn();
    const real = window.location;
    vi.stubGlobal('location', { ...real, origin: real.origin, search: '?return_to=%2Fproject%2Fabc', replace });
    mount(<SignIn />);
    await vi.waitFor(() => expect(replace).toHaveBeenCalledWith('/project/abc'));
    vi.unstubAllGlobals();
  });
});
