import { useEffect } from 'react';
import { signInUrl, useWorkOSSession } from '@/components/auth/WorkOSSession';
import { IngabeIcon } from '@/components/Brand';
import { Button } from '@/components/ui/button';
import { pageLang, withLang } from '@/lib/pageLang';

// Ingabe's own step before WorkOS' sign-in page (2026-11-07). WorkOS' page asks only for an email and emails a code
// only to invited addresses, without saying so, so people who were never invited waited for a code that never came.
// The server sends a browser here until it has signed in once (/auth/login, src/routes/auth_routes.py). Nothing here
// depends on any address, so the page cannot tell anyone who is on Ingabe.

const TEXT = {
  en: {
    title: 'Sign in to Ingabe',
    rule: 'Ingabe is by invitation. We email a sign-in code only to addresses that have been invited.',
    signIn: 'I have an invitation: sign in',
    noInvite: 'No invitation yet?',
    request: 'Request access',
    requestNote: 'It takes a minute. Your invitation arrives by email once it is approved.',
    waiting: 'Waiting for a code that never arrives? Your address is not invited yet: request access instead.',
  },
  fr: {
    title: 'Se connecter à Ingabe',
    rule: 'Ingabe fonctionne sur invitation. Le code de connexion est envoyé par e-mail uniquement aux adresses invitées.',
    signIn: "J'ai une invitation : me connecter",
    noInvite: "Pas encore d'invitation ?",
    request: "Demander l'accès",
    requestNote: 'Cela prend une minute. Votre invitation arrive par e-mail une fois la demande acceptée.',
    waiting: "Vous attendez un code qui n'arrive jamais ? Votre adresse n'est pas encore invitée : demandez l'accès.",
  },
} as const;

export default function SignIn() {
  const { status } = useWorkOSSession();
  const params = new URLSearchParams(window.location.search);
  const returnTo = params.get('return_to') || '/';
  const lang = pageLang();
  const t = TEXT[lang];

  useEffect(() => {
    document.documentElement.lang = lang;
  }, [lang]);
  useEffect(() => {
    // Signed in already (another tab): nothing to explain, go where they were heading.
    if (status === 'signedIn') window.location.replace(returnTo.startsWith('/') && !returnTo.startsWith('//') ? returnTo : '/');
  }, [status, returnTo]);

  return (
    <div className="flex min-h-screen w-full items-center justify-center bg-canvas p-4">
      <div className="w-full max-w-sm rounded-xl border border-line bg-panel p-6 text-fg">
        <IngabeIcon className="mx-auto mb-4 h-10 w-10" />
        <h1 className="text-center font-medium">{t.title}</h1>
        <p className="mt-3 text-center text-sm text-fg-muted">{t.rule}</p>
        <Button asChild className="mt-5 w-full">
          <a href={signInUrl(returnTo, { confirmed: true })}>{t.signIn}</a>
        </Button>
        <div className="mt-6 border-t border-line pt-5 text-center">
          <p className="text-sm font-medium">{t.noInvite}</p>
          <Button asChild variant="outline" className="mt-3 w-full">
            <a href={withLang('/request-access', lang)}>{t.request}</a>
          </Button>
          <p className="mt-2 text-xs text-fg-muted">{t.requestNote}</p>
        </div>
        <p className="mt-5 text-center text-xs text-fg-subtle">{t.waiting}</p>
      </div>
    </div>
  );
}
