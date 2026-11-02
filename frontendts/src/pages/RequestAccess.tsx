import { Loader2 } from 'lucide-react';
import { type FormEvent, useState } from 'react';
import { Link } from 'react-router-dom';
import { signInUrl, useWorkOSSession } from '@/components/auth/WorkOSSession';
import { IngabeIcon } from '@/components/Brand';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Textarea } from '@/components/ui/textarea';

// Ingabe is invite-only: someone without an invitation asks here, Ingabe staff get a phone push and approve
// (WorkOS emails an invitation) or decline on the Companies page (src/services/access_requests.py). The website's
// and the app's sign-up links lead here, so nobody waits for a code WorkOS will never send.

const CARD = 'w-full max-w-sm rounded-xl border border-line bg-panel p-6 text-fg';

// The server's own check (looks_like_email, src/services/project_people.py): something@something.something, no spaces.
const EMAIL = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;

function Shell({ children }: React.PropsWithChildren) {
  return <div className="flex min-h-screen w-full items-center justify-center bg-canvas p-4">{children}</div>;
}

export default function RequestAccess() {
  const { status } = useWorkOSSession();
  const [name, setName] = useState('');
  const [email, setEmail] = useState('');
  const [emailTouched, setEmailTouched] = useState(false);
  const [organization, setOrganization] = useState('');
  const [reason, setReason] = useState('');
  const [website, setWebsite] = useState(''); // hidden: only a bot fills it in
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [sentTo, setSentTo] = useState<string | null>(null);

  if (status === 'signedIn') {
    return (
      <Shell>
        <div className={`${CARD} text-center`}>
          <IngabeIcon className="mx-auto mb-4 h-10 w-10" />
          <p className="font-medium">You're already signed in</p>
          <Button asChild className="mt-4">
            <Link to="/">Open your maps</Link>
          </Button>
        </div>
      </Shell>
    );
  }

  if (sentTo) {
    return (
      <Shell>
        <div className={`${CARD} text-center`}>
          <IngabeIcon className="mx-auto mb-4 h-10 w-10" />
          <p className="font-medium">Request sent</p>
          <p className="mt-2 text-sm text-fg-muted">Your invitation will arrive at {sentTo}.</p>
        </div>
      </Shell>
    );
  }

  const emailOk = EMAIL.test(email.trim());
  // Say what is wrong with the email as soon as someone leaves the field, not after they press the button.
  const emailWrong = emailTouched && email.trim() !== '' && !emailOk;

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const res = await fetch('/api/access-requests', {
        method: 'POST',
        credentials: 'same-origin',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          email: email.trim(),
          name: name.trim() || null,
          organization: organization.trim() || null,
          reason: reason.trim() || null,
          website: website || null,
        }),
      });
      if (!res.ok) {
        const body = await res.json().catch(() => null);
        throw new Error(typeof body?.detail === 'string' ? body.detail : "Your request didn't go through. Try again.");
      }
      setSentTo(email.trim());
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <Shell>
      <form onSubmit={submit} className={CARD} noValidate>
        <IngabeIcon className="mx-auto mb-4 h-10 w-10" />
        <h1 className="text-center font-medium">Request access</h1>
        <div className="mt-5 flex flex-col gap-3">
          <Input
            required
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder="Your name"
            aria-label="Your name"
            autoComplete="name"
            maxLength={120}
          />
          <div>
            <Input
              required
              type="email"
              inputMode="email"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              onBlur={() => setEmailTouched(true)}
              placeholder="you@company.rw"
              aria-label="Your email"
              aria-invalid={emailWrong || undefined}
              aria-describedby={emailWrong ? 'email-problem' : undefined}
              autoComplete="email"
              maxLength={254}
            />
            {emailWrong && (
              <p id="email-problem" className="mt-1 text-xs text-bad">
                Use an email like you@company.rw
              </p>
            )}
          </div>
          <Input
            value={organization}
            onChange={(e) => setOrganization(e.target.value)}
            placeholder="Organization (optional)"
            aria-label="Your organization"
            autoComplete="organization"
            maxLength={160}
          />
          <Textarea
            value={reason}
            onChange={(e) => setReason(e.target.value)}
            placeholder="What for? (optional)"
            aria-label="What you would like to use Ingabe for"
            maxLength={1000}
            rows={3}
          />
          <input
            type="text"
            name="website"
            value={website}
            onChange={(e) => setWebsite(e.target.value)}
            tabIndex={-1}
            autoComplete="off"
            aria-hidden="true"
            className="hidden"
          />
        </div>
        {error && (
          <p role="alert" className="mt-3 rounded-md border border-bad/40 bg-bad/10 px-3 py-2 text-sm text-bad">
            {error}
          </p>
        )}
        <Button type="submit" disabled={busy || !name.trim() || !emailOk} className="mt-4 w-full">
          {busy && <Loader2 className="h-4 w-4 animate-spin" />}
          Request access
        </Button>
        <a href={signInUrl('/')} className="mt-4 block text-center text-sm text-fg-muted hover:text-fg">
          Sign in
        </a>
      </form>
    </Shell>
  );
}
