import {
  Ban,
  Building2,
  Check,
  ChevronDown,
  ChevronRight,
  Copy,
  ExternalLink,
  Loader2,
  Mail,
  Plus,
  RefreshCw,
  UserMinus,
  UserPlus,
  Users,
  X,
} from 'lucide-react';
import { type FormEvent, useCallback, useEffect, useState } from 'react';
import { toast } from 'sonner';
import { useWorkOSSession } from '@/components/auth/WorkOSSession';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';

// Ingabe staff: every partner company, whether its admin has joined, and the
// one action each state needs. Companies manage their own people afterwards
// (Organization members page); deeper checks live in the WorkOS dashboard.
// Each partner company works in one industry, chosen here: its people see only
// that one. Ingabe's own company sees all of them (src/services/industry.py).

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
  // 'all': Ingabe's own company; null: not given one yet (its first person's choice at sign-in sets it).
  industry: string | null;
}

// Someone without an invitation who asked to join (src/services/access_requests.py).
interface AccessRequest {
  id: number;
  email: string;
  name: string | null;
  organization: string | null;
  reason: string | null;
  source: 'form' | 'sign_up_attempt' | 'waitlist';
  attempts: number;
  created_at: string;
  status: 'open' | 'approved' | 'declined';
  decided_by: string | null;
  decided_at: string | null;
  decided_how: string | null;
}

const HOW_THEY_ASKED: Record<AccessRequest['source'], string> = {
  form: 'Asked on the Request access page',
  sign_up_attempt: 'Tried to sign up (no code was sent: sign-up is invite-only)',
  waitlist: 'Joined the waitlist on the sign-in page',
};

interface IndustryOption {
  key: string;
  label: string;
}

const SELECT = 'h-9 rounded-md border border-gray-700 bg-gray-900 px-2 text-sm text-gray-100 focus:border-gray-500 focus:outline-none';

function CompanyIndustry({ company, options, onDone }: { company: Company; options: IndustryOption[]; onDone: () => void }) {
  const [busy, setBusy] = useState(false);
  if (company.industry === 'all') {
    return <span className="rounded bg-gray-800 px-2 py-0.5 text-xs text-gray-300">All industries (Ingabe)</span>;
  }
  const change = async (value: string) => {
    const label = options.find((o) => o.key === value)?.label ?? value;
    if (company.industry && !window.confirm(`Move ${company.name} to ${label}? Its people will see only ${label} from now on.`)) return;
    setBusy(true);
    try {
      await call(`/${company.id}/industry`, { method: 'PUT', body: JSON.stringify({ industry: value }) });
      toast.success(`${company.name} now works in ${label}`);
      onDone();
    } catch (err) {
      toast.error(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  };
  return (
    <select
      className={SELECT}
      value={company.industry ?? ''}
      disabled={busy}
      onChange={(e) => e.target.value && change(e.target.value)}
      aria-label={`${company.name}'s industry`}
    >
      {!company.industry && <option value="">Industry not set</option>}
      {options.map((o) => (
        <option key={o.key} value={o.key}>
          {o.label}
        </option>
      ))}
    </select>
  );
}

// A company's people, for taking someone out of the company or blocking them from Ingabe
// (src/services/access_removal.py): either takes effect at the person's next click.
interface Member {
  id: string;
  user_id: string;
  email: string | null;
  name: string | null;
  role: string | null;
  status: string | null;
  last_sign_in_at: string | null;
  blocked: boolean;
}

interface BlockedPerson {
  user_id: string;
  email: string | null;
  blocked_at: string;
  blocked_by: string;
  reason: string | null;
}

function CompanyPeople({ company, myEmail, onChange }: { company: Company; myEmail: string | null; onChange: () => void }) {
  const [open, setOpen] = useState(false);
  const [members, setMembers] = useState<Member[] | null>(null);
  const [busy, setBusy] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const res = await call<{ members: Member[] }>(`/${company.id}/people`);
      setMembers(res.members);
    } catch (err) {
      toast.error(err instanceof Error ? err.message : String(err));
    }
  }, [company.id]);

  useEffect(() => {
    if (open) load();
  }, [open, load]);

  const act = async (key: string, run: () => Promise<unknown>, done: string) => {
    setBusy(key);
    try {
      await run();
      toast.success(done);
      await load();
      onChange();
    } catch (err) {
      toast.error(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(null);
    }
  };

  const who = (m: Member) => (m.name ? `${m.name} (${m.email ?? 'no email'})` : (m.email ?? m.user_id));

  return (
    <div className="mt-3">
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        aria-expanded={open}
        className="flex items-center gap-1.5 text-sm text-gray-300 hover:text-gray-100"
      >
        {open ? <ChevronDown className="h-4 w-4" /> : <ChevronRight className="h-4 w-4" />}
        <Users className="h-4 w-4 text-gray-500" />
        People ({company.active_members})
      </button>
      {open && (
        <div className="mt-2 flex flex-col gap-2">
          {members === null ? (
            <Loader2 className="h-4 w-4 animate-spin text-gray-400" />
          ) : members.length === 0 ? (
            <p className="text-sm text-gray-400">Nobody has joined yet.</p>
          ) : (
            <ul className="flex flex-col gap-2">
              {members.map((m) => {
                const me = !!myEmail && (m.email ?? '').toLowerCase() === myEmail.toLowerCase();
                return (
                  <li key={m.id} className="flex flex-wrap items-center gap-x-3 gap-y-2 rounded-md bg-gray-800/40 px-3 py-2 text-sm">
                    <span className="min-w-0 flex-1 break-words text-gray-200">
                      {who(m)}
                      <span className="ml-2 text-xs text-gray-500">
                        {m.role ?? 'member'}
                        {m.last_sign_in_at ? `, last signed in ${m.last_sign_in_at.slice(0, 10)}` : ', never signed in'}
                      </span>
                      {m.blocked && <span className="ml-2 rounded bg-red-900/50 px-1.5 py-0.5 text-xs text-red-300">Blocked</span>}
                    </span>
                    {me ? (
                      <span className="text-xs text-gray-500">You</span>
                    ) : (
                      <span className="flex flex-wrap gap-2">
                        <Button
                          size="sm"
                          variant="outline"
                          disabled={busy !== null}
                          onClick={() => {
                            if (
                              !window.confirm(
                                `Remove ${who(m)} from ${company.name}? They are signed out now and can no longer open ${company.name}'s work. They keep any other company.`,
                              )
                            )
                              return;
                            act(
                              `rm-${m.id}`,
                              () => call(`/${company.id}/members/${m.id}`, { method: 'DELETE' }),
                              `${who(m)} removed from ${company.name}`,
                            );
                          }}
                        >
                          {busy === `rm-${m.id}` ? <Loader2 className="h-4 w-4 animate-spin" /> : <UserMinus className="h-4 w-4" />}
                          Remove from company
                        </Button>
                        {m.blocked ? (
                          <Button
                            size="sm"
                            variant="ghost"
                            disabled={busy !== null}
                            onClick={() =>
                              act(
                                `ub-${m.id}`,
                                () => call(`/people/${m.user_id}/block`, { method: 'DELETE' }),
                                `${who(m)} can sign in again`,
                              )
                            }
                          >
                            {busy === `ub-${m.id}` ? <Loader2 className="h-4 w-4 animate-spin" /> : <Check className="h-4 w-4" />}
                            Unblock
                          </Button>
                        ) : (
                          <Button
                            size="sm"
                            variant="destructive"
                            disabled={busy !== null}
                            onClick={() => {
                              if (
                                !window.confirm(
                                  `Block ${who(m)} from Ingabe? They are signed out now and cannot use Ingabe in any company until you unblock them. Nothing is deleted.`,
                                )
                              )
                                return;
                              act(
                                `bl-${m.id}`,
                                () => call(`/people/${m.user_id}/block`, { method: 'POST', body: JSON.stringify({}) }),
                                `${who(m)} blocked`,
                              );
                            }}
                          >
                            {busy === `bl-${m.id}` ? <Loader2 className="h-4 w-4 animate-spin" /> : <Ban className="h-4 w-4" />}
                            Block
                          </Button>
                        )}
                      </span>
                    )}
                  </li>
                );
              })}
            </ul>
          )}
        </div>
      )}
    </div>
  );
}

function BlockedPeople({ blocked, onChange }: { blocked: BlockedPerson[]; onChange: () => void }) {
  const [busy, setBusy] = useState<string | null>(null);
  if (blocked.length === 0) return null;
  const unblock = async (b: BlockedPerson) => {
    setBusy(b.user_id);
    try {
      await call(`/people/${b.user_id}/block`, { method: 'DELETE' });
      toast.success(`${b.email ?? b.user_id} can sign in again`);
      onChange();
    } catch (err) {
      toast.error(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(null);
    }
  };
  return (
    <section className="flex flex-col gap-2">
      <div>
        <h2 className="text-base font-semibold">Blocked people</h2>
        <p className="text-sm text-gray-400">
          Refused everywhere in Ingabe until unblocked. Unblocking lets them sign in again; their companies are as they were.
        </p>
      </div>
      <ul className="flex flex-col gap-2">
        {blocked.map((b) => (
          <li key={b.user_id} className="flex flex-wrap items-center gap-x-3 gap-y-2 rounded-md border border-red-900/60 px-3 py-2 text-sm">
            <span className="min-w-0 flex-1 break-words text-gray-200">
              {b.email ?? b.user_id}
              <span className="ml-2 text-xs text-gray-500">
                blocked {b.blocked_at.slice(0, 10)} by {b.blocked_by}
                {b.reason ? `: ${b.reason}` : ''}
              </span>
            </span>
            <Button size="sm" variant="ghost" disabled={busy !== null} onClick={() => unblock(b)}>
              {busy === b.user_id ? <Loader2 className="h-4 w-4 animate-spin" /> : <Check className="h-4 w-4" />}
              Unblock
            </Button>
          </li>
        ))}
      </ul>
    </section>
  );
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

function AccessRequestRow({
  req,
  companies,
  onDone,
  onNewCompany,
}: {
  req: AccessRequest;
  companies: Company[];
  onDone: () => void;
  onNewCompany: (req: AccessRequest) => void;
}) {
  const [companyId, setCompanyId] = useState('');
  const [role, setRole] = useState<'member' | 'admin'>('member');
  const [busy, setBusy] = useState<'approve' | 'decline' | null>(null);
  const who = req.name || req.email;

  const approve = async () => {
    const company = companies.find((c) => c.id === companyId);
    setBusy('approve');
    try {
      await call(`/access-requests/${req.id}/approve`, { method: 'POST', body: JSON.stringify({ organization_id: companyId, role }) });
      toast.success(`${who} invited to ${company?.name ?? 'the company'}: WorkOS is emailing ${req.email}`);
      onDone();
    } catch (err) {
      toast.error(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(null);
    }
  };

  const decline = async () => {
    if (!window.confirm(`Decline ${who}'s request? They are not told automatically.`)) return;
    setBusy('decline');
    try {
      await call(`/access-requests/${req.id}/decline`, { method: 'POST' });
      toast.success(`${who}'s request declined`);
      onDone();
    } catch (err) {
      toast.error(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(null);
    }
  };

  return (
    <li className="rounded-md border border-gray-700 p-4">
      <div className="flex flex-wrap items-center gap-3">
        <span className="text-base font-medium">{who}</span>
        {req.name && <span className="text-sm text-gray-400">{req.email}</span>}
        <span className="rounded bg-sky-900/50 px-2 py-0.5 text-xs text-sky-300">{HOW_THEY_ASKED[req.source]}</span>
        <span className="ml-auto text-xs text-gray-500">
          {req.created_at.slice(0, 10)}
          {req.attempts > 1 ? `, ${req.attempts} times` : ''}
        </span>
      </div>
      {(req.organization || req.reason) && (
        <div className="mt-2 space-y-1 text-sm text-gray-300">
          {req.organization && <p>From: {req.organization}</p>}
          {req.reason && <p className="whitespace-pre-wrap text-gray-400">{req.reason}</p>}
        </div>
      )}
      <div className="mt-3 flex flex-wrap items-center gap-2">
        <select
          className={SELECT}
          value={companyId}
          onChange={(e) => setCompanyId(e.target.value)}
          aria-label={`Company to invite ${who} into`}
        >
          <option value="">Invite into…</option>
          {companies.map((c) => (
            <option key={c.id} value={c.id}>
              {c.name}
            </option>
          ))}
        </select>
        <select
          className={SELECT}
          value={role}
          onChange={(e) => setRole(e.target.value as 'member' | 'admin')}
          aria-label={`${who}'s role`}
        >
          <option value="member">as member</option>
          <option value="admin">as admin</option>
        </select>
        <Button size="sm" disabled={!companyId || busy !== null} onClick={approve}>
          {busy === 'approve' ? <Loader2 className="h-4 w-4 animate-spin" /> : <Check className="h-4 w-4" />}
          Approve
        </Button>
        <Button size="sm" variant="outline" disabled={busy !== null} onClick={() => onNewCompany(req)}>
          <UserPlus className="h-4 w-4" />
          New company for them
        </Button>
        <Button size="sm" variant="ghost" disabled={busy !== null} onClick={decline}>
          {busy === 'decline' ? <Loader2 className="h-4 w-4 animate-spin" /> : <X className="h-4 w-4" />}
          Decline
        </Button>
        <a href={`mailto:${req.email}`} className="ml-auto inline-flex items-center gap-1 text-sm text-emerald-400 hover:underline">
          <Mail className="h-4 w-4" />
          Write to them
        </a>
      </div>
    </li>
  );
}

export default function Companies() {
  const { me } = useWorkOSSession();
  const [companies, setCompanies] = useState<Company[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [name, setName] = useState('');
  const [adminEmail, setAdminEmail] = useState('');
  const [newIndustry, setNewIndustry] = useState('');
  const [industries, setIndustries] = useState<IndustryOption[]>([]);
  const [adding, setAdding] = useState(false);
  const [resending, setResending] = useState<string | null>(null);
  const [requests, setRequests] = useState<AccessRequest[]>([]);
  const [blocked, setBlocked] = useState<BlockedPerson[]>([]);
  const [cancelling, setCancelling] = useState<string | null>(null);
  // The request the Add company form is filled in for: once the company is added, the request is approved.
  const [companyFor, setCompanyFor] = useState<AccessRequest | null>(null);

  const load = useCallback(async () => {
    try {
      const [res, asked, cut] = await Promise.all([
        call<{ companies: Company[]; industries: IndustryOption[] }>(''),
        call<{ requests: AccessRequest[] }>('/access-requests'),
        call<{ blocked: BlockedPerson[] }>('/blocked'),
      ]);
      setCompanies(res.companies);
      setIndustries(res.industries);
      setRequests(asked.requests);
      setBlocked(cut.blocked);
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  }, []);

  const startCompanyFor = (req: AccessRequest) => {
    setCompanyFor(req);
    setName(req.organization ?? '');
    setAdminEmail(req.email);
    window.scrollTo({ top: 0, behavior: 'smooth' });
  };

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
      const res = await call<{
        organization_id: string;
        name: string;
        created: boolean;
        invitation: { resent?: boolean };
        industry: string;
        industry_kept: boolean;
      }>('', {
        method: 'POST',
        body: JSON.stringify({ name: name.trim(), admin_email: adminEmail.trim(), industry: newIndustry }),
      });
      const sent = res.invitation?.resent ? 'was already invited: invitation sent again' : 'invitation sent';
      const kept = res.industry_kept
        ? `; it keeps its industry (${industries.find((o) => o.key === res.industry)?.label ?? res.industry}): change it on its row`
        : '';
      toast.success(
        res.created ? `${res.name} added; ${adminEmail.trim()} ${sent}` : `${res.name} already exists; ${adminEmail.trim()} ${sent}${kept}`,
      );
      if (companyFor && companyFor.email === adminEmail.trim().toLowerCase()) {
        // The form just invited them as the first admin: close their request without a second email.
        await call(`/access-requests/${companyFor.id}/approve`, {
          method: 'POST',
          body: JSON.stringify({ organization_id: res.organization_id, role: 'admin', already_invited: true }),
        });
      }
      setCompanyFor(null);
      setName('');
      setAdminEmail('');
      setNewIndustry('');
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

  const cancelInvitation = async (company: Company, inv: Invitation) => {
    if (!window.confirm(`Cancel ${inv.email}'s invitation to ${company.name}? Its link stops working.`)) return;
    setCancelling(inv.id);
    try {
      await call(`/${company.id}/invitations/${inv.id}`, { method: 'DELETE' });
      toast.success(`${inv.email}'s invitation cancelled`);
      await load();
    } catch (err) {
      toast.error(err instanceof Error ? err.message : String(err));
    } finally {
      setCancelling(null);
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
      <div className="flex items-baseline justify-between gap-4">
        <h1 className="text-xl font-semibold">Companies</h1>
        {/* System owner only (first email in PLATFORM_ADMIN_EMAILS): sign-in logs, the sign-in page, API keys and
            corporate logins live in WorkOS. */}
        {me?.is_owner && (
          <a
            href={WORKOS_DASHBOARD}
            target="_blank"
            rel="noreferrer"
            className="inline-flex items-center gap-1 text-sm text-fg-muted hover:text-fg"
          >
            WorkOS dashboard <ExternalLink className="h-3.5 w-3.5" aria-hidden="true" />
          </a>
        )}
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
        <select
          required
          className={SELECT}
          value={newIndustry}
          onChange={(e) => setNewIndustry(e.target.value)}
          aria-label="The company's industry"
        >
          <option value="">Industry…</option>
          {industries.map((o) => (
            <option key={o.key} value={o.key}>
              {o.label}
            </option>
          ))}
        </select>
        <Button type="submit" disabled={adding || !name.trim() || !adminEmail.trim() || !newIndustry}>
          {adding ? <Loader2 className="h-4 w-4 animate-spin" /> : <Plus className="h-4 w-4" />}
          Add company
        </Button>
      </form>

      {companyFor && (
        <p className="-mt-4 flex items-center gap-2 text-sm text-gray-400">
          Adding a company for {companyFor.name || companyFor.email}: choose its industry, then Add company.
          <button type="button" onClick={() => setCompanyFor(null)} className="text-emerald-400 hover:underline">
            Cancel
          </button>
        </p>
      )}

      {error && <p className="rounded-md border border-red-800 bg-red-950/40 px-3 py-2 text-sm text-red-300">{error}</p>}

      {requests.some((r) => r.status === 'open') && (
        <section className="flex flex-col gap-3">
          <div>
            <h2 className="text-base font-semibold">People asking to join</h2>
            <p className="text-sm text-gray-400">
              Approve sends an invitation into the company you pick (WorkOS emails it). Decline closes the request; they are not told
              automatically.
            </p>
          </div>
          <ul className="flex flex-col gap-3">
            {requests
              .filter((r) => r.status === 'open')
              .map((r) => (
                <AccessRequestRow key={r.id} req={r} companies={companies ?? []} onDone={load} onNewCompany={startCompanyFor} />
              ))}
          </ul>
        </section>
      )}

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
                  <CompanyIndustry company={c} options={industries} onDone={load} />
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
                        <Button size="sm" variant="ghost" disabled={cancelling === inv.id} onClick={() => cancelInvitation(c, inv)}>
                          {cancelling === inv.id ? <Loader2 className="h-4 w-4 animate-spin" /> : <X className="h-4 w-4" />}
                          Cancel invitation
                        </Button>
                      </li>
                    ))}
                  </ul>
                )}
                <CompanyPeople company={c} myEmail={me?.user.email ?? null} onChange={load} />
                <div className="mt-3">
                  <InviteAdmin company={c} onDone={load} />
                </div>
              </li>
            );
          })}
        </ul>
      )}

      <BlockedPeople blocked={blocked} onChange={load} />

      {requests.some((r) => r.status !== 'open') && (
        <section className="text-sm text-gray-400">
          <p className="font-medium text-gray-300">Requests decided in the last 30 days</p>
          <ul className="mt-2 flex flex-col gap-1">
            {requests
              .filter((r) => r.status !== 'open')
              .map((r) => (
                <li key={r.id}>
                  {r.name ? `${r.name} (${r.email})` : r.email}: {r.decided_how}
                  {r.decided_by ? `, by ${r.decided_by}` : ''}
                  {r.decided_at ? ` on ${r.decided_at.slice(0, 10)}` : ''}
                </li>
              ))}
          </ul>
        </section>
      )}
    </div>
  );
}
