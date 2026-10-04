// Readable rows for the selected-feature panel: plain labels instead of raw
// property names, and internal fields hidden.

const HIDDEN_FIELDS = new Set(['h3_index', 'screening_model', 'analysis_goal', 'domain', 'has_exposure', 'color']);

// Auto-enriched metric columns added by the (now removed) enrichment API:
// computed values, not original layer attributes.
const ENRICHED_PREFIXES = [
  'soil_',
  'ndvi_',
  'evi_',
  'ndwi_',
  'savi_',
  'ndre_',
  'ndbi_',
  'temp_',
  'rainfall_',
  'wind_',
  'ch4_',
  'n2o_',
  'co2_',
  'cropland_',
  'forest_',
  'built_',
  'rangeland_',
];

const LABELS: Record<string, string> = {
  risk_score: 'Risk score (0–100)',
  risk_level: 'Risk level',
  likely_issue: 'Likely issue',
  recommended_action: 'Recommended action',
  exposure_count: 'Buildings or assets counted here',
  evidence_level: 'Evidence',
  evidence_basis: 'Based on',
  confidence: 'Confidence',
  h3_resolution: 'Hexagon size',
};

// Average H3 cell area by resolution.
const H3_AREA: Record<number, string> = {
  5: 'about 250 km²',
  6: 'about 36 km²',
  7: 'about 5 km²',
  8: 'about 0.7 km²',
  9: 'about 0.1 km² (10 ha)',
  10: 'about 1.5 ha',
  11: 'about 2,100 m²',
  12: 'about 300 m²',
};

function humanize(key: string): string {
  const words = key.replace(/_/g, ' ').trim();
  return words.charAt(0).toUpperCase() + words.slice(1);
}

function formatValue(key: string, value: unknown): string {
  if (key === 'h3_resolution') {
    const res = Number(value);
    return H3_AREA[res] ? `${H3_AREA[res]} (H3 resolution ${res})` : String(value);
  }
  if (key === 'risk_level' && typeof value === 'string') return humanize(value);
  if (typeof value === 'boolean') return value ? 'Yes' : 'No';
  return String(value);
}

export interface FeatureFieldRow {
  key: string;
  label: string;
  value: string;
}

export function featureFieldRows(properties: Record<string, unknown>): FeatureFieldRow[] {
  return Object.entries(properties)
    .filter(([key]) => !HIDDEN_FIELDS.has(key) && !ENRICHED_PREFIXES.some((p) => key.startsWith(p)))
    .map(([key, value]) => ({ key, label: LABELS[key] ?? humanize(key), value: formatValue(key, value) }));
}
