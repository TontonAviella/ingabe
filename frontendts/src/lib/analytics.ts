import posthog from 'posthog-js';

type AnalyticsValue = string | number | boolean | null | undefined;
type AnalyticsProperties = Record<string, AnalyticsValue>;

const analyticsEnabled = () => typeof window !== 'undefined' && Boolean(import.meta.env.VITE_POSTHOG_KEY);

const cleanProperties = (properties: AnalyticsProperties): Record<string, string | number | boolean | null> => {
  const cleaned: Record<string, string | number | boolean | null> = {};
  for (const [key, value] of Object.entries(properties)) {
    if (value !== undefined) cleaned[key] = value;
  }
  return cleaned;
};

const getErrorMessage = (error: unknown): string => {
  if (error instanceof Error) return error.message;
  if (typeof error === 'string') return error;
  return '';
};

export const initAnalytics = () => {
  const posthogKey = import.meta.env.VITE_POSTHOG_KEY;
  if (!posthogKey) return;

  posthog.init(posthogKey, {
    api_host: import.meta.env.VITE_POSTHOG_HOST || 'https://us.i.posthog.com',
    autocapture: true,
    capture_pageview: true,
    capture_pageleave: true,
    persistence: 'localStorage+cookie',
  });
};

export const track = (event: string, properties: AnalyticsProperties = {}) => {
  if (!analyticsEnabled()) return;

  try {
    posthog.capture(event, cleanProperties(properties));
  } catch (error) {
    if (import.meta.env.DEV) {
      console.warn('[analytics] capture failed', event, error);
    }
  }
};

export const httpStatusFromError = (error: unknown): number | null => {
  const status = (error as { status?: unknown })?.status;
  if (typeof status === 'number') return status;

  const message = getErrorMessage(error);
  const match = message.match(/\b(?:HTTP|status)\s*(\d{3})\b/i);
  return match ? Number.parseInt(match[1], 10) : null;
};

export const classifyError = (error: unknown): string => {
  const status = httpStatusFromError(error);
  if (status !== null) return `http_${status}`;

  const message = getErrorMessage(error).toLowerCase();
  if (!message) return 'unknown';
  if (message.includes('token') || message.includes('unauthorized') || message.includes('session expired')) return 'auth';
  if (message.includes('timeout') || message.includes('timed out')) return 'timeout';
  if (message.includes('network') || message.includes('failed to fetch')) return 'network';
  if (message.includes('raster')) return 'raster';
  if (message.includes('postgis') || message.includes('.mvt')) return 'vector_tile';
  if (message.includes('s3') || message.includes('upload')) return 'upload';
  return 'application';
};

export const trackError = (event: string, error: unknown, properties: AnalyticsProperties = {}) => {
  const message = getErrorMessage(error);
  track(event, {
    ...properties,
    error_class: classifyError(error),
    http_status: httpStatusFromError(error),
    error_message_length: message.length,
  });
};

export const trackDuration = (event: string, startedAtMs: number, properties: AnalyticsProperties = {}) => {
  track(event, {
    ...properties,
    duration_ms: Math.max(0, Math.round(Date.now() - startedAtMs)),
  });
};

const fileExtension = (name: string): string => {
  const parts = name.split('.');
  if (parts.length < 2) return 'none';
  return parts[parts.length - 1].toLowerCase().slice(0, 24) || 'none';
};

const sizeBucket = (bytes: number): string => {
  const mib = bytes / (1024 * 1024);
  if (mib < 10) return '<10MiB';
  if (mib < 100) return '10-100MiB';
  if (mib < 512) return '100-512MiB';
  if (mib < 1024) return '512MiB-1GiB';
  if (mib < 5 * 1024) return '1-5GiB';
  return '>=5GiB';
};

export const fileAnalytics = (file: File): AnalyticsProperties => ({
  file_extension: fileExtension(file.name),
  file_mime_type: file.type || 'unknown',
  file_size_bytes: file.size,
  file_size_bucket: sizeBucket(file.size),
});
