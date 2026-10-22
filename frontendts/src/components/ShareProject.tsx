import { apiFetch } from '@mundi/ee';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { UserPlus, X } from 'lucide-react';
import { useState } from 'react';
import { toast } from 'sonner';
import { Button } from '@/components/ui/button';
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from '@/components/ui/dialog';

// "Share": who a project is shared with, and for its owner, sharing it with a colleague (someone in one of their
// organizations, from the People page) as editor or viewer. The server decides who may share and with whom
// (src/services/project_people.py); this only shows its answer.

type Role = 'editor' | 'viewer';
interface Person {
  user_id: string;
  email: string | null;
  role: Role | null;
}
export interface ProjectPeople {
  owner: Person;
  people: Person[];
  can_share: boolean;
  colleagues: Person[];
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

  const change = useMutation({
    mutationFn: async (action: { kind: 'share'; email: string; role: Role } | { kind: 'remove'; userId: string }) =>
      readJson(
        action.kind === 'share'
          ? await apiFetch(`/api/projects/${projectId}/people`, {
              method: 'PUT',
              headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify({ email: action.email, role: action.role }),
            })
          : await apiFetch(`/api/projects/${projectId}/people/${action.userId}`, { method: 'DELETE' }),
      ),
    onSuccess: (people, action) => {
      queryClient.setQueryData(key, people);
      if (action.kind === 'share') {
        setEmail('');
        toast(`Shared with ${action.email}.`);
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
              ? 'Give a colleague from your organization access. Editors can open the maps, ask Sage and add layers; viewers can only look. Only you can delete the project.'
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
                    ? 'It is shared with everyone in your organization.'
                    : 'Nobody else is in your organization yet. Invite people from People in the sidebar, then share.'}
                </p>
              ))}
          </div>
        )}
      </DialogContent>
    </Dialog>
  );
}
