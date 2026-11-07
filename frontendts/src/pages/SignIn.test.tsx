import { afterEach, describe, expect, it, vi } from 'vitest';
import SignIn from '@/pages/SignIn';
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
    expect(document.documentElement.lang).toBe('fr');
  });
});
