import { apiFetch } from '@mundi/ee';
import { useQuery } from '@tanstack/react-query';

const API_BASE = '/api/rwanda';

export interface DataCoverage {
  source: string;
  measured_every: string;
  value_covers: string;
  note: string;
}

export interface DistrictNdviMap {
  type: 'FeatureCollection';
  features: Array<{
    type: 'Feature';
    geometry: GeoJSON.Geometry;
    properties: {
      district: string;
      mean_ndvi: number | null;
      ndvi_class: string | null;
      ndvi_label: string | null;
      color: string | null;
      week_start: string | null;
      computed_at: string | null;
      // "One value for the whole X district: all N villages in it share it."
      shared_note_sector?: string;
      shared_note_cell?: string;
      shared_note_village?: string;
    };
  }>;
  // null, and no NDVI on the districts, in projects of industries without vegetation analysis (Power Grid, Telecom)
  legend: { title: string; items: Array<{ key: string; label: string; range: string; color: string }> } | null;
  levels: MapLevel[];
  data_coverage: DataCoverage | null;
  vegetation: boolean;
}

export type AdminLevel = 'district' | 'sector' | 'cell' | 'village';

export interface MapLevel {
  level: AdminLevel;
  has_values: boolean;
  values_from: AdminLevel | null;
}

export interface AdminOutlines {
  type: 'FeatureCollection';
  level: AdminLevel;
  truncated: boolean;
  features: Array<{
    type: 'Feature';
    geometry: GeoJSON.Geometry;
    properties: { id: string; name: string; level: AdminLevel; district?: string; sector?: string; cell?: string };
  }>;
}

/** Outlines of the sector/cell/village units in view; bbox is west,south,east,north. */
export function useAdminOutlines(level: AdminLevel, bbox: string | null) {
  return useQuery<AdminOutlines>({
    queryKey: ['rwanda', 'admin-outlines', level, bbox],
    queryFn: async () => {
      const res = await apiFetch(`${API_BASE}/admin/${level}/outlines?bbox=${bbox}`);
      if (!res.ok) throw new Error(`Failed to fetch ${level} outlines`);
      return res.json();
    },
    enabled: level !== 'district' && !!bbox,
    // Keep the last outlines while panning, but never show one level's outlines under another's name.
    placeholderData: (previous) => (previous?.level === level ? previous : undefined),
    staleTime: 60 * 60 * 1000,
  });
}

/** District outlines, with NDVI when the project's industry has vegetation analysis (the server decides). */
export function useDistrictNdviMap(projectId?: string) {
  return useQuery<DistrictNdviMap>({
    queryKey: ['rwanda', 'ndvi', 'districts', projectId ?? null],
    queryFn: async () => {
      const query = projectId ? `?project_id=${encodeURIComponent(projectId)}` : '';
      const res = await apiFetch(`${API_BASE}/ndvi/districts${query}`);
      if (!res.ok) throw new Error('Failed to fetch district NDVI');
      return res.json();
    },
  });
}
