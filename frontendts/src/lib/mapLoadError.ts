// What to say when a piece of the map does not load.
//
// MapLibre reports every tile that fails, each with its own web address in the message. Shown as they come, a
// weak connection filled the screen with one red card per tile (a phone on a hillside, 2026-11-17). A dropped
// connection is not many errors: it is one thing, said once, and it is over when the map loads again.

/** The one line for a dropped connection. The same words every time, so it is shown once however many tiles fail. */
export const NO_CONNECTION = 'No connection. The map will finish loading when it returns.';

// What browsers say when a request never reached the server: Chrome, Firefox, Safari.
const NEVER_ARRIVED = /failed to fetch|networkerror|load failed|network connection was lost|internet connection appears to be offline/i;

/** true when a map request got no answer at all (the connection, not the server): status 0, or the browser's
 * own words for it. A status from the server (401, 404, 500) is an answer, so it is not this. */
export function isConnectionFailure(status: number | undefined, message: string | undefined): boolean {
  if (status !== undefined && status !== 0) return false;
  return status === 0 || NEVER_ARRIVED.test(message ?? '');
}

/** The line for a tile the server answered with an error. It names the status and not the tile's address, so
 * many failing tiles are one line. */
export function serverFailureMessage(status: number): string {
  return `Part of the map did not load (the server answered ${status}). Move the map to try again.`;
}
