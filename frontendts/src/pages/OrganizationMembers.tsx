import { Building2, Loader2, Mail, Trash2, UserPlus, X } from 'lucide-react';
import { type FormEvent, useCallback, useEffect, useState } from 'react';
import { toast } from 'sonner';
import { roleLabel, useWorkOSSession } from '@/components/auth/WorkOSSession';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';

// Members of the active WorkOS organization. Admins invite people by email,
// change roles and remove members here; members only see the list. WorkOS
// sends the invitation emails, so nobody needs the WorkOS dashboard.

interface Member {
  id: string;
  user_id: string;
  email: string | null;
  name: string | null;
  role: string | null;
  status: string;
}

interface Invitation {
  id: string;
  email: string;
  role: string | null;
  expires_at: string | null;
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
      (result) =>
        (result as { resent?: boolean }).resent ? `${address} was already invited: invitation sent again` : `Invitation sent to ${address}`,
    ).then(() => setEmail(''));
  };

  if (!org) {
    return (
      <div className="flex w-full flex-col gap-2 p-6 text-gray-300">
        <h1 className="text-xl font-semibold text-gray-100">Organization members</h1>
        <p>Choose an organization in the sidebar to see its members.</p>
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
            {manage ? 'Invite people, set their role, or remove them.' : 'Only admins can change members.'}
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
          <h2 className="mb-2 text-sm font-medium uppercase tracking-wide text-gray-400">Pending invitations</h2>
          <ul className="divide-y divide-gray-800 rounded-md border border-gray-700">
            {data.invitations.map((inv) => (
              <li key={inv.id} className="flex items-center gap-3 px-3 py-2">
                <span className="min-w-0 flex-1 truncate">{inv.email}</span>
                <span className="text-sm text-gray-300">{roleLabel(inv.role)}</span>
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
              </li>
            ))}
          </ul>
        </section>
      )}
    </div>
  );
}
