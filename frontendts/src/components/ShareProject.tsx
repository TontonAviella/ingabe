import { apiFetch } from '@mundi/ee';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { UserPlus, X } from 'lucide-react';
import { useState } from 'react';
import { toast } from 'sonner';
import { Button } from '@/components/ui/button';
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from '@/components/ui/dialog';

// "Share": who a project is shared with, and for its owner, sharing it with a colleague (someone in one of their
// organizations, from the People page) as editor or viewer, or with anyone by email as a guest: this project only,
// for 1, 7 or 30 days. The server decides who may share and with whom (src/services/project_people.py); this only
// shows its answer.

type Role = 'editor' | 'viewer';
interface Person {
  user_id: string;
  email: string | null;
  role: Role | null;
}
interface Guest {
  id: number;
  email: string;
  role: Role;
  status: 'invited' | 'active';
  expires_at: string;
}
export interface ProjectPeople {
  owner: Person;
  people: Person[];
  can_share: boolean;
  colleagues: Person[];
  guests?: Guest[];
  guest_days?: number[];
}

const until = (iso: string) => new Date(iso).toLocaleDateString(undefined, { day: 'numeric', month: 'short' });
type Change =
  | { kind: 'share'; email: string; role: Role }
  | { kind: 'remove'; userId: string }
  | { kind: 'invite'; email: string; role: Role; days: number }
  | { kind: 'removeGuest'; id: number };

async function send(projectId: string, action: Change): Promise<Response> {
  const json = (method: string, url: string, body: unknown) =>
    apiFetch(url, { method, headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
  const base = `/api/projects/${projectId}`;
  if (action.kind === 'share') return json('PUT', `${base}/people`, { email: action.email, role: action.role });
  if (action.kind === 'invite') return json('POST', `${base}/guests`, { email: action.email, role: action.role, days: action.days });
  if (action.kind === 'remove') return apiFetch(`${base}/people/${action.userId}`, { method: 'DELETE' });
  return apiFetch(`${base}/guests/${action.id}`, { method: 'DELETE' });
}

const ROLE_WORDS: Record<Role, string> = {
  editor: 'Can edit',
  viewer: 'Can view',
};

async function readJson(res: Response): Promise<ProjectPeople> {
  const body = await res.json().catch(() => null);
  if (!res.ok) throw new Error(body?.detail ?? 'Something went wrong. Try again.');
  return body as ProjectPeople;
}

/** The Share button and its dialog for one project. */
export function ShareProject({ projectId }: { projectId: string }) {
  const [open, setOpen] = useState(false);
  return (
    <>
      <Button
        type="button"
        size="sm"
        variant="outline"
        onClick={() => setOpen(true)}
        className="h-7 shrink-0 gap-1.5 px-2 text-xs"
        title="Share this project"
      >
        <UserPlus className="h-3.5 w-3.5" />
        Share
      </Button>
      {open && <ShareDialog projectId={projectId} onClose={() => setOpen(false)} />}
    </>
  );
}

function ShareDialog({ projectId, onClose }: { projectId: string; onClose: () => void }) {
  const queryClient = useQueryClient();
  const key = ['project-people', projectId];
  const { data, error, isLoading } = useQuery<ProjectPeople>({
    queryKey: key,
    queryFn: async () => readJson(await apiFetch(`/api/projects/${projectId}/people`)),
  });
  const [email, setEmail] = useState('');
  const [role, setRole] = useState<Role>('editor');
  const [guestEmail, setGuestEmail] = useState('');
  const [guestRole, setGuestRole] = useState<Role>('viewer');
  const [guestDays, setGuestDays] = useState(7);

  const change = useMutation({
    mutationFn: async (action: Change) => readJson(await send(projectId, action)),
    onSuccess: (people, action) => {
      queryClient.setQueryData(key, people);
      if (action.kind === 'share') {
        setEmail('');
        toast(`Shared with ${action.email}.`);
      }
      if (action.kind === 'invite') {
        setGuestEmail('');
        toast(
          `${action.email} is invited until ${until(people.guests?.find((g) => g.email === action.email.trim().toLowerCase())?.expires_at ?? '')}.`,
        );
      }
    },
    onError: (e) => toast.error((e as Error).message),
  });

  const sharedIds = new Set(data?.people.map((p) => p.user_id));
  const addable = (data?.colleagues ?? []).filter((c) => !sharedIds.has(c.user_id));

  return (
    <Dialog open onOpenChange={(next) => !next && onClose()}>
      <DialogContent className="sm:max-w-md">
        <DialogHeader>
          <DialogTitle>Share this project</DialogTitle>
          <DialogDescription>
            {data?.can_share
              ? 'Give a colleague access, or invite anyone as a guest for a limited time. Editors can open the maps, ask Sage and add layers; viewers can only look. Only you can delete the project.'
              : 'The people who can open this project. Only its owner can change this.'}
          </DialogDescription>
        </DialogHeader>

        {isLoading && <p className="text-sm text-gray-400">Loading…</p>}
        {error && <p className="text-sm text-red-400">{(error as Error).message}</p>}

        {data && (
          <div className="flex flex-col gap-4">
            <ul className="flex flex-col gap-2 text-sm" aria-label="People with access">
              <li className="flex items-center justify-between gap-2">
                <span className="truncate">{data.owner.email ?? 'Owner'}</span>
                <span className="text-gray-400">Owner</span>
              </li>
              {data.people.map((p) => (
                <li key={p.user_id} className="flex items-center justify-between gap-2">
                  <span className="truncate">{p.email ?? 'Someone'}</span>
                  {data.can_share ? (
                    <span className="flex items-center gap-1">
                      <select
                        aria-label={`Role of ${p.email ?? 'this person'}`}
                        value={p.role ?? 'viewer'}
                        disabled={change.isPending}
                        onChange={(e) => p.email && change.mutate({ kind: 'share', email: p.email, role: e.target.value as Role })}
                        className="rounded border border-gray-600 bg-transparent px-1 py-0.5 text-xs"
                      >
                        <option value="editor">{ROLE_WORDS.editor}</option>
                        <option value="viewer">{ROLE_WORDS.viewer}</option>
                      </select>
                      <button
                        type="button"
                        aria-label={`Stop sharing with ${p.email ?? 'this person'}`}
                        disabled={change.isPending}
                        onClick={() => change.mutate({ kind: 'remove', userId: p.user_id })}
                        className="cursor-pointer rounded p-1 text-gray-400 hover:text-white"
                      >
                        <X className="h-3.5 w-3.5" />
                      </button>
                    </span>
                  ) : (
                    <span className="text-gray-400">{p.role ? ROLE_WORDS[p.role] : ''}</span>
                  )}
                </li>
              ))}
              {(data.guests ?? []).map((g) => (
                <li key={`guest-${g.id}`} className="flex items-center justify-between gap-2">
                  <span className="flex min-w-0 flex-col">
                    <span className="truncate">{g.email}</span>
                    <span className="text-xs text-gray-400">
                      {g.status === 'invited' ? 'Invited, not signed in yet' : 'Guest'} · {ROLE_WORDS[g.role].toLowerCase()} until{' '}
                      {until(g.expires_at)}
                    </span>
                  </span>
                  <button
                    type="button"
                    aria-label={`End ${g.email}'s access now`}
                    disabled={change.isPending}
                    onClick={() => change.mutate({ kind: 'removeGuest', id: g.id })}
                    className="cursor-pointer rounded p-1 text-gray-400 hover:text-white"
                  >
                    <X className="h-3.5 w-3.5" />
                  </button>
                </li>
              ))}
            </ul>

            {data.can_share &&
              (addable.length > 0 ? (
                <form
                  className="flex flex-col gap-2 border-t border-gray-700 pt-4 sm:flex-row"
                  onSubmit={(e) => {
                    e.preventDefault();
                    if (email) change.mutate({ kind: 'share', email, role });
                  }}
                >
                  <select
                    aria-label="Colleague"
                    value={email}
                    onChange={(e) => setEmail(e.target.value)}
                    className="min-w-0 flex-1 rounded border border-gray-600 bg-transparent px-2 py-1.5 text-sm"
                  >
                    <option value="">Choose a colleague…</option>
                    {addable.map((c) => (
                      <option key={c.user_id} value={c.email ?? ''}>
                        {c.email}
                      </option>
                    ))}
                  </select>
                  <select
                    aria-label="Role"
                    value={role}
                    onChange={(e) => setRole(e.target.value as Role)}
                    className="rounded border border-gray-600 bg-transparent px-2 py-1.5 text-sm"
                  >
                    <option value="editor">{ROLE_WORDS.editor}</option>
                    <option value="viewer">{ROLE_WORDS.viewer}</option>
                  </select>
                  <Button type="submit" disabled={!email || change.isPending} className="bg-[#D9A066] text-[#140E0B] hover:bg-[#E9B987]">
                    {change.isPending ? 'Sharing…' : 'Share'}
                  </Button>
                </form>
              ) : (
                <p className="border-t border-gray-700 pt-4 text-sm text-gray-400">
                  {data.colleagues.length > 0
                    ? 'Everyone in your organization already has it.'
                    : 'Nobody else is in your organization yet.'}
                </p>
              ))}

            {data.can_share && (
              <form
                className="flex flex-col gap-2 border-t border-gray-700 pt-4"
                onSubmit={(e) => {
                  e.preventDefault();
                  if (guestEmail.trim()) change.mutate({ kind: 'invite', email: guestEmail.trim(), role: guestRole, days: guestDays });
                }}
              >
                <span className="text-sm font-semibold">Someone outside your organization</span>
                <span className="text-xs text-gray-400">
                  They see this project only, until the time runs out. They open it by signing in to Ingabe with this email.
                </span>
                <input
                  id="guest-email"
                  type="email"
                  aria-label="Guest's email"
                  placeholder="name@example.com"
                  value={guestEmail}
                  onChange={(e) => setGuestEmail(e.target.value)}
                  className="min-w-0 rounded border border-gray-600 bg-transparent px-2 py-1.5 text-sm"
                />
                <div className="flex flex-wrap gap-2">
                  <select
                    id="guest-role"
                    aria-label="Guest's role"
                    value={guestRole}
                    onChange={(e) => setGuestRole(e.target.value as Role)}
                    className="rounded border border-gray-600 bg-transparent px-2 py-1.5 text-sm"
                  >
                    <option value="viewer">{ROLE_WORDS.viewer}</option>
                    <option value="editor">{ROLE_WORDS.editor}</option>
                  </select>
                  <select
                    id="guest-days"
                    aria-label="How long"
                    value={guestDays}
                    onChange={(e) => setGuestDays(Number(e.target.value))}
                    className="rounded border border-gray-600 bg-transparent px-2 py-1.5 text-sm"
                  >
                    {(data.guest_days ?? [1, 7, 30]).map((d) => (
                      <option key={d} value={d}>
                        for {d} {d === 1 ? 'day' : 'days'}
                      </option>
                    ))}
                  </select>
                  <Button
                    type="submit"
                    disabled={!guestEmail.trim() || change.isPending}
                    className="ml-auto bg-[#D9A066] text-[#140E0B] hover:bg-[#E9B987]"
                  >
                    {change.isPending ? 'Inviting…' : 'Invite'}
                  </Button>
                </div>
              </form>
            )}
          </div>
        )}
      </DialogContent>
    </Dialog>
  );
}
