// Map health from people's browsers: tells the server whether MapLibre loaded, failed, or lost its graphics and
// whether deck.gl's 3D overlay started. Counted on /metrics as frontend_map_events_total (the "Maps for users"
// alarms). Only these event names are sent. Each kind of error is sent once per page, so one broken tab left open
// is one report; a load is sent each time a map is made.

export type MapHealthEvent = 'map_load' | 'map_error' | 'tile_error' | 'deck_ready' | 'deck_error' | 'webgl_lost';

const sentOnce = new Set<MapHealthEvent>();
const EVERY_TIME: ReadonlySet<MapHealthEvent> = new Set(['map_load', 'deck_ready']);

export function reportMapHealth(event: MapHealthEvent): void {
  if (!EVERY_TIME.has(event)) {
    if (sentOnce.has(event)) return;
    sentOnce.add(event);
  }
  try {
    void fetch('/api/client-health', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ event }),
      keepalive: true,
    }).catch(() => undefined);
  } catch {
    // Reporting must never disturb the map.
  }
}

// A tile error worth counting is the server's or the network's fault: a 5xx or no answer at all (status 0).
// A 401 (session ended) or 404 (a deleted layer) is not a broken map.
export function isServerTileFailure(status: number | undefined): boolean {
  return status === undefined || status === 0 || status >= 500;
}
