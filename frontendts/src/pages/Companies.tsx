import { Building2, Copy, ExternalLink, Loader2, Mail, Plus, RefreshCw } from 'lucide-react';
import { type FormEvent, useCallback, useEffect, useState } from 'react';
import { toast } from 'sonner';
import { useWorkOSSession } from '@/components/auth/WorkOSSession';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';

// Ingabe staff: every partner company, whether its admin has joined, and the
// one action each state needs. Companies manage their own people afterwards
// (Organization members page); deeper checks live in the WorkOS dashboard.

interface Invitation {
  id: string;
  email: string;
  state: string;
  role: string | null;
  expires_at: string;
}

interface Company {
  id: string;
  name: string;
  created_at: string;
  active_members: number;
  invitations: Invitation[];
  status: { code: 'active' | 'invited' | 'expired' | 'no_admin'; text: string };
}

const STATUS_STYLE: Record<Company['status']['code'], string> = {
  active: 'bg-emerald-900/50 text-emerald-300',
  invited: 'bg-sky-900/50 text-sky-300',
  expired: 'bg-amber-900/50 text-amber-300',
  no_admin: 'bg-gray-800 text-gray-300',
};

const WORKOS_DASHBOARD = 'https://dashboard.workos.com';

async function call<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`/api/admin/companies${path}`, {
    credentials: 'same-origin',
    headers: init?.body ? { 'Content-Type': 'application/json' } : undefined,
    ...init,
  });
  if (!res.ok) {
    const body = await res.json().catch(() => null);
    throw new Error(body?.detail ?? `Request failed (${res.status})`);
  }
  return res.json();
}

function InviteAdmin({ company, onDone }: { company: Company; onDone: () => void }) {
  const [email, setEmail] = useState('');
  const [busy, setBusy] = useState(false);
  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setBusy(true);
    try {
      const res = await call<{ resent?: boolean }>(`/${company.id}/admins`, {
        method: 'POST',
        body: JSON.stringify({ email: email.trim() }),
      });
      toast.success(res.resent ? `${email.trim()} was already invited: invitation sent again` : `Invitation sent to ${email.trim()}`);
      setEmail('');
      onDone();
    } catch (err) {
      toast.error(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  };
  return (
    <form onSubmit={submit} className="flex items-center gap-2">
      <Input
        type="email"
        required
        value={email}
        onChange={(e) => setEmail(e.target.value)}
        placeholder="another admin's email"
        className="h-8 min-w-48 flex-1"
        aria-label={`Invite another admin to ${company.name}`}
      />
      <Button type="submit" size="sm" variant="outline" disabled={busy || !email.trim()}>
        {busy ? <Loader2 className="h-4 w-4 animate-spin" /> : <Mail className="h-4 w-4" />}
        Invite admin
      </Button>
    </form>
  );
}

export default function Companies() {
  const { me } = useWorkOSSession();
  const [companies, setCompanies] = useState<Company[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [name, setName] = useState('');
  const [adminEmail, setAdminEmail] = useState('');
  const [adding, setAdding] = useState(false);
  const [resending, setResending] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      setCompanies((await call<{ companies: Company[] }>('')).companies);
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  }, []);

  useEffect(() => {
    load();
    // Invitations get accepted elsewhere: refresh when the person comes back to this tab.
    window.addEventListener('focus', load);
    return () => window.removeEventListener('focus', load);
  }, [load]);

  const add = async (e: FormEvent) => {
    e.preventDefault();
    setAdding(true);
    try {
      const res = await call<{ name: string; created: boolean; invitation: { resent?: boolean } }>('', {
        method: 'POST',
        body: JSON.stringify({ name: name.trim(), admin_email: adminEmail.trim() }),
      });
      const sent = res.invitation?.resent ? 'was already invited: invitation sent again' : 'invitation sent';
      toast.success(
        res.created ? `${res.name} added; ${adminEmail.trim()} ${sent}` : `${res.name} already exists; ${adminEmail.trim()} ${sent}`,
      );
      setName('');
      setAdminEmail('');
      await load();
    } catch (err) {
      toast.error(err instanceof Error ? err.message : String(err));
    } finally {
      setAdding(false);
    }
  };

  const resend = async (inv: Invitation) => {
    setResending(inv.id);
    try {
      await call(`/invitations/${inv.id}/resend`, { method: 'POST' });
      toast.success(`Invitation sent again to ${inv.email}`);
      await load();
    } catch (err) {
      toast.error(err instanceof Error ? err.message : String(err));
    } finally {
      setResending(null);
    }
  };

  if (me && !me.is_staff) {
    return (
      <div className="p-6 text-gray-300">
        <h1 className="text-xl font-semibold text-gray-100">Companies</h1>
        <p className="mt-2">Only Ingabe staff can manage companies.</p>
      </div>
    );
  }

  return (
    <div className="flex w-full max-w-5xl flex-col gap-6 p-6 text-gray-100">
      <div>
        <h1 className="text-xl font-semibold">Companies</h1>
        <p className="text-sm text-gray-400">
          Add a company and its first admin. WorkOS emails the invitation; after that the company adds its own people.
        </p>
      </div>

      <form onSubmit={add} className="flex flex-wrap items-center gap-2 rounded-md border border-gray-700 bg-gray-800/40 p-3">
        <Building2 className="h-4 w-4 text-gray-400" />
        <Input
          required
          value={name}
          onChange={(e) => setName(e.target.value)}
          placeholder="Company name, e.g. BK Insurance"
          className="min-w-56 flex-1"
          aria-label="Company name"
        />
        <Input
          type="email"
          required
          value={adminEmail}
          onChange={(e) => setAdminEmail(e.target.value)}
          placeholder="First admin's email"
          className="min-w-56 flex-1"
          aria-label="First admin's email"
        />
        <Button type="submit" disabled={adding || !name.trim() || !adminEmail.trim()}>
          {adding ? <Loader2 className="h-4 w-4 animate-spin" /> : <Plus className="h-4 w-4" />}
          Add company
        </Button>
      </form>

      {error && <p className="rounded-md border border-red-800 bg-red-950/40 px-3 py-2 text-sm text-red-300">{error}</p>}

      {!companies && !error ? (
        <Loader2 className="h-5 w-5 animate-spin text-gray-400" />
      ) : companies && companies.length === 0 ? (
        <p className="text-sm text-gray-400">No companies yet.</p>
      ) : (
        <ul className="flex flex-col gap-3">
          {companies?.map((c) => {
            const pending = c.invitations.filter((i) => i.state === 'pending' || i.state === 'expired');
            return (
              <li key={c.id} className="rounded-md border border-gray-700 p-4">
                <div className="flex flex-wrap items-center gap-3">
                  <span className="text-base font-medium">{c.name}</span>
                  <span className={`rounded px-2 py-0.5 text-xs ${STATUS_STYLE[c.status.code]}`}>{c.status.text}</span>
                  <span className="ml-auto flex items-center gap-1 text-xs text-gray-500">
                    {c.id}
                    <button
                      type="button"
                      title="Copy the WorkOS id (search it in the WorkOS dashboard)"
                      onClick={() => navigator.clipboard.writeText(c.id).then(() => toast.success('WorkOS id copied'))}
                      className="rounded p-1 hover:bg-gray-800"
                    >
                      <Copy className="h-3.5 w-3.5" />
                    </button>
                  </span>
                </div>
                {pending.length > 0 && (
                  <ul className="mt-3 flex flex-col gap-1 text-sm">
                    {pending.map((inv) => (
                      <li key={inv.id} className="flex flex-wrap items-center gap-2 text-gray-300">
                        <Mail className="h-4 w-4 text-gray-500" />
                        <span>{inv.email}</span>
                        <span className="text-xs text-gray-500">
                          {inv.state === 'expired' ? 'invitation expired' : `invited, expires ${inv.expires_at.slice(0, 10)}`}
                        </span>
                        <Button size="sm" variant="ghost" disabled={resending === inv.id} onClick={() => resend(inv)}>
                          {resending === inv.id ? <Loader2 className="h-4 w-4 animate-spin" /> : <RefreshCw className="h-4 w-4" />}
                          Send again
                        </Button>
                      </li>
                    ))}
                  </ul>
                )}
                <div className="mt-3">
                  <InviteAdmin company={c} onDone={load} />
                </div>
              </li>
            );
          })}
        </ul>
      )}

      {/* System owner only (first email in PLATFORM_ADMIN_EMAILS). */}
      {me?.is_owner && (
        <section className="rounded-md border border-gray-800 bg-gray-900/50 p-4 text-sm text-gray-400">
          <p className="font-medium text-gray-200">When to open the WorkOS dashboard instead</p>
          <ul className="mt-2 list-disc space-y-1 pl-5">
            <li>Someone cannot sign in: its sign-in logs say why (wrong email, expired code, blocked attempt).</li>
            <li>Changing the sign-in page: logo, colours, and which ways to sign in (Google, email code, password).</li>
            <li>Rotating the WorkOS API key.</li>
            <li>A company asks to sign in with its own corporate login (paid, per company).</li>
          </ul>
          <a
            href={WORKOS_DASHBOARD}
            target="_blank"
            rel="noreferrer"
            className="mt-3 inline-flex items-center gap-1 text-emerald-400 hover:underline"
          >
            Open the WorkOS dashboard <ExternalLink className="h-3.5 w-3.5" />
          </a>
        </section>
      )}
    </div>
  );
}
