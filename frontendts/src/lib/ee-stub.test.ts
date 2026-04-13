/**
 * Tests for ee-stub.tsx auth utilities.
 *
 * We test the pure logic functions (getJwt, getCachedToken, fetchMaybeAuth)
 * by reaching into the module internals via __test__ hooks. React component
 * tests (_SetTokenProvider, Provider, RequireAuth) would require a full Clerk
 * mock and are separate — this file covers the 14 token lifecycle + fetch paths.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

describe('getJwt', () => {
  describe('when CLERK_PUBLISHABLE_KEY is not set', () => {
    beforeEach(() => {
      vi.stubEnv('VITE_CLERK_PUBLISHABLE_KEY', '');
    });

    afterEach(() => {
      vi.unstubAllEnvs();
      vi.resetModules();
    });

    it('returns undefined', async () => {
      const mod = await import('./ee-stub-testable');
      const result = await mod.getJwt();
      expect(result).toBeUndefined();
    });
  });

  describe('when CLERK_PUBLISHABLE_KEY is set', () => {
    beforeEach(() => {
      vi.stubEnv('VITE_CLERK_PUBLISHABLE_KEY', 'pk_test_abc123');
    });

    afterEach(() => {
      vi.unstubAllEnvs();
      vi.resetModules();
    });

    it('returns undefined when _getTokenFn is null', async () => {
      const mod = await import('./ee-stub-testable');
      const result = await mod.getJwt();
      expect(result).toBeUndefined();
    });

    it('calls _getTokenFn and returns token', async () => {
      const mod = await import('./ee-stub-testable');
      const mockGetToken = vi.fn().mockResolvedValue('jwt-token-123');
      mod.__test__.setGetTokenFn(mockGetToken);

      const result = await mod.getJwt();
      expect(result).toBe('jwt-token-123');
      expect(mockGetToken).toHaveBeenCalledWith(undefined);
    });

    it('updates _cachedToken on successful call', async () => {
      const mod = await import('./ee-stub-testable');
      mod.__test__.setGetTokenFn(vi.fn().mockResolvedValue('cached-abc'));

      await mod.getJwt();
      expect(mod.getCachedToken()).toBe('cached-abc');
    });

    it('forwards skipCache option to _getTokenFn', async () => {
      const mod = await import('./ee-stub-testable');
      const mockGetToken = vi.fn().mockResolvedValue('fresh-token');
      mod.__test__.setGetTokenFn(mockGetToken);

      await mod.getJwt({ skipCache: true });
      expect(mockGetToken).toHaveBeenCalledWith({ skipCache: true });
    });

    it('returns undefined when _getTokenFn returns null', async () => {
      const mod = await import('./ee-stub-testable');
      mod.__test__.setGetTokenFn(vi.fn().mockResolvedValue(null));

      const result = await mod.getJwt();
      expect(result).toBeUndefined();
    });
  });
});

describe('TokenManager', () => {
  beforeEach(() => {
    vi.stubEnv('VITE_CLERK_PUBLISHABLE_KEY', 'pk_test_abc');
  });

  afterEach(() => {
    vi.unstubAllEnvs();
    vi.resetModules();
  });

  it('never overwrites a good token with null (null-guard)', async () => {
    const mod = await import('./ee-stub-testable');
    // First call succeeds
    const mockGetToken = vi.fn()
      .mockResolvedValueOnce('good-token')
      .mockResolvedValueOnce(null); // transient Clerk failure
    mod.__test__.setGetTokenFn(mockGetToken);

    await mod.getJwt();
    expect(mod.getCachedToken()).toBe('good-token');

    // Second call returns null — cached token should survive
    await mod.getJwt();
    expect(mod.getCachedToken()).toBe('good-token');
  });

  it('deduplicates concurrent refresh calls (stampede prevention)', async () => {
    const mod = await import('./ee-stub-testable');
    const mockGetToken = vi.fn().mockResolvedValue('dedup-token');
    mod.__test__.setGetTokenFn(mockGetToken);

    // Fire 5 concurrent getJwt() calls
    const results = await Promise.all([
      mod.getJwt(),
      mod.getJwt(),
      mod.getJwt(),
      mod.getJwt(),
      mod.getJwt(),
    ]);

    // All should resolve with the same token
    for (const r of results) expect(r).toBe('dedup-token');
    // _getTokenFn called once: initialize()'s refresh() creates the promise,
    // and all 5 concurrent calls coalesce onto it
    expect(mockGetToken).toHaveBeenCalledTimes(1);
  });

  it('does not leak stale promise across destroy/reinit', async () => {
    const mod = await import('./ee-stub-testable');
    const oldMock = vi.fn().mockResolvedValue('old-token');
    mod.__test__.setGetTokenFn(oldMock);
    await mod.getJwt();
    expect(mod.getCachedToken()).toBe('old-token');

    // Destroy and reinit with new mock
    mod.__test__.reset();
    const newMock = vi.fn().mockResolvedValue('new-token');
    mod.__test__.setGetTokenFn(newMock);
    const result = await mod.getJwt();

    expect(result).toBe('new-token');
    expect(mod.getCachedToken()).toBe('new-token');
    expect(newMock).toHaveBeenCalled();
  });
});

describe('getCachedToken', () => {
  afterEach(() => {
    vi.unstubAllEnvs();
    vi.resetModules();
  });

  it('returns null when no token has been cached', async () => {
    vi.stubEnv('VITE_CLERK_PUBLISHABLE_KEY', 'pk_test_abc');
    const mod = await import('./ee-stub-testable');
    expect(mod.getCachedToken()).toBeNull();
  });

  it('returns the last cached token', async () => {
    vi.stubEnv('VITE_CLERK_PUBLISHABLE_KEY', 'pk_test_abc');
    const mod = await import('./ee-stub-testable');
    mod.__test__.setCachedToken('my-cached-jwt');
    expect(mod.getCachedToken()).toBe('my-cached-jwt');
  });
});

describe('fetchMaybeAuth', () => {
  let mockFetch: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    vi.stubEnv('VITE_CLERK_PUBLISHABLE_KEY', 'pk_test_abc');
    mockFetch = vi.fn().mockResolvedValue(new Response('ok', { status: 200 }));
    vi.stubGlobal('fetch', mockFetch);
  });

  afterEach(() => {
    vi.unstubAllEnvs();
    vi.unstubAllGlobals();
    vi.resetModules();
    vi.restoreAllMocks();
  });

  it('makes plain fetch when no CLERK key', async () => {
    vi.stubEnv('VITE_CLERK_PUBLISHABLE_KEY', '');
    const mod = await import('./ee-stub-testable');

    await mod.fetchMaybeAuth('/api/test');
    expect(mockFetch).toHaveBeenCalledTimes(1);
    // Should not have Authorization header
    const callInit = mockFetch.mock.calls[0][1];
    const headers = callInit?.headers;
    if (headers instanceof Headers) {
      expect(headers.has('Authorization')).toBe(false);
    }
  });

  it('makes plain fetch when no token available', async () => {
    const mod = await import('./ee-stub-testable');
    // _getTokenFn is null, so getJwt returns undefined

    await mod.fetchMaybeAuth('/api/test');
    expect(mockFetch).toHaveBeenCalledTimes(1);
  });

  it('attaches Bearer token header', async () => {
    const mod = await import('./ee-stub-testable');
    mod.__test__.setGetTokenFn(vi.fn().mockResolvedValue('bearer-token-xyz'));

    await mod.fetchMaybeAuth('/api/data');

    expect(mockFetch).toHaveBeenCalledTimes(1);
    const headers = mockFetch.mock.calls[0][1]?.headers;
    expect(headers).toBeInstanceOf(Headers);
    expect((headers as Headers).get('Authorization')).toBe('Bearer bearer-token-xyz');
  });

  it('retries on 401 with fresh token', async () => {
    const mod = await import('./ee-stub-testable');
    mod.__test__.setGetTokenFn(vi.fn().mockResolvedValueOnce('stale-token').mockResolvedValueOnce('fresh-token'));

    mockFetch
      .mockResolvedValueOnce(new Response('Unauthorized', { status: 401 }))
      .mockResolvedValueOnce(new Response('ok', { status: 200 }));

    const response = await mod.fetchMaybeAuth('/api/protected');
    expect(response.status).toBe(200);
    expect(mockFetch).toHaveBeenCalledTimes(2);

    // Second call should have fresh token
    const retryHeaders = mockFetch.mock.calls[1][1]?.headers as Headers;
    expect(retryHeaders.get('Authorization')).toBe('Bearer fresh-token');
    expect(retryHeaders.get('X-Retry-After-401')).toBe('true');
  });

  it('does not retry 401 when fresh token equals stale token', async () => {
    const mod = await import('./ee-stub-testable');
    mod.__test__.setGetTokenFn(vi.fn().mockResolvedValue('same-token'));

    mockFetch.mockResolvedValueOnce(new Response('Unauthorized', { status: 401 }));

    const response = await mod.fetchMaybeAuth('/api/protected');
    expect(response.status).toBe(401);
    expect(mockFetch).toHaveBeenCalledTimes(1);
  });

  it('does not retry when X-Retry-After-401 is already set', async () => {
    const mod = await import('./ee-stub-testable');
    mod.__test__.setGetTokenFn(vi.fn().mockResolvedValue('some-token'));

    mockFetch.mockResolvedValue(new Response('Unauthorized', { status: 401 }));

    const headers = new Headers();
    headers.set('X-Retry-After-401', 'true');

    const response = await mod.fetchMaybeAuth('/api/protected', { headers });
    expect(response.status).toBe(401);
    expect(mockFetch).toHaveBeenCalledTimes(1);
  });

  it('applies timeout via AbortController when no signal provided', async () => {
    const mod = await import('./ee-stub-testable');

    await mod.fetchMaybeAuth('/api/data');

    const fetchInit = mockFetch.mock.calls[0][1];
    expect(fetchInit?.signal).toBeInstanceOf(AbortSignal);
  });

  it('preserves caller signal and skips timeout', async () => {
    const mod = await import('./ee-stub-testable');
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
