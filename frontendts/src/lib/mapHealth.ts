// Map health from people's browsers: tells the server whether MapLibre loaded, failed, or lost its graphics and
// whether deck.gl's 3D overlay started. Counted on /metrics as frontend_map_events_total (the "Maps for users"
// alarm). Only these event names are sent; at most one of each per minute per page, so a burst of tile errors
// is one report.

export type MapHealthEvent = 'map_load' | 'map_error' | 'tile_error' | 'deck_ready' | 'deck_error' | 'webgl_lost';

const lastSent = new Map<MapHealthEvent, number>();
const ONCE_PER_MS = 60_000;

export function reportMapHealth(event: MapHealthEvent): void {
  const now = Date.now();
  if (now - (lastSent.get(event) ?? 0) < ONCE_PER_MS) return;
  lastSent.set(event, now);
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
