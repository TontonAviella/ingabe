import { Building2, Check, ChevronsUpDown, Loader2, LogOut, User } from 'lucide-react';
import React, { createContext, useCallback, useContext, useEffect, useMemo, useState } from 'react';
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu';
import { useSidebar } from '@/components/ui/sidebar';

// WorkOS sign-in, server-side: the session lives in an HTTP-only cookie set by
// the backend (/auth/callback). The app only asks /api/auth/me who is signed in
// and sends people to /auth/login when they are not.

export interface WorkOSOrganization {
  id: string;
  name: string;
  role: string | null;
}

export interface WorkOSMe {
  user: { email: string | null; first_name: string | null; last_name: string | null; picture: string | null };
  organization: WorkOSOrganization | null;
  organizations: WorkOSOrganization[];
}

type Status = 'loading' | 'signedIn' | 'signedOut';

interface SessionValue {
  status: Status;
  me: WorkOSMe | null;
  switchOrganization: (organizationId: string | null) => Promise<void>;
}

const SessionContext = createContext<SessionValue>({ status: 'loading', me: null, switchOrganization: async () => undefined });

export const SIGNED_OUT_EVENT = 'mundi:signed-out';

export function signInUrl(returnTo = window.location.pathname + window.location.search): string {
  return `/auth/login?return_to=${encodeURIComponent(returnTo)}`;
}

export function WorkOSSessionProvider({ children }: React.PropsWithChildren) {
  const [status, setStatus] = useState<Status>('loading');
  const [me, setMe] = useState<WorkOSMe | null>(null);

  const load = useCallback(async () => {
    try {
      const res = await fetch('/api/auth/me', { credentials: 'same-origin' });
      if (res.status === 401) {
        setMe(null);
        setStatus('signedOut');
        return;
      }
      if (!res.ok) throw new Error(`auth/me ${res.status}`);
      setMe(await res.json());
      setStatus('signedIn');
    } catch (err) {
      console.warn('[Auth] Could not check the session:', err);
      setStatus((s) => (s === 'loading' ? 'signedOut' : s));
    }
  }, []);

  useEffect(() => {
    load();
    const onSignedOut = () => setStatus('signedOut');
    window.addEventListener(SIGNED_OUT_EVENT, onSignedOut);
    return () => window.removeEventListener(SIGNED_OUT_EVENT, onSignedOut);
  }, [load]);

  const switchOrganization = useCallback(async (organizationId: string | null) => {
    const res = await fetch('/api/auth/organization', {
      method: 'POST',
      credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ organization_id: organizationId }),
    });
    if (!res.ok) throw new Error(`Could not switch organization (${res.status})`);
    // Everything on screen was loaded for the previous organization: start fresh.
    window.location.assign('/');
  }, []);

  const value = useMemo(() => ({ status, me, switchOrganization }), [status, me, switchOrganization]);
  return <SessionContext.Provider value={value}>{children}</SessionContext.Provider>;
}

export function useWorkOSSession(): SessionValue {
  return useContext(SessionContext);
}

export function WorkOSRequireAuth({ children }: React.PropsWithChildren) {
  const { status } = useWorkOSSession();
  useEffect(() => {
    if (status === 'signedOut') window.location.assign(signInUrl());
  }, [status]);
  if (status === 'signedIn') return <>{children}</>;
  return (
    <div className="flex min-h-screen items-center justify-center bg-background text-muted-foreground">
      <Loader2 className="mr-2 h-4 w-4 animate-spin" />
      <span className="text-sm">{status === 'loading' ? 'Checking your session…' : 'Redirecting to sign in…'}</span>
    </div>
  );
}

function displayName(me: WorkOSMe): string {
  const name = [me.user.first_name, me.user.last_name].filter(Boolean).join(' ');
  return name || me.user.email || 'Signed in';
}

function initials(me: WorkOSMe): string {
  const parts = [me.user.first_name, me.user.last_name].filter(Boolean) as string[];
  if (parts.length)
    return parts
      .map((p) => p[0])
      .join('')
      .slice(0, 2)
      .toUpperCase();
  return (me.user.email ?? '?')[0].toUpperCase();
}

function Avatar({ me, size = 'h-8 w-8' }: { me: WorkOSMe; size?: string }) {
  return me.user.picture ? (
    <img src={me.user.picture} alt="" className={`${size} rounded-full object-cover`} referrerPolicy="no-referrer" />
  ) : (
    <span className={`${size} inline-flex items-center justify-center rounded-full bg-emerald-600 text-xs font-semibold text-white`}>
      {initials(me)}
    </span>
  );
}

const ROLE_LABEL: Record<string, string> = { admin: 'Admin', owner: 'Owner', member: 'Member' };

function roleLabel(role: string | null | undefined): string {
  return role ? (ROLE_LABEL[role] ?? role.charAt(0).toUpperCase() + role.slice(1)) : 'Member';
}

/** Organization switcher for the sidebar. Hidden for users who belong to no organization. */
export function WorkOSOrgSwitcher() {
  const { status, me, switchOrganization } = useWorkOSSession();
  const { state } = useSidebar();
  const [switching, setSwitching] = useState<string | null>(null);
  if (status !== 'signedIn' || !me || me.organizations.length === 0) return null;
  const current = me.organization;
  const collapsed = state === 'collapsed';

  const pick = async (id: string | null) => {
    if (id === (current?.id ?? null)) return;
    setSwitching(id ?? 'personal');
    try {
      await switchOrganization(id);
    } catch (err) {
      console.warn(err);
      setSwitching(null);
    }
  };

  return (
    <div className={collapsed ? 'px-1 py-2' : 'px-3 py-2'}>
      <DropdownMenu>
        <DropdownMenuTrigger asChild>
          <button
            type="button"
            title={current ? `${current.name} (${roleLabel(current.role)})` : 'Choose an organization'}
            className="flex w-full items-center gap-2 rounded-md border border-gray-700 bg-gray-800/60 px-2 py-1.5 text-left text-sm text-gray-100 hover:bg-gray-700/70 focus:outline-none focus-visible:ring-2 focus-visible:ring-emerald-500"
          >
            <span className="inline-flex h-7 w-7 shrink-0 items-center justify-center rounded-md bg-indigo-600 text-white">
              <Building2 className="h-4 w-4" />
            </span>
            {!collapsed && (
              <>
                <span className="min-w-0 flex-1">
                  <span className="block truncate font-medium">{current?.name ?? 'No organization'}</span>
                  <span className="block truncate text-xs text-gray-400">{current ? roleLabel(current.role) : 'Personal workspace'}</span>
                </span>
                {switching ? (
                  <Loader2 className="h-4 w-4 animate-spin text-gray-400" />
                ) : (
                  <ChevronsUpDown className="h-4 w-4 text-gray-400" />
                )}
              </>
            )}
          </button>
        </DropdownMenuTrigger>
        <DropdownMenuContent side="right" align="start" className="w-64">
          <DropdownMenuLabel className="text-xs font-normal text-muted-foreground">Organizations</DropdownMenuLabel>
          {me.organizations.map((org) => (
            <DropdownMenuItem key={org.id} onSelect={() => pick(org.id)} className="cursor-pointer">
              <Building2 className="mr-2 h-4 w-4" />
              <span className="min-w-0 flex-1">
                <span className="block truncate">{org.name}</span>
                <span className="block text-xs text-muted-foreground">{roleLabel(org.role)}</span>
              </span>
              {org.id === current?.id && <Check className="ml-2 h-4 w-4" />}
            </DropdownMenuItem>
          ))}
          <DropdownMenuSeparator />
          <DropdownMenuItem onSelect={() => pick(null)} className="cursor-pointer">
            <User className="mr-2 h-4 w-4" />
            <span className="flex-1">Personal workspace</span>
            {!current && <Check className="ml-2 h-4 w-4" />}
          </DropdownMenuItem>
        </DropdownMenuContent>
      </DropdownMenu>
    </div>
  );
}

/** Signed-in user and sign-out, for the sidebar. */
export function WorkOSAccountMenu() {
  const { status, me } = useWorkOSSession();
  const { state } = useSidebar();
  if (status !== 'signedIn' || !me) return null;
  const collapsed = state === 'collapsed';

  return (
    <div className={collapsed ? 'px-1 py-2' : 'px-3 py-2'}>
      <DropdownMenu>
        <DropdownMenuTrigger asChild>
          <button
            type="button"
            title={displayName(me)}
            className="flex w-full items-center gap-2 rounded-md px-1 py-1 text-left text-sm text-gray-100 hover:bg-gray-700/70 focus:outline-none focus-visible:ring-2 focus-visible:ring-emerald-500"
          >
            <Avatar me={me} />
            {!collapsed && (
              <span className="min-w-0 flex-1">
                <span className="block truncate font-medium">{displayName(me)}</span>
                {me.user.email && displayName(me) !== me.user.email && (
                  <span className="block truncate text-xs text-gray-400">{me.user.email}</span>
                )}
              </span>
            )}
          </button>
        </DropdownMenuTrigger>
        <DropdownMenuContent side="right" align="end" className="w-60">
          <DropdownMenuLabel>
            <span className="block truncate">{displayName(me)}</span>
            {me.user.email && <span className="block truncate text-xs font-normal text-muted-foreground">{me.user.email}</span>}
          </DropdownMenuLabel>
          <DropdownMenuSeparator />
          <DropdownMenuItem asChild className="cursor-pointer">
            <a href="/auth/logout">
              <LogOut className="mr-2 h-4 w-4" />
              Sign out
            </a>
          </DropdownMenuItem>
        </DropdownMenuContent>
      </DropdownMenu>
    </div>
  );
}
