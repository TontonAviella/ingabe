export type ProjectViewLoadState<T> =
  | { kind: 'error'; message: string }
  | { kind: 'loading' }
  | { kind: 'ready'; project: T; versionId: string };

export function shouldRetryProjectQuery(failureCount: number, error: unknown): boolean {
  if (error instanceof Error && error.message === 'Project not found') return false;
  return failureCount < 3;
}

export function getProjectViewLoadState<T>(
  project: T | undefined,
  versionId: string | null,
  projectError: unknown,
  mapError: unknown,
): ProjectViewLoadState<T> {
  const error = projectError ?? mapError;
  if (error) {
    return {
      kind: 'error',
      message: error instanceof Error ? error.message : 'Unknown error',
    };
  }
  if (!project || !versionId) return { kind: 'loading' };
  return { kind: 'ready', project, versionId };
}
