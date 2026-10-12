import { describe, expect, it } from 'vitest';
import { serverError } from './serverError';

function json(status: number, body: unknown) {
  return new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } });
}

describe('serverError', () => {
  it("keeps the server's sentence word for word", async () => {
    const reason = 'The marked dishes are 18 px across as the finder reads this photo; it was measured on dishes of 31 px or more.';
    const error = await serverError(json(422, { detail: reason }));
    expect(error.status).toBe(422);
    expect(error.message).toBe(reason);
  });

  it("joins FastAPI's validation messages instead of showing [object Object]", async () => {
    const error = await serverError(
      json(422, {
        detail: [
          { loc: ['body', 'part'], msg: "Input should be 'tower_dish', 'tower_antenna' or 'tower_radio_unit'", type: 'enum' },
          { loc: ['body', 'marks'], msg: 'Field required', type: 'missing' },
        ],
      }),
    );
    expect(error.message).toBe("Input should be 'tower_dish', 'tower_antenna' or 'tower_radio_unit'; Field required");
  });

  it('falls back to the status when the server gave no words', async () => {
    expect((await serverError(new Response('<html>Bad gateway</html>', { status: 502 }))).message).toBe('Request failed (502)');
    expect((await serverError(json(500, { detail: { code: 7 } }))).message).toBe('Request failed (500)');
    expect((await serverError(json(404, {}))).message).toBe('Request failed (404)');
  });
});
