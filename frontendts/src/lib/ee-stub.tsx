import { ClerkProvider, RedirectToSignIn, SignedIn, SignedOut, UserButton, useAuth } from '@clerk/clerk-react';
import MaplibreGeocoder from '@maplibre/maplibre-gl-geocoder';
import React, { useEffect } from 'react';
import '@maplibre/maplibre-gl-geocoder/dist/maplibre-gl-geocoder.css';

const CLERK_PUBLISHABLE_KEY = import.meta.env.VITE_CLERK_PUBLISHABLE_KEY;
// When set, this app acts as a Clerk satellite domain and redirects sign-in
// to the primary domain (NozaLabs). Example: "https://nozalabs.rw/sign-in"
const CLERK_SIGN_IN_URL = import.meta.env.VITE_CLERK_SIGN_IN_URL;
const CLERK_SIGN_UP_URL = import.meta.env.VITE_CLERK_SIGN_UP_URL;

// Detect broken satellite config: sign-in URL points to localhost but we're
// running on a real domain. This happens when dev .env leaks into production.
const _signInIsLocalhost = CLERK_SIGN_IN_URL && new URL(CLERK_SIGN_IN_URL, window.location.href).hostname === 'localhost';
const _isProductionDomain = typeof window !== 'undefined' && window.location.hostname !== 'localhost' && window.location.hostname !== '127.0.0.1';
const IS_SATELLITE_BROKEN = Boolean(_signInIsLocalhost && _isProductionDomain);
const IS_SATELLITE = Boolean(CLERK_SIGN_IN_URL) && !IS_SATELLITE_BROKEN;
const IS_DEV_KEY = CLERK_PUBLISHABLE_KEY?.startsWith('pk_test_');

// ── init ────────────────────────────────────────────────────────────────
export async function init(): Promise<void> {
  if (!CLERK_PUBLISHABLE_KEY) {
    console.warn('[Auth] VITE_CLERK_PUBLISHABLE_KEY not set — auth disabled');
  }
  if (IS_DEV_KEY && _isProductionDomain) {
    console.error('[Auth] Clerk DEVELOPMENT key detected on production domain. Set VITE_CLERK_PUBLISHABLE_KEY to a pk_live_* key.');
  }
  if (IS_SATELLITE_BROKEN) {
    console.error('[Auth] Satellite sign-in URL points to localhost but app is running on', window.location.hostname, '— satellite mode disabled. Set VITE_CLERK_SIGN_IN_URL to the real primary domain sign-in URL.');
  } else if (IS_SATELLITE) {
    console.log('[Auth] Running as satellite domain — sign-in via', CLERK_SIGN_IN_URL);
  }
}

// ── Provider ────────────────────────────────────────────────────────────
export function Provider({ children }: React.PropsWithChildren) {
  if (!CLERK_PUBLISHABLE_KEY) {
    return <>{children}</>;
  }

  // Satellite mode: redirect sign-in/sign-up to the primary domain (NozaLabs)
  const satelliteProps = IS_SATELLITE
    ? {
        isSatellite: true as const,
        domain: (url: URL) => url.host,
        signInUrl: CLERK_SIGN_IN_URL,
        signUpUrl: CLERK_SIGN_UP_URL || CLERK_SIGN_IN_URL.replace('/sign-in', '/sign-up'),
      }
    : {};

  return (
    <ClerkProvider publishableKey={CLERK_PUBLISHABLE_KEY} {...satelliteProps}>
      <_SetTokenProvider>{children}</_SetTokenProvider>
    </ClerkProvider>
  );
}

// ── RequireAuth ─────────────────────────────────────────────────────────
export function RequireAuth({ children }: React.PropsWithChildren) {
  if (!CLERK_PUBLISHABLE_KEY) {
    return <>{children}</>;
  }

  return (
    <>
      <SignedIn>{children}</SignedIn>
      <SignedOut>
        {IS_SATELLITE_BROKEN ? <_BrokenAuthFallback /> : <RedirectToSignIn />}
      </SignedOut>
    </>
  );
}

function _BrokenAuthFallback() {
  return (
    <div className="flex items-center justify-center min-h-screen bg-background">
      <div className="text-center max-w-md px-6">
        <h1 className="text-2xl font-bold mb-3">Sign-in unavailable</h1>
        <p className="text-muted-foreground mb-4">
          Authentication is misconfigured on this server. The sign-in service
          cannot be reached.
        </p>
        <p className="text-sm text-muted-foreground">
          If you are the administrator, check that <code className="bg-muted px-1 rounded">VITE_CLERK_SIGN_IN_URL</code> points
          to your primary domain, not localhost.
        </p>
      </div>
    </div>
  );
}

// ── OptionalAuth ────────────────────────────────────────────────────────
export function OptionalAuth({ children }: React.PropsWithChildren) {
  return <>{children}</>;
}

// ── Routes (sign-in / sign-up pages) ────────────────────────────────────
export function Routes(_reactRouterDom: unknown): React.ReactNode | null {
  // Clerk's hosted UI handles sign-in/sign-up, no extra routes needed
  return null;
}

// ── AccountMenu ─────────────────────────────────────────────────────────
export function AccountMenu(): React.ReactNode | null {
  if (!CLERK_PUBLISHABLE_KEY) {
    return null;
  }

  return (
    <div className="px-3 py-2">
      <UserButton
        afterSignOutUrl="/"
        appearance={{
          elements: {
            avatarBox: 'w-8 h-8',
          },
        }}
      />
    </div>
  );
}

// ── ScheduleCallButton ──────────────────────────────────────────────────
export function ScheduleCallButton(): React.ReactNode | null {
  return null;
}

// ── ShareEmbedModal ─────────────────────────────────────────────────────
export function ShareEmbedModal(_props: { isOpen: boolean; onClose: () => void; projectId?: string }): React.ReactNode | null {
  return null;
}

// ── ApiKeys ─────────────────────────────────────────────────────────────
export function ApiKeys(): React.ReactNode | null {
  if (!CLERK_PUBLISHABLE_KEY) {
    return null;
  }

  return (
    <div className="p-6 max-w-2xl mx-auto">
      <h1 className="text-2xl font-bold mb-4">Account Settings</h1>
      <p className="text-muted-foreground">Manage your account from the user menu in the sidebar.</p>
    </div>
  );
}

// ── getJwt ──────────────────────────────────────────────────────────────
// This is called outside React components (e.g. in useEffect callbacks).
// We store the getToken function from useAuth when the Provider mounts.
let _getTokenFn: (() => Promise<string | null>) | null = null;
// Cached token for synchronous access (used by MapLibre transformRequest)
let _cachedToken: string | null = null;

export function _SetTokenProvider({ children }: React.PropsWithChildren) {
  const { getToken } = useAuth();

  useEffect(() => {
    _getTokenFn = getToken;
    const refreshToken = () => {
      getToken().then((t) => {
        _cachedToken = t;
      });
    };
    // Eagerly cache token so transformRequest has it immediately
    refreshToken();
    // Refresh cached token every 50s (Clerk tokens expire ~60s)
    const interval = setInterval(refreshToken, 50_000);
    // Refresh immediately when tab becomes visible again.
    // Browsers throttle setInterval in background tabs, so the cached token
    // used by MapLibre's synchronous transformRequest is often stale after
    // the user switches back to the tab.
    const handleVisibility = () => {
      if (document.visibilityState === 'visible') refreshToken();
    };
    document.addEventListener('visibilitychange', handleVisibility);
    return () => {
      _getTokenFn = null;
      _cachedToken = null;
      clearInterval(interval);
      document.removeEventListener('visibilitychange', handleVisibility);
    };
  }, [getToken]);

  return <>{children}</>;
}

/**
 * Synchronous access to the cached Clerk JWT token.
 * Used by MapLibre's transformRequest to add Bearer tokens to tile requests.
 */
export function getCachedToken(): string | null {
  return _cachedToken;
}

// ── useIsReady ──────────────────────────────────────────────────────────
// Returns true once Clerk has loaded and the user is signed in.
// Use this to gate React Query `enabled` so fetches don't fire before auth.
export function useIsReady(): boolean {
  if (!CLERK_PUBLISHABLE_KEY) {
    return true; // no auth — always ready
  }
  // biome-ignore lint/correctness/useHookAtTopLevel: CLERK_PUBLISHABLE_KEY is a build-time constant, hook call order is stable per build
  const { isLoaded, isSignedIn } = useAuth();
  return isLoaded && (isSignedIn ?? false);
}

// ── apiFetch ────────────────────────────────────────────────────────────
// Drop-in replacement for fetch() that attaches the Clerk Bearer token.
// Use this for all /api/* calls instead of raw fetch().
export { fetchMaybeAuth as apiFetch };

export async function getJwt(): Promise<string | undefined> {
  if (!CLERK_PUBLISHABLE_KEY) {
    return undefined;
  }

  if (_getTokenFn) {
    const token = await _getTokenFn();
    _cachedToken = token;
    return token ?? undefined;
  }

  return undefined;
}

// ── fetchMaybeAuth ──────────────────────────────────────────────────────

/** Default request timeout in milliseconds (30 seconds). */
const DEFAULT_TIMEOUT_MS = 30_000;

/**
 * Drop-in fetch() replacement with:
 * - Clerk Bearer token injection
 * - Request timeout via AbortController (30s default)
 * - Single-retry on 401 with fresh token
 */
export async function fetchMaybeAuth(input: RequestInfo | URL, init?: RequestInit): Promise<Response> {
  const doFetch = (fetchInit?: RequestInit) => {
    // Wire up timeout via AbortController (skip if caller already set a signal)
    if (fetchInit?.signal) return fetch(input, fetchInit);

    const controller = new AbortController();
    const timeoutId = setTimeout(() => controller.abort(), DEFAULT_TIMEOUT_MS);
    return fetch(input, { ...fetchInit, signal: controller.signal }).finally(() => clearTimeout(timeoutId));
  };

  if (!CLERK_PUBLISHABLE_KEY) {
    return doFetch(init);
  }

  const token = await getJwt();
  if (!token) {
    return doFetch(init);
  }

  const headers = new Headers(init?.headers);
  headers.set('Authorization', `Bearer ${token}`);
  const hasRetriedBefore = headers.has('X-Retry-After-401');
  const response = await doFetch({ ...init, headers });

  // If we get 401 Unauthorized, token might have expired mid-operation.
  // Retry once with a fresh token (Clerk auto-refreshes expired tokens).
  if (response.status === 401 && !hasRetriedBefore) {
    console.log('401 received, refreshing token and retrying...');
    const freshToken = await getJwt();
    if (freshToken && freshToken !== token) {
      const retryHeaders = new Headers(init?.headers);
      retryHeaders.set('Authorization', `Bearer ${freshToken}`);
      retryHeaders.set('X-Retry-After-401', 'true'); // Prevent infinite retry
      return doFetch({ ...init, headers: retryHeaders });
    }
  }

  return response;
}

// ── createGeocoder ──────────────────────────────────────────────────────
// nominatim allows limited geocoding results
export function createGeocoder(maplibregl: any) {
  const geocoderApi = {
    forwardGeocode: async (config: { query: string; limit?: number }) => {
      const features: any[] = [];
      const url = new URL('https://nominatim.openstreetmap.org/search');
      url.searchParams.set('q', config.query);
      url.searchParams.set('format', 'geojson');
      url.searchParams.set('polygon_geojson', '1');
      url.searchParams.set('addressdetails', '1');
      url.searchParams.set('limit', String(config.limit ?? 5));

      const response = await fetch(url.toString(), {
        headers: { Accept: 'application/geo+json' },
      });
      const geojson = await response.json();

      for (const feature of geojson.features || []) {
        if (!feature?.bbox || feature.bbox.length !== 4) continue;
        const [minx, miny, maxx, maxy] = feature.bbox;
        const center = [minx + (maxx - minx) / 2, miny + (maxy - miny) / 2];
        features.push({
          type: 'Feature',
          geometry: { type: 'Point', coordinates: center },
          place_name: feature.properties?.display_name,
          properties: feature.properties,
          text: feature.properties?.display_name,
          place_type: ['place'],
          center,
          bbox: feature.bbox,
        });
      }
      return { features };
    },
  };

  return new MaplibreGeocoder(geocoderApi as any, {
    maplibregl,
    placeholder: 'Search places',
    marker: false,
  });
}
