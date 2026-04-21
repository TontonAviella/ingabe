import { ChevronDown, ChevronUp, Eye, EyeOff, Loader2, Plus, Satellite, Trash2, Upload, X } from 'lucide-react';
import type { Map as MLMap } from 'maplibre-gl';
import React, { useState } from 'react';
import type { UploadingFile } from '../lib/frontend-types';
import type { MapLayer, TileLayerUpdate } from '../lib/types';

interface MapLayersPanelProps {
  layers: MapLayer[];
  hiddenLayerIDs: string[];
  toggleLayerVisibility: (layerId: string) => void;
  loadingLayerIDs?: string[];
  openDropzone?: () => void;
  uploadingFiles?: UploadingFile[];
  ephemeralTileLayers?: TileLayerUpdate[];
  onRemoveEphemeralTileLayer?: (sourceId: string) => void;
  onClearAllEphemeralTileLayers?: () => void;
  mapRef?: React.RefObject<MLMap | null>;
}

const MapLayersPanel: React.FC<MapLayersPanelProps> = ({
  layers,
  hiddenLayerIDs,
  toggleLayerVisibility,
  loadingLayerIDs,
  openDropzone,
  uploadingFiles,
  ephemeralTileLayers,
  onRemoveEphemeralTileLayer,
  onClearAllEphemeralTileLayers,
  mapRef,
}) => {
  const [collapsed, setCollapsed] = useState(false);
  const [hiddenEphemeralIDs, setHiddenEphemeralIDs] = useState<Set<string>>(new Set());
  const count = layers.length;
  const tileCount = ephemeralTileLayers?.length ?? 0;
  const activeUploads = uploadingFiles?.filter((f) => f.status === 'uploading') ?? [];

  if (count === 0 && tileCount === 0 && activeUploads.length === 0) return null;

  return (
    <div className="absolute top-4 right-4 bg-white/95 dark:bg-gray-800/95 backdrop-blur-sm rounded-lg shadow-lg min-w-52 max-w-64 z-10">
      <button
        type="button"
        onClick={() => setCollapsed(!collapsed)}
        className="w-full flex items-center justify-between px-3 py-2 text-sm font-medium cursor-pointer hover:bg-gray-50 dark:hover:bg-gray-700 rounded-t-lg"
      >
        <span>Layers ({count + tileCount})</span>
        <div className="flex items-center gap-1">
          {openDropzone && (
            <span
              role="button"
              tabIndex={0}
              onClick={(e) => {
                e.stopPropagation();
                openDropzone();
              }}
              onKeyDown={(e) => {
                if (e.key === 'Enter') {
                  e.stopPropagation();
                  openDropzone();
                }
              }}
              className="p-0.5 rounded hover:bg-gray-200 dark:hover:bg-gray-600"
              title="Upload file"
            >
              <Plus className="h-3.5 w-3.5" />
            </span>
          )}
          {collapsed ? <ChevronDown className="h-4 w-4" /> : <ChevronUp className="h-4 w-4" />}
        </div>
      </button>

      {!collapsed && (
        <>
          {count > 0 && (
            <ul className="border-t border-gray-200 dark:border-gray-700 py-1 max-h-64 overflow-y-auto">
              {layers.map((layer) => {
                const isHidden = hiddenLayerIDs.includes(layer.id);
                const isLoading = loadingLayerIDs?.includes(layer.id);

                return (
                  <li key={layer.id} className="flex items-center gap-2 px-3 py-1.5 hover:bg-gray-50 dark:hover:bg-gray-700 text-sm">
                    <button
                      type="button"
                      onClick={() => toggleLayerVisibility(layer.id)}
                      className="flex-shrink-0 cursor-pointer"
                      aria-label={isHidden ? 'Show layer' : 'Hide layer'}
                    >
                      {isLoading ? (
                        <Loader2 className="h-4 w-4 animate-spin text-gray-400" />
                      ) : isHidden ? (
                        <EyeOff className="h-4 w-4 text-gray-400" />
                      ) : (
                        <Eye className="h-4 w-4 text-gray-600 dark:text-gray-300" />
                      )}
                    </button>
                    <span className={`truncate ${isHidden ? 'text-gray-400 line-through' : ''}`} title={layer.name}>
                      {layer.name}
                    </span>
                  </li>
                );
              })}
            </ul>
          )}

          {tileCount > 0 && (
            <div className="border-t border-gray-200 dark:border-gray-700 py-1">
              <div className="flex items-center justify-between px-3 py-1 text-xs font-medium text-gray-500 dark:text-gray-400 uppercase tracking-wide">
                <span className="flex items-center gap-1">
                  <Satellite className="h-3 w-3" />
                  Satellite Analysis ({tileCount})
                </span>
                {onClearAllEphemeralTileLayers && (
                  <button
                    type="button"
                    onClick={onClearAllEphemeralTileLayers}
                    className="flex items-center gap-0.5 text-gray-400 hover:text-red-500 cursor-pointer transition-colors"
                    aria-label="Clear all analysis layers"
                    title="Clear all"
                  >
                    <Trash2 className="h-3 w-3" />
                  </button>
                )}
              </div>
              {ephemeralTileLayers!.map((tl) => {
                const isHidden = hiddenEphemeralIDs.has(tl.source_id);
                return (
                  <div key={tl.source_id} className="flex items-center gap-2 px-3 py-1.5 hover:bg-gray-50 dark:hover:bg-gray-700 text-sm">
                    <button
                      type="button"
                      onClick={() => {
                        const map = mapRef?.current;
                        if (map?.getLayer(tl.source_id)) {
                          const next = !isHidden;
                          map.setLayoutProperty(tl.source_id, 'visibility', next ? 'none' : 'visible');
                          setHiddenEphemeralIDs((prev) => {
                            const s = new Set(prev);
                            if (next) s.add(tl.source_id);
                            else s.delete(tl.source_id);
                            return s;
                          });
                        }
                      }}
                      className="flex-shrink-0 cursor-pointer"
                      aria-label={isHidden ? 'Show layer' : 'Hide layer'}
                    >
                      {isHidden ? <EyeOff className="h-4 w-4 text-gray-400" /> : <Eye className="h-4 w-4 text-emerald-500" />}
                    </button>
                    <span className={`truncate flex-1 ${isHidden ? 'text-gray-400 line-through' : ''}`} title={tl.name}>
                      {tl.name}
                    </span>
                    {onRemoveEphemeralTileLayer && (
                      <button
                        type="button"
                        onClick={() => onRemoveEphemeralTileLayer(tl.source_id)}
                        className="flex-shrink-0 cursor-pointer p-0.5 rounded hover:bg-gray-200 dark:hover:bg-gray-600"
                        aria-label="Remove layer"
                      >
                        <X className="h-3 w-3 text-gray-400" />
                      </button>
                    )}
                  </div>
                );
              })}
            </div>
          )}

          {activeUploads.length > 0 && (
            <div className="border-t border-gray-200 dark:border-gray-700 px-3 py-2 space-y-2">
              {activeUploads.map((file) => (
                <div key={file.id} className="flex items-center gap-2 text-xs text-gray-500">
                  <Upload className="h-3 w-3 flex-shrink-0" />
                  <span className="truncate flex-1">{file.file.name}</span>
                  <span>{file.progress}%</span>
                </div>
              ))}
            </div>
          )}
        </>
      )}
    </div>
  );
};

export default MapLayersPanel;
