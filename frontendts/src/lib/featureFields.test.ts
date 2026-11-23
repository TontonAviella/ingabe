import { describe, expect, it } from 'vitest';
import { featureFieldRows } from './featureFields';

describe('featureFieldRows', () => {
  it('reads a satellite plants hexagon in plain words and hides the drawing flag', () => {
    const rows = featureFieldRows({
      plants: 'Dense vegetation',
      ndvi: 0.69,
      weakest: 0.12,
      seen_on: '23 Oct 2026',
      older: true,
      h3_resolution: 11,
      note: 'An older look than most of the photo',
    });
    expect(rows.map((r) => [r.label, r.value])).toEqual([
      ['Plants', 'Dense vegetation'],
      ['NDVI (greenness, -1 to 1)', '0.69'],
      ['Its weakest square (NDVI)', '0.12'],
      ['Seen on', '23 Oct 2026'],
      ['Hexagon size', 'about 2,100 m² (H3 resolution 11)'],
      ['Note', 'An older look than most of the photo'],
    ]);
  });

  it('reads a soil moisture hexagon with its ten days, from its outline or from its number', () => {
    const facts = { moisture: 68.3, band: '65-75%', period: '1-10 Nov 2026', h3_resolution: 10, note: "FAO's first estimate" };
    const expected = [
      ['Soil moisture (%)', '68.3'],
      ['Range', '65-75%'],
      ['Ten days', '1-10 Nov 2026'],
      ['Hexagon size', 'about 1.5 ha (H3 resolution 10)'],
      ['Note', "FAO's first estimate"],
    ];
    expect(featureFieldRows(facts).map((r) => [r.label, r.value])).toEqual(expected);
    expect(featureFieldRows({ ...facts, reading: '68%' }).map((r) => [r.label, r.value])).toEqual(expected);
  });

  it('still hides enriched columns such as ndvi_mean and soil_ph, and keeps risk labels', () => {
    const rows = featureFieldRows({ ndvi_mean: 0.4, soil_ph: 5.5, risk_score: 70, name: 'Plot 3' });
    expect(rows.map((r) => r.label)).toEqual(['Risk score (0–100)', 'Name']);
  });
});
