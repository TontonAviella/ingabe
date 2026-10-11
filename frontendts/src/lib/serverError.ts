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

/** The server's own words for a failed request, or its status. */
export async function serverError(res: Response): Promise<ServerError> {
  let detail = `Request failed (${res.status})`;
  try {
    detail = (await res.json()).detail ?? detail;
  } catch {
    /* not JSON: keep the status */
  }
  return new ServerError(res.status, detail);
}
