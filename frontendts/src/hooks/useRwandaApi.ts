import { apiFetch } from '@mundi/ee';
import { useMutation, useQuery } from '@tanstack/react-query';

const API_BASE = '/api/rwanda';

export interface DistrictSummary {
  district: string;
  parcel_count: number;
  avg_ndvi: number;
  ndvi_trend?: string;
  last_updated?: string;
}

export interface NdviTimeseriesPoint {
  date: string;
  mean_ndvi: number;
  h3_index?: string;
  district?: string;
}

export interface NdviTimeseriesResponse {
  timeseries: NdviTimeseriesPoint[];
  summary?: {
    min: number;
    max: number;
    mean: number;
  };
}

export interface H3GridResponse {
  type: 'FeatureCollection';
  features: Array<{
    type: 'Feature';
    geometry: {
      type: 'Polygon';
      coordinates: number[][][];
    };
    properties: {
      h3_index: string;
      mean_ndvi?: number;
      [key: string]: unknown;
    };
  }>;
}

export interface MLStatusResponse {
  ml_ready: boolean;
  model_version?: string;
  last_trained?: string;
}

export interface YieldRiskResponse {
  risk_level: 'low' | 'medium' | 'high';
  confidence: number;
  predicted_yield?: number;
  recommendations?: string[];
}

export function useDistrictSummary(district?: string) {
  return useQuery({
    queryKey: ['rwanda', 'summary', district],
    queryFn: async (): Promise<DistrictSummary> => {
      const url = district ? `${API_BASE}/summary/${encodeURIComponent(district)}` : `${API_BASE}/tables`;
      const res = await apiFetch(url);
      if (!res.ok) throw new Error('Failed to fetch district summary');
      return res.json();
    },
    enabled: !!district,
  });
}

export function useNdviTimeseries(params?: { district?: string; h3_index?: string; start_date?: string; end_date?: string }) {
  return useQuery<NdviTimeseriesResponse>({
    queryKey: ['rwanda', 'ndvi', params],
    queryFn: async () => {
      const searchParams = new URLSearchParams();
      if (params?.district) searchParams.set('district', params.district);
      if (params?.h3_index) searchParams.set('h3_index', params.h3_index);
      if (params?.start_date) searchParams.set('start_date', params.start_date);
      if (params?.end_date) searchParams.set('end_date', params.end_date);
      const res = await apiFetch(`${API_BASE}/ndvi/timeseries?${searchParams}`);
      if (!res.ok) throw new Error('Failed to fetch NDVI timeseries');
      return res.json();
    },
    enabled: !!params && (!!params.district || !!params.h3_index),
  });
}

export function useH3Grid(resolution = 7, bounds?: string) {
  return useQuery<H3GridResponse>({
    queryKey: ['rwanda', 'h3grid', resolution, bounds],
    queryFn: async () => {
      const searchParams = new URLSearchParams({ resolution: String(resolution) });
      if (bounds) searchParams.set('bounds', bounds);
      else searchParams.set('bounds', '28.86,-2.84,30.90,-1.04'); // Rwanda default bounds
      const res = await apiFetch(`${API_BASE}/grid/h3?${searchParams}`);
      if (!res.ok) throw new Error('Failed to fetch H3 grid');
      return res.json();
    },
  });
}

export function useMLStatus() {
  return useQuery<MLStatusResponse>({
    queryKey: ['rwanda', 'ml', 'status'],
    queryFn: async () => {
      const res = await apiFetch(`${API_BASE}/ml/status`);
      if (!res.ok) throw new Error('Failed to fetch ML status');
      return res.json();
    },
    refetchInterval: 30000, // Refresh every 30 seconds
  });
}

export function useYieldRiskPrediction() {
  return useMutation<YieldRiskResponse, Error, Array<{ mean_ndvi: number; date: string }>>({
    mutationFn: async (ndviTimeseries: Array<{ mean_ndvi: number; date: string }>) => {
      const res = await apiFetch(`${API_BASE}/ml/yield-risk`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ ndvi_timeseries: ndviTimeseries }),
      });
      if (!res.ok) throw new Error('Failed to predict yield risk');
      return res.json();
    },
  });
}

export function useNdviClassification() {
  return useMutation<{ classification: string; confidence: number }, Error, Array<{ mean_ndvi: number; date: string }>>({
    mutationFn: async (ndviTimeseries: Array<{ mean_ndvi: number; date: string }>) => {
      const res = await apiFetch(`${API_BASE}/ml/classify`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ ndvi_timeseries: ndviTimeseries }),
      });
      if (!res.ok) throw new Error('Failed to classify NDVI');
      return res.json();
    },
  });
}

// Vector tile integration hooks
export interface VectorTileLayerInfo {
  available: boolean;
  url: string | null;
  size_bytes?: number;
  last_modified?: string;
  format?: string;
  layer_name?: string;
  properties?: string[];
  zoom_range?: { min: number; max: number };
  message?: string;
}

export interface VectorTilesStatusResponse {
  vector_tiles_enabled: boolean;
  layers: Record<string, VectorTileLayerInfo>;
}

export function useVectorTilesStatus() {
  return useQuery<VectorTilesStatusResponse>({
    queryKey: ['rwanda', 'vector-tiles', 'status'],
    queryFn: async () => {
      const res = await apiFetch(`${API_BASE}/tiles/status`);
      if (!res.ok) throw new Error('Failed to fetch vector tiles status');
      return res.json();
    },
    refetchInterval: 60000, // Refresh every minute
    staleTime: 30000,
  });
}

// Superset integration hooks
export function useSupersetStatus() {
  return useQuery({
    queryKey: ['superset-status'],
    queryFn: async () => {
      const res = await apiFetch('/api/rwanda/superset/status');
      if (!res.ok) throw new Error('Failed to check Superset status');
      return res.json() as Promise<{ available: boolean; url: string }>;
    },
    refetchInterval: 30000, // Check every 30s
  });
}

export function useSupersetDashboards() {
  return useQuery({
    queryKey: ['superset-dashboards'],
    queryFn: async () => {
      const res = await apiFetch('/api/rwanda/superset/dashboards');
      if (!res.ok) throw new Error('Failed to fetch dashboards');
      return res.json() as Promise<{ dashboards: Array<{ id: string; title: string; url: string; status: string }> }>;
    },
    enabled: false, // Only fetch when explicitly enabled
  });
}

export function useSupersetGuestToken() {
  return useMutation({
    mutationFn: async (dashboardId: string) => {
      const res = await apiFetch('/api/rwanda/superset/guest-token', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ dashboard_id: dashboardId }),
      });
      if (!res.ok) throw new Error('Failed to get guest token');
      return res.json() as Promise<{ token: string }>;
    },
  });
}
