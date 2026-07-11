import { describe, expect, it } from 'vitest';
import { getProjectViewLoadState, shouldRetryProjectQuery } from './projectViewLoadState';

// Regression: ISSUE-002 - deleted projects stayed on Loading forever.
// Found by /qa on 2026-07-09
// Report: .gstack/qa-reports/qa-report-localhost-2026-07-09.md
describe('getProjectViewLoadState', () => {
  it('shows a project error before the missing project loading state', () => {
    expect(getProjectViewLoadState(undefined, 'Mdeleted', new Error('Project not found'), null)).toEqual({
      kind: 'error',
      message: 'Project not found',
    });
  });

  it('shows a map error before the loading state', () => {
    expect(getProjectViewLoadState({}, 'Mbroken', null, new Error('Map not found'))).toEqual({
      kind: 'error',
      message: 'Map not found',
    });
  });

  it('distinguishes loading from ready data', () => {
    expect(getProjectViewLoadState(undefined, null, null, null)).toEqual({ kind: 'loading' });
    expect(getProjectViewLoadState({ id: 'Pready' }, 'Mready', null, null)).toEqual({
      kind: 'ready',
      project: { id: 'Pready' },
      versionId: 'Mready',
    });
  });

  it('does not retry a project that is known to be missing', () => {
    expect(shouldRetryProjectQuery(0, new Error('Project not found'))).toBe(false);
    expect(shouldRetryProjectQuery(0, new Error('Temporary failure'))).toBe(true);
    expect(shouldRetryProjectQuery(3, new Error('Temporary failure'))).toBe(false);
  });
});
