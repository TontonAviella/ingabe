// Readable rows for the selected-feature panel: plain labels instead of raw
// property names, and internal fields hidden.

// 'older' and 'newer' only tell the map to draw a satellite hexagon in grey, and 'reading' is the number it writes on a soil
// moisture hexagon; the hexagon's note and its moisture say both in words. 'band' named the blue a soil moisture
// hexagon was once drawn in; it is drawn in one ink now, and the figure says it.
const HIDDEN_FIELDS = new Set([
  'band',
  'h3_index',
  'screening_model',
  'analysis_goal',
  'domain',
  'has_exposure',
  'color',
  'photo_key',
  'older',
  'newer',
  'reading',
]);

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
  // hexagons of the satellite view (src/services/satellite_view.py)
  ndvi: 'NDVI (greenness, -1 to 1)',
  weakest: 'Its weakest square',
  moisture: 'Soil moisture (%)',
  band: 'Range',
  period: 'Ten days',
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
    // the size a reader can picture; the grid's own number for it means nothing to them
    return H3_AREA[res] ?? String(value);
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

/** What was tapped, when it is one reading: the figure, what it measures, what the figure means and when it is for. */
export interface FeatureReading {
  value: string;
  label: string;
  /** The reading in words ("Cropland or moderate vegetation"). */
  means?: string;
  /** The day or days it is for ("seen 19 Nov 2026", "1-10 Nov 2026"). */
  when?: string;
  /** The properties the reading already says: left out of the rows under it. */
  said: string[];
}

/** The reading a tapped hexagon of the satellite view leads with (src/services/satellite_view.py); null for
 * anything else, which is read as rows. A hexagon is known by its size being there: a district or a plot that
 * only happens to hold columns called ndvi and seen_on keeps all its rows. */
export function featureReading(properties: Record<string, unknown>): FeatureReading | null {
  if (properties.h3_resolution === undefined) return null;
  const text = (key: string) => (typeof properties[key] === 'string' && properties[key] ? (properties[key] as string) : undefined);
  if (typeof properties.ndvi === 'number' && (text('plants') || text('seen_on'))) {
    const seen = text('seen_on');
    return {
      value: properties.ndvi.toFixed(2),
      label: 'NDVI',
      means: text('plants'),
      when: seen ? `seen ${seen}` : undefined,
      said: ['ndvi', 'plants', 'seen_on'],
    };
  }
  if (typeof properties.moisture === 'number' && text('period')) {
    return { value: `${Math.round(properties.moisture)}%`, label: 'Soil moisture', when: text('period'), said: ['moisture', 'period'] };
  }
  return null;
}
