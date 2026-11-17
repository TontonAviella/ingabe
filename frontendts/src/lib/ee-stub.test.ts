/**
 * Tests for fetchMaybeAuth (apiFetch) in ee-stub.tsx.
 *
 * The sign-in provider is fixed at build time (VITE_AUTH_PROVIDER), so each
 * test stubs the env and imports a fresh copy of the module.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { SIGNED_OUT_EVENT } from '@/components/auth/WorkOSSession';

describe('fetchMaybeAuth', () => {
  let mockFetch: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    mockFetch = vi.fn().mockResolvedValue(new Response('ok', { status: 200 }));
    vi.stubGlobal('fetch', mockFetch);
  });

  afterEach(() => {
    vi.unstubAllEnvs();
    vi.unstubAllGlobals();
    vi.resetModules();
    vi.restoreAllMocks();
  });

  describe('without a sign-in provider', () => {
    beforeEach(() => {
      vi.stubEnv('VITE_AUTH_PROVIDER', '');
    });

    it('makes a plain fetch with no Authorization header or credentials override', async () => {
      const mod = await import('./ee-stub');

      await mod.fetchMaybeAuth('/api/test', { headers: { 'X-Test': '1' } });

      expect(mockFetch).toHaveBeenCalledTimes(1);
      const callInit = mockFetch.mock.calls[0][1];
      expect(new Headers(callInit?.headers).has('Authorization')).toBe(false);
      expect(callInit?.credentials).toBeUndefined();
    });

    it('returns a 401 as is, without a retry or a signed-out event', async () => {
      const mod = await import('./ee-stub');
      const onSignedOut = vi.fn();
      window.addEventListener(SIGNED_OUT_EVENT, onSignedOut);
      mockFetch.mockResolvedValueOnce(new Response('Unauthorized', { status: 401 }));

      const response = await mod.fetchMaybeAuth('/api/protected');

      window.removeEventListener(SIGNED_OUT_EVENT, onSignedOut);
      expect(response.status).toBe(401);
      expect(mockFetch).toHaveBeenCalledTimes(1);
      expect(onSignedOut).not.toHaveBeenCalled();
    });

    it('applies timeout via AbortController when no signal provided', async () => {
      const mod = await import('./ee-stub');

      await mod.fetchMaybeAuth('/api/data');

      const fetchInit = mockFetch.mock.calls[0][1];
      expect(fetchInit?.signal).toBeInstanceOf(AbortSignal);
    });

    it('preserves caller signal and skips timeout', async () => {
      const mod = await import('./ee-stub');
      const controller = new AbortController();

      await mod.fetchMaybeAuth('/api/data', { signal: controller.signal });

      // When caller provides a signal, doFetch passes it through directly.
      // Check the signal is an AbortSignal (identity may differ in jsdom)
      // and that no extra AbortController was created.
      const fetchInit = mockFetch.mock.calls[0][1];
      expect(fetchInit?.signal).toBeInstanceOf(AbortSignal);
      // The caller's controller should still control it
      expect(fetchInit?.signal.aborted).toBe(false);
      controller.abort();
      expect(fetchInit?.signal.aborted).toBe(true);
    });
  });

  describe('a request that never reaches the server', () => {
    it('is said in plain words, not the browser\'s', async () => {
      const mod = await import('./ee-stub');
      for (const words of ['Failed to fetch', 'Load failed', 'NetworkError when attempting to fetch resource.']) {
        mockFetch.mockRejectedValueOnce(new TypeError(words));
        await expect(mod.fetchMaybeAuth('/api/data')).rejects.toMatchObject({ status: 0, message: 'No connection. Try again when it returns.' });
      }
    });

    it('leaves a request the caller stopped as it is', async () => {
      const mod = await import('./ee-stub');
      const stopped = new DOMException('The user aborted a request.', 'AbortError');
      mockFetch.mockRejectedValueOnce(stopped);
      await expect(mod.fetchMaybeAuth('/api/data', { signal: new AbortController().signal })).rejects.toBe(stopped);
    });
  });

  describe('with WorkOS', () => {
    beforeEach(() => {
      vi.stubEnv('VITE_AUTH_PROVIDER', 'workos');
    });

    it('sends the session cookie and no Authorization header', async () => {
      const mod = await import('./ee-stub');

      await mod.fetchMaybeAuth('/api/data');

      expect(mockFetch).toHaveBeenCalledTimes(1);
      const callInit = mockFetch.mock.calls[0][1];
      expect(callInit?.credentials).toBe('same-origin');
      expect(new Headers(callInit?.headers).has('Authorization')).toBe(false);
      expect(callInit?.signal).toBeInstanceOf(AbortSignal);
    });

    it('reports a 401 to the session provider once, without a retry', async () => {
      const mod = await import('./ee-stub');
      const onSignedOut = vi.fn();
      window.addEventListener(SIGNED_OUT_EVENT, onSignedOut);
      mockFetch.mockResolvedValueOnce(new Response('Unauthorized', { status: 401 }));

      const response = await mod.fetchMaybeAuth('/api/protected');

      window.removeEventListener(SIGNED_OUT_EVENT, onSignedOut);
      expect(response.status).toBe(401);
      expect(mockFetch).toHaveBeenCalledTimes(1);
      expect(onSignedOut).toHaveBeenCalledTimes(1);
    });

    it('does not report a signed-out event for other responses', async () => {
      const mod = await import('./ee-stub');
      const onSignedOut = vi.fn();
      window.addEventListener(SIGNED_OUT_EVENT, onSignedOut);
      mockFetch.mockResolvedValueOnce(new Response('Forbidden', { status: 403 }));

      await mod.fetchMaybeAuth('/api/protected');

      window.removeEventListener(SIGNED_OUT_EVENT, onSignedOut);
      expect(onSignedOut).not.toHaveBeenCalled();
    });
  });
});
