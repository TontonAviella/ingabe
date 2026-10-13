import { apiFetch } from '@mundi/ee';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { getJson, sendJson } from './apiJson';
import { ServerError } from './serverError';

vi.mock('@mundi/ee', () => ({ apiFetch: vi.fn() }));
const fetchMock = vi.mocked(apiFetch);

afterEach(() => fetchMock.mockReset());

describe('getJson / sendJson', () => {
  it('sends the body as JSON with the method and returns the answer', async () => {
    fetchMock.mockResolvedValue(new Response(JSON.stringify({ job_id: 'a'.repeat(64), status: 'queued' }), { status: 200 }));
    await expect(sendJson('/api/x', 'POST', { part: 'tower_dish' })).resolves.toEqual({ job_id: 'a'.repeat(64), status: 'queued' });
    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe('/api/x');
    expect(init?.method).toBe('POST');
    expect(init?.body).toBe('{"part":"tower_dish"}');
    expect(new Headers(init?.headers).get('Content-Type')).toBe('application/json');
  });

  it('gives nothing back for 204 No Content', async () => {
    fetchMock.mockResolvedValue(new Response(null, { status: 204 }));
    await expect(sendJson('/api/x', 'PUT', {})).resolves.toBeUndefined();
  });

  it("throws the server's words and status when it refuses", async () => {
    fetchMock.mockResolvedValue(
      new Response(JSON.stringify({ detail: 'The finder line is full; try again in a minute.' }), { status: 503 }),
    );
    const error = await getJson('/api/x').catch((e) => e);
    expect(error).toBeInstanceOf(ServerError);
    expect(error).toMatchObject({ status: 503, message: 'The finder line is full; try again in a minute.' });
  });
});
