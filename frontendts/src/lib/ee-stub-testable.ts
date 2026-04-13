/**
 * Test-only re-export of ee-stub internals.
 * This file is ONLY imported by tests — it exposes setters for the
 * module-scoped _getTokenFn and _cachedToken variables that are
 * otherwise private. This avoids modifying the production code.
 */

// Re-export all public API
export { fetchMaybeAuth, getCachedToken, getJwt } from './ee-stub';

// The module-level variables live in ee-stub.tsx. The __test__ export at the
// bottom of ee-stub.tsx exposes setters so tests can manipulate internal state
// without going through _SetTokenProvider (which requires a full React tree).
export { __test__ } from './ee-stub';
