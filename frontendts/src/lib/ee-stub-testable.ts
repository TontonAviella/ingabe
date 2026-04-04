/**
 * Test-only re-export of ee-stub internals.
 * This file is ONLY imported by tests — it exposes setters for the
 * module-scoped _getTokenFn and _cachedToken variables that are
 * otherwise private. This avoids modifying the production code.
 */

// Re-export all public API
export { fetchMaybeAuth, getCachedToken, getJwt } from './ee-stub';

// The module-level variables live in ee-stub.tsx. We need a way to set them
// from tests without going through _SetTokenProvider (which requires React).
// We'll add tiny test-only exports to ee-stub.tsx behind a __test__ namespace.

// Actually, we need to modify ee-stub.tsx slightly to expose test hooks.
// See the __test__ export at the bottom of ee-stub.tsx.
export { __test__ } from './ee-stub';
