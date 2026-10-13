// A request the server refused, in the server's own words. The panels show `message` as it is: the server
// explains why (a photo still processing, objects too small for the finder), so the words are never rewritten here.

export class ServerError extends Error {
  constructor(
    public status: number,
    message: string,
  ) {
    super(message);
  }
}

/** FastAPI's `detail` as words: a sentence as it is; a list of validation errors as their messages. */
function detailText(detail: unknown): string | null {
  if (typeof detail === 'string' && detail.trim()) return detail;
  if (Array.isArray(detail)) {
    const messages = detail.map((item) => (item && typeof item === 'object' ? (item as { msg?: unknown }).msg : null));
    if (messages.length > 0 && messages.every((m): m is string => typeof m === 'string' && m.trim() !== '')) return messages.join('; ');
  }
  return null;
}

/** The server's own words for a failed request, or its status. */
export async function serverError(res: Response): Promise<ServerError> {
  let detail: string | null = null;
  try {
    detail = detailText((await res.json()).detail);
  } catch {
    /* not JSON: keep the status */
  }
  return new ServerError(res.status, detail ?? `Request failed (${res.status})`);
}
