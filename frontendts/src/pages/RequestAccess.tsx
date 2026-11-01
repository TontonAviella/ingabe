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

const CARD = 'w-full max-w-sm rounded-lg border border-gray-700 bg-gray-900 p-6 text-gray-100';

function Shell({ children }: React.PropsWithChildren) {
  return <div className="flex min-h-screen w-full items-center justify-center bg-background p-4">{children}</div>;
}

export default function RequestAccess() {
  const { status } = useWorkOSSession();
  const [name, setName] = useState('');
  const [email, setEmail] = useState('');
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
          <p className="font-medium">You are already signed in</p>
          <Link
            to="/"
            className="mt-4 inline-block rounded-md bg-emerald-600 px-4 py-2 text-sm font-medium text-white hover:bg-emerald-500"
          >
            Go to your maps
          </Link>
        </div>
      </Shell>
    );
  }

  if (sentTo) {
    return (
      <Shell>
        <div className={`${CARD} text-center`}>
          <IngabeIcon className="mx-auto mb-4 h-10 w-10" />
          <p className="font-medium">Thank you, we have your request</p>
          <p className="mt-2 text-sm text-gray-400">
            Once we say yes, an invitation arrives at {sentTo}. Open it to sign in. There is no code to wait for until then.
          </p>
        </div>
      </Shell>
    );
  }

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
        throw new Error(typeof body?.detail === 'string' ? body.detail : `Something went wrong (${res.status}). Please try again.`);
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
      <form onSubmit={submit} className={CARD}>
        <IngabeIcon className="mx-auto mb-4 h-10 w-10" />
        <p className="text-center font-medium">Request access to Ingabe</p>
        <p className="mt-2 text-center text-sm text-gray-400">
          Ingabe is invite-only for now. Tell us who you are, and once we say yes, an invitation arrives in your email.
        </p>
        <div className="mt-5 flex flex-col gap-3">
          <Input
            required
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder="Your name"
            aria-label="Your name"
            maxLength={120}
          />
          <Input
            required
            type="email"
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            placeholder="Your email"
            aria-label="Your email"
            maxLength={254}
          />
          <Input
            value={organization}
            onChange={(e) => setOrganization(e.target.value)}
            placeholder="Your organization (optional)"
            aria-label="Your organization"
            maxLength={160}
          />
          <Textarea
            value={reason}
            onChange={(e) => setReason(e.target.value)}
            placeholder="What would you like to use Ingabe for? (optional)"
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
        {error && <p className="mt-3 rounded-md border border-red-800 bg-red-950/40 px-3 py-2 text-sm text-red-300">{error}</p>}
        <Button
          type="submit"
          disabled={busy || !name.trim() || !email.trim()}
          className="mt-4 w-full bg-emerald-600 text-white hover:bg-emerald-500"
        >
          {busy && <Loader2 className="h-4 w-4 animate-spin" />}
          Request access
        </Button>
        <p className="mt-4 text-center text-sm text-gray-400">
          Already invited?{' '}
          <a href={signInUrl('/')} className="text-emerald-400 hover:underline">
            Sign in
          </a>
        </p>
      </form>
    </Shell>
  );
}
