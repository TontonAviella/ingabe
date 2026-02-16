// Copyright (C) 2025 Ingabe Ltd.
//
// This program is free software: you can redistribute it and/or modify
// it under the terms of the GNU Affero General Public License as published by
// the Free Software Foundation, either version 3 of the License, or
// (at your option) any later version.

import type { Map as MLMap } from 'maplibre-gl';

type ReplayFn = () => void;
type CleanupFn = () => void;

/**
 * StyleBridge protects imperatively-added map sources/layers from being
 * wiped by atomic `map.setStyle()` calls (used by mundi.ai's server-driven
 * LLM symbology pipeline).
 *
 * Controls that add sources/layers (MeasureControl, InspectControl, etc.)
 * register a replay callback. After every `style.load` event (which fires
 * after setStyle), all registered callbacks are invoked so controls can
 * re-add their sources and layers.
 */
export class StyleBridge {
  private _map: MLMap;
  private _replayers = new Set<ReplayFn>();
  private _bound: () => void;

  constructor(map: MLMap) {
    this._map = map;
    this._bound = this._onStyleLoad.bind(this);
    map.on('style.load', this._bound);
  }

  /**
   * Register a callback that re-adds sources/layers after setStyle().
   * The callback should check `map.getSource(id)` before adding to
   * avoid duplicates. Returns an unsubscribe function.
   */
  onStyleReset(replay: ReplayFn): CleanupFn {
    this._replayers.add(replay);
    return () => {
      this._replayers.delete(replay);
    };
  }

  private _onStyleLoad() {
    for (const replay of this._replayers) {
      try {
        replay();
      } catch (e) {
        console.warn('[StyleBridge] replay callback failed:', e);
      }
    }
  }

  destroy() {
    try {
      this._map.off('style.load', this._bound);
    } catch (_) {
      // Map may already be destroyed
    }
    this._replayers.clear();
  }
}
