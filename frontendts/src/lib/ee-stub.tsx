import type React from 'react';
import {
  SIGNED_OUT_EVENT,
  useWorkOSSession,
  WorkOSAccountMenu,
  WorkOSOrgSwitcher,
  WorkOSRequireAuth,
  WorkOSSessionProvider,
} from '@/components/auth/WorkOSSession';

// Sign-in provider, fixed at build time. "workos": the backend keeps the
// session in an HTTP-only cookie (no tokens in the browser). Anything else:
// no sign-in, the backend's MUNDI_AUTH_MODE decides (legacy single-user mode).
const IS_WORKOS = (import.meta.env.VITE_AUTH_PROVIDER || '').toLowerCase() === 'workos';

// ── init ────────────────────────────────────────────────────────────────
export async function init(): Promise<void> {
  if (!IS_WORKOS) {
    console.warn('[Auth] VITE_AUTH_PROVIDER is not "workos" — sign-in disabled');
  }
}

// ── Provider ────────────────────────────────────────────────────────────
export function Provider({ children }: React.PropsWithChildren) {
  if (IS_WORKOS) {
    return <WorkOSSessionProvider>{children}</WorkOSSessionProvider>;
  }
  return <>{children}</>;
}

// ── RequireAuth ─────────────────────────────────────────────────────────
export function RequireAuth({ children }: React.PropsWithChildren) {
  if (IS_WORKOS) {
    return <WorkOSRequireAuth>{children}</WorkOSRequireAuth>;
  }
  return <>{children}</>;
}

// ── OptionalAuth ────────────────────────────────────────────────────────
export function OptionalAuth({ children }: React.PropsWithChildren) {
  return <>{children}</>;
}

// ── AccountMenu ─────────────────────────────────────────────────────────
export function AccountMenu(): React.ReactNode | null {
  return IS_WORKOS ? <WorkOSAccountMenu /> : null;
}

// ── OrgSwitcher ────────────────────────────────────────────────────────
export function OrgSwitcher(): React.ReactNode | null {
  return IS_WORKOS ? <WorkOSOrgSwitcher /> : null;
}

// ── useIsReady ──────────────────────────────────────────────────────────
// Returns true once the session is known and the user is signed in.
// Use this to gate React Query `enabled` so fetches don't fire before auth.
export function useIsReady(): boolean {
  if (IS_WORKOS) {
    // biome-ignore lint/correctness/useHookAtTopLevel: IS_WORKOS is a build-time constant, hook call order is stable per build
    return useWorkOSSession().status === 'signedIn';
  }
  return true; // no auth — always ready
}

// ── useIsSignedOut ─────────────────────────────────────────────────────
// Returns true when the session is known and the user is definitively NOT signed in.
// Useful for showing "sign in" prompts on OptionalAuth pages.
export function useIsSignedOut(): boolean {
  if (IS_WORKOS) {
    // biome-ignore lint/correctness/useHookAtTopLevel: IS_WORKOS is a build-time constant, hook call order is stable per build
    return useWorkOSSession().status === 'signedOut';
  }
  return false; // no auth — never "signed out"
}

// ── apiFetch ────────────────────────────────────────────────────────────
// Use this for all /api/* calls instead of raw fetch().
export { fetchMaybeAuth as apiFetch };

// ── fetchMaybeAuth ──────────────────────────────────────────────────────

/** Default request timeout in milliseconds (30 seconds). */
const DEFAULT_TIMEOUT_MS = 30_000;

/**
 * Drop-in fetch() replacement with:
 * - Request timeout via AbortController (30s default)
 * - With WorkOS: the session cookie on every same-origin request, and a 401
 *   reported to the session provider (which sends the user to sign in)
 */
export async function fetchMaybeAuth(input: RequestInfo | URL, init?: RequestInit): Promise<Response> {
  const doFetch = (fetchInit?: RequestInit) => {
    // Wire up timeout via AbortController (skip if caller already set a signal)
    if (fetchInit?.signal) return fetch(input, fetchInit);

    const controller = new AbortController();
    const timeoutId = setTimeout(() => controller.abort(), DEFAULT_TIMEOUT_MS);
    return fetch(input, { ...fetchInit, signal: controller.signal }).finally(() => clearTimeout(timeoutId));
  };

  if (IS_WORKOS) {
    // The session cookie goes with every same-origin request; a 401 means it
    // ended, so tell the session provider (which sends the user to sign in).
    const response = await doFetch({ credentials: 'same-origin', ...init });
    if (response.status === 401) window.dispatchEvent(new Event(SIGNED_OUT_EVENT));
    return response;
  }
  return doFetch(init);
}
