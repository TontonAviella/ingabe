import { Building2, Copy, Loader2, Mail, RotateCw, Trash2, UserPlus, X } from 'lucide-react';
import { type FormEvent, useCallback, useEffect, useState } from 'react';
import { toast } from 'sonner';
import { roleLabel, useWorkOSSession } from '@/components/auth/WorkOSSession';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';

// The people of the active WorkOS organization. Admins invite people by email,
// see whether each invitation was taken up, send it again or as a ready message
// (WhatsApp, when the email does not reach the person), change roles and remove
// members; members only see the list. Nobody needs the WorkOS dashboard.

interface Member {
  id: string;
  user_id: string;
  email: string | null;
  name: string | null;
  role: string | null;
  status: string;
  last_sign_in_at: string | null;
}

interface Invitation {
  id: string;
  email: string;
  role: string | null;
  state: 'pending' | 'expired';
  created_at: string | null;
  expires_at: string | null;
  /** The invitation's own sign-in link, for sending another way. */
  link: string | null;
}

/** "today", "yesterday", "3 days ago", or the date. */
export function ago(iso: string | null, now: Date = new Date()): string | null {
  if (!iso) return null;
  const when = new Date(iso);
  if (Number.isNaN(when.getTime())) return null;
  const days = Math.floor((now.getTime() - when.getTime()) / 86_400_000);
  if (days <= 0) return 'today';
  if (days === 1) return 'yesterday';
  if (days < 14) return `${days} days ago`;
  return `on ${when.toLocaleDateString(undefined, { day: 'numeric', month: 'short', year: 'numeric' })}`;
}

/** The message an admin sends the invited person (by WhatsApp or SMS): where to go and which address to use. */
export function inviteMessage(email: string, organization: string, link: string | null, site: string): string {
  const open = link ? `Open your invitation: ${link}` : `Open ${site}`;
  return (
    `Hello, you are invited to ${organization} on noza·labs. ${open} and choose "Continue with Google" with ${email} ` +
    `(or type ${email} and follow the steps).`
  );
}

async function copy(text: string, what: string) {
  try {
    await navigator.clipboard.writeText(text);
    toast.success(`${what} copied`);
  } catch {
    toast.error('Could not copy here: select the text and copy it');
  }
}

interface MembersResponse {
  can_manage: boolean;
  me: string;
  roles: string[];
  members: Member[];
  invitations: Invitation[];
}

async function call<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`/api/auth/organization${path}`, {
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

function RoleSelect({
  value,
  roles,
  disabled,
  onChange,
}: {
  value: string;
  roles: string[];
  disabled?: boolean;
  onChange: (role: string) => void;
}) {
  return (
    <select
      value={value}
      disabled={disabled}
      onChange={(e) => onChange(e.target.value)}
      className="rounded-md border border-gray-700 bg-gray-800 px-2 py-1 text-sm text-gray-100 disabled:opacity-60"
    >
      {roles.map((r) => (
        <option key={r} value={r}>
          {roleLabel(r)}
        </option>
      ))}
    </select>
  );
}

export default function OrganizationMembers() {
  const { me } = useWorkOSSession();
  const org = me?.organization ?? null;
  const [data, setData] = useState<MembersResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [email, setEmail] = useState('');
  const [inviteRole, setInviteRole] = useState('member');
  const [justInvited, setJustInvited] = useState<{ email: string; link: string | null } | null>(null);

  const load = useCallback(async () => {
    if (!org) return;
    try {
      setData(await call<MembersResponse>('/members'));
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  }, [org]);

  useEffect(() => {
    load();
    // Invitations get accepted elsewhere: refresh when the person comes back to this tab.
    window.addEventListener('focus', load);
    return () => window.removeEventListener('focus', load);
  }, [load]);

  const run = async (key: string, action: () => Promise<unknown>, done: string | ((result: unknown) => string)) => {
    setBusy(key);
    try {
      const result = await action();
      toast.success(typeof done === 'function' ? done(result) : done);
      await load();
    } catch (err) {
      toast.error(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(null);
    }
  };

  const invite = (e: FormEvent) => {
    e.preventDefault();
    const address = email.trim();
    if (!address) return;
    run(
      'invite',
      () => call('/invitations', { method: 'POST', body: JSON.stringify({ email: address, role: inviteRole }) }),
      (result) => {
        const sent = result as { resent?: boolean; link?: string | null };
        setJustInvited({ email: address, link: sent.link ?? null });
        return sent.resent ? `${address} was already invited: invitation sent again` : `Invitation sent to ${address}`;
      },
    ).then(() => setEmail(''));
  };

  if (!org) {
    return (
      <div className="flex w-full flex-col gap-2 p-6 text-gray-300">
        <h1 className="text-xl font-semibold text-gray-100">People</h1>
        <p>Choose an organization in the sidebar to see its people.</p>
      </div>
    );
  }

  const manage = data?.can_manage ?? false;
  const roles = data?.roles ?? ['admin', 'member'];

  return (
    <div className="flex w-full max-w-4xl flex-col gap-6 p-6 text-gray-100">
      <div className="flex items-center gap-3">
        <span className="inline-flex h-9 w-9 items-center justify-center rounded-md bg-indigo-600 text-white">
          <Building2 className="h-5 w-5" />
        </span>
        <div>
          <h1 className="text-xl font-semibold">{org.name}</h1>
          <p className="text-sm text-gray-400">
            {manage
              ? 'Invite people by email. They sign in with Google or Microsoft, or with that email address.'
              : 'Only admins can change who is here.'}
          </p>
        </div>
      </div>

      {error && <p className="rounded-md border border-red-800 bg-red-950/40 px-3 py-2 text-sm text-red-300">{error}</p>}

      {manage && (
        <form onSubmit={invite} className="flex flex-wrap items-center gap-2 rounded-md border border-gray-700 bg-gray-800/40 p-3">
          <Mail className="h-4 w-4 text-gray-400" />
          <Input
            type="email"
            required
            placeholder="name@example.rw"
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            className="min-w-56 flex-1"
            aria-label="Email address to invite"
          />
          <RoleSelect value={inviteRole} roles={roles} onChange={setInviteRole} />
          <Button type="submit" disabled={busy === 'invite' || !email.trim()}>
            {busy === 'invite' ? <Loader2 className="h-4 w-4 animate-spin" /> : <UserPlus className="h-4 w-4" />}
            Invite
          </Button>
        </form>
      )}

      {manage && justInvited && (
        <section className="flex flex-col gap-2 rounded-md border border-emerald-800/60 bg-emerald-950/30 p-3 text-sm">
          <p className="m-0 text-emerald-200">
            WorkOS emailed the invitation to <b>{justInvited.email}</b>. If it does not arrive (spam folder, typing slip), send this message
            yourself:
          </p>
          <p className="m-0 select-all rounded bg-gray-900/70 px-2 py-1.5 text-gray-200">
            {inviteMessage(justInvited.email, org.name, justInvited.link, window.location.origin)}
          </p>
          <div className="flex gap-2">
            <Button
              size="sm"
              variant="secondary"
              onClick={() => copy(inviteMessage(justInvited.email, org.name, justInvited.link, window.location.origin), 'Message')}
            >
              <Copy className="h-4 w-4" /> Copy message
            </Button>
            <Button size="sm" variant="ghost" onClick={() => setJustInvited(null)}>
              Close
            </Button>
          </div>
        </section>
      )}

      <section>
        <h2 className="mb-2 text-sm font-medium uppercase tracking-wide text-gray-400">Members{data ? ` (${data.members.length})` : ''}</h2>
        {!data && !error ? (
          <Loader2 className="h-5 w-5 animate-spin text-gray-400" />
        ) : (
          <ul className="divide-y divide-gray-800 rounded-md border border-gray-700">
            {data?.members.map((m) => {
              const isMe = m.user_id === data.me;
              return (
                <li key={m.id} className="flex items-center gap-3 px-3 py-2">
                  <span className="min-w-0 flex-1">
                    <span className="block truncate">
                      {m.name ?? m.email ?? m.user_id}
                      {isMe && <span className="ml-2 text-xs text-gray-400">(you)</span>}
                    </span>
                    {m.name && m.email && <span className="block truncate text-xs text-gray-400">{m.email}</span>}
                    <span className="block text-xs text-gray-500">
                      {m.last_sign_in_at ? `Last signed in ${ago(m.last_sign_in_at)}` : 'Not signed in yet'}
                    </span>
                  </span>
                  {m.status !== 'active' && <span className="text-xs text-amber-400">{m.status}</span>}
                  {manage && !isMe ? (
                    <>
                      <RoleSelect
                        value={m.role ?? 'member'}
                        roles={roles}
                        disabled={busy === m.id}
                        onChange={(role) =>
                          run(
                            m.id,
                            () => call(`/members/${m.id}`, { method: 'PATCH', body: JSON.stringify({ role }) }),
                            `${m.email ?? 'Member'} is now ${roleLabel(role)}`,
                          )
                        }
                      />
                      <Button
                        variant="ghost"
                        size="icon"
                        title="Remove from organization"
                        disabled={busy === m.id}
                        onClick={() => {
                          if (!window.confirm(`Remove ${m.email ?? 'this member'} from ${org.name}?`)) return;
                          run(m.id, () => call(`/members/${m.id}`, { method: 'DELETE' }), `${m.email ?? 'Member'} removed`);
                        }}
                      >
                        <Trash2 className="h-4 w-4 text-red-400" />
                      </Button>
                    </>
                  ) : (
                    <span className="text-sm text-gray-300">{roleLabel(m.role)}</span>
                  )}
                </li>
              );
            })}
          </ul>
        )}
      </section>

      {manage && data && data.invitations.length > 0 && (
        <section>
          <h2 className="mb-2 text-sm font-medium uppercase tracking-wide text-gray-400">
            Invited, not joined yet ({data.invitations.length})
          </h2>
          <ul className="divide-y divide-gray-800 rounded-md border border-gray-700">
            {data.invitations.map((inv) => (
              <li key={inv.id} className="flex flex-wrap items-center gap-x-3 gap-y-1 px-3 py-2">
                <span className="min-w-0 flex-1">
                  <span className="block truncate">{inv.email}</span>
                  <span className={`block text-xs ${inv.state === 'expired' ? 'text-amber-400' : 'text-gray-500'}`}>
                    {inv.state === 'expired'
                      ? `Invitation expired ${ago(inv.expires_at) ?? ''}: send it again`
                      : `Invited ${ago(inv.created_at) ?? ''}, not joined yet`}
                  </span>
                </span>
                <span className="text-sm text-gray-300">{roleLabel(inv.role)}</span>
                <Button
                  variant="ghost"
                  size="sm"
                  title="Copy a message with the invitation link, to send by WhatsApp or SMS"
                  onClick={() => copy(inviteMessage(inv.email, org.name, inv.link, window.location.origin), 'Message')}
                >
                  <Copy className="h-4 w-4" /> Message
                </Button>
                <Button
                  variant="ghost"
                  size="sm"
                  title="Email the invitation again"
                  disabled={busy === inv.id}
                  onClick={() =>
                    run(inv.id, () => call(`/invitations/${inv.id}/resend`, { method: 'POST' }), `Invitation sent again to ${inv.email}`)
                  }
                >
                  <RotateCw className="h-4 w-4" /> Send again
                </Button>
                {inv.state === 'pending' && (
                  <Button
                    variant="ghost"
                    size="icon"
                    title="Cancel invitation"
                    disabled={busy === inv.id}
                    onClick={() =>
                      run(inv.id, () => call(`/invitations/${inv.id}`, { method: 'DELETE' }), `Invitation to ${inv.email} cancelled`)
                    }
                  >
                    <X className="h-4 w-4" />
                  </Button>
                )}
              </li>
            ))}
          </ul>
        </section>
      )}
    </div>
  );
}
