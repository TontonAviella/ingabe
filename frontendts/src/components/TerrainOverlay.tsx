import type { IControl, Map as MLMap } from 'maplibre-gl';
import { useEffect, useRef, useState } from 'react';

// 3D terrain on the project map: Rwanda's hills under the basemap and every layer (drone photos included),
// switched on and off from a button beside the outline toggle. Elevation tiles come through our own server,
// which keeps each one after the first fetch (/api/basemaps/terrain).

const DEM_SRC = 'rw-terrain-dem';
const STORAGE_KEY = 'mundi.terrain3d';
const EXAGGERATION = 1.5; // Rwanda's hills read better slightly raised
const TILTED = 60;

function readEnabled(): boolean {
  try {
    return localStorage.getItem(STORAGE_KEY) === 'on';
  } catch {
    return false;
  }
}

function writeEnabled(on: boolean) {
  try {
    localStorage.setItem(STORAGE_KEY, on ? 'on' : 'off');
  } catch {
    /* private window: the toggle still works for this session */
  }
}

/** Adds the elevation source and turns terrain on, if a style reload removed them. */
function applyTerrain(map: MLMap) {
  if (!map.getSource(DEM_SRC)) {
    map.addSource(DEM_SRC, {
      type: 'raster-dem',
      tiles: [`${window.location.origin}/api/basemaps/terrain/{z}/{x}/{y}.png`],
      tileSize: 256,
      maxzoom: 15,
      encoding: 'terrarium',
      attribution:
        'Terrain: <a href="https://registry.opendata.aws/terrain-tiles/" target="_blank" rel="noopener">Mapzen Terrain Tiles</a> (SRTM and others)',
    });
  }
  if (map.getTerrain()?.source !== DEM_SRC) map.setTerrain({ source: DEM_SRC, exaggeration: EXAGGERATION });
}

function removeTerrain(map: MLMap) {
  if (map.getTerrain()) map.setTerrain(null);
  if (map.getSource(DEM_SRC)) map.removeSource(DEM_SRC);
}

class TerrainControl implements IControl {
  private _button: HTMLButtonElement | undefined;
  constructor(
    private _on: boolean,
    private _onToggle: () => void,
  ) {}
  onAdd(): HTMLElement {
    const container = document.createElement('div');
    container.className = 'maplibregl-ctrl maplibregl-ctrl-group';
    const button = document.createElement('button');
    button.type = 'button';
    button.style.display = 'flex';
    button.style.alignItems = 'center';
    button.style.justifyContent = 'center';
    button.innerHTML = `<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="#333" stroke-width="2" stroke-linejoin="round" aria-hidden="true">
      <path d="M2 20l7-12 4 6 3-4 6 10z"/><path d="M7.5 10.5l1.5 1.5 1.5-1.5"/></svg>`;
    button.addEventListener('click', () => this._onToggle());
    this._button = button;
    this.setOn(this._on);
    container.appendChild(button);
    return container;
  }
  onRemove(): void {
    this._button?.parentElement?.remove();
  }
  setOn(on: boolean) {
    this._on = on;
    if (!this._button) return;
    const label = on ? 'Flatten the map (turn off 3D terrain)' : 'Show the hills in 3D';
    this._button.title = label;
    this._button.setAttribute('aria-label', label);
    this._button.setAttribute('aria-pressed', String(on));
    this._button.style.background = on ? '#fde68a' : 'transparent';
  }
}

export function TerrainOverlay({ map }: { map: MLMap | null }) {
  const [enabled, setEnabled] = useState(readEnabled);

  // Toggle button beside the outline toggle.
  // biome-ignore lint/correctness/useExhaustiveDependencies: the control keeps its own on/off state after mount
  useEffect(() => {
    if (!map) return;
    const control = new TerrainControl(enabled, () =>
      setEnabled((on) => {
        writeEnabled(!on);
        control.setOn(!on);
        return !on;
      }),
    );
    map.addControl(control, 'top-right');
    return () => {
      try {
        map.removeControl(control);
      } catch {
        /* map already destroyed */
      }
    };
  }, [map]);

  // Tilt in when switched on, level out when switched off; not on first load, so a saved "on" keeps the user's view.
  const firstRun = useRef(true);
  useEffect(() => {
    if (!map) return;
    if (firstRun.current) {
      firstRun.current = false;
      return;
    }
    if (enabled && map.getPitch() < 30) map.easeTo({ pitch: TILTED, duration: 800 });
    if (!enabled && map.getPitch() > 0) map.easeTo({ pitch: 0, duration: 600 });
  }, [map, enabled]);

  // Terrain: set now and again after every style reload (Sage and the basemap switcher call setStyle).
  useEffect(() => {
    if (!map) return;
    if (!enabled) {
      try {
        removeTerrain(map);
      } catch {
        /* style mid-reload: nothing of ours is on it */
      }
      return;
    }
    const apply = () => {
      try {
        applyTerrain(map);
      } catch {
        map.once('idle', apply);
      }
    };
    apply();
    map.on('style.load', apply);
    return () => {
      map.off('style.load', apply);
      map.off('idle', apply);
    };
  }, [map, enabled]);

  return null;
}
