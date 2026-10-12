import { apiFetch } from '@mundi/ee';
import { serverError } from '@/lib/serverError';

// JSON requests to the app's own API. A refused request throws a ServerError carrying the server's own words.

export async function getJson<T>(url: string): Promise<T> {
  const res = await apiFetch(url);
  if (!res.ok) throw await serverError(res);
  return res.json();
}

/** Sends `body` as JSON and returns the server's JSON answer (undefined for 204 No Content). */
export async function sendJson<T>(url: string, method: 'POST' | 'PUT', body: unknown): Promise<T> {
  const res = await apiFetch(url, {
    method,
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  if (!res.ok) throw await serverError(res);
  return res.status === 204 ? (undefined as T) : res.json();
}
