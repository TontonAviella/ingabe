import { describe, expect, it } from 'vitest';
import { featureFieldRows } from './featureFields';

describe('featureFieldRows', () => {
  it('reads a satellite plants square in plain words and hides the drawing flag', () => {
    const rows = featureFieldRows({
      plants: 'Dense vegetation',
      ndvi: 0.69,
      seen_on: '19 Nov 2026',
      faded: true,
      note: 'An older look than most of the photo',
    });
    expect(rows.map((r) => [r.label, r.value])).toEqual([
      ['Plants', 'Dense vegetation'],
      ['NDVI (greenness, -1 to 1)', '0.69'],
      ['Seen on', '19 Nov 2026'],
      ['Note', 'An older look than most of the photo'],
    ]);
  });

  it('reads a soil moisture square with its ten days', () => {
    const rows = featureFieldRows({ moisture: 68.3, band: '65-75%', period: '1-10 Nov 2026', note: "FAO's first estimate" });
    expect(rows.map((r) => [r.label, r.value])).toEqual([
      ['Soil moisture (%)', '68.3'],
      ['Range', '65-75%'],
      ['Ten days', '1-10 Nov 2026'],
      ['Note', "FAO's first estimate"],
    ]);
  });

  it('still hides enriched columns such as ndvi_mean and soil_ph, and keeps risk labels', () => {
    const rows = featureFieldRows({ ndvi_mean: 0.4, soil_ph: 5.5, risk_score: 70, name: 'Plot 3' });
    expect(rows.map((r) => r.label)).toEqual(['Risk score (0–100)', 'Name']);
  });
});
