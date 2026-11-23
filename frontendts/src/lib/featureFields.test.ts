import { describe, expect, it } from 'vitest';
import { featureFieldRows, featureReading } from './featureFields';

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
      ['Its weakest square', '0.12'],
      ['Seen on', '23 Oct 2026'],
      ['Hexagon size', 'about 2,100 m²'],
      ['Note', 'An older look than most of the photo'],
    ]);
  });

  it('reads a soil moisture hexagon with its ten days, from its outline or from its number', () => {
    const facts = { moisture: 68.3, band: '65-75%', period: '1-10 Nov 2026', h3_resolution: 10, note: "FAO's first estimate" };
    const expected = [
      ['Soil moisture (%)', '68.3'],
      ['Ten days', '1-10 Nov 2026'],
      ['Hexagon size', 'about 1.5 ha'],
      ['Note', "FAO's first estimate"],
    ];
    expect(featureFieldRows(facts).map((r) => [r.label, r.value])).toEqual(expected);
    expect(featureFieldRows({ ...facts, reading: '68%' }).map((r) => [r.label, r.value])).toEqual(expected);
  });

  it('still hides enriched columns such as ndvi_mean and soil_ph, and keeps risk labels', () => {
    const rows = featureFieldRows({ ndvi_mean: 0.4, soil_ph: 5.5, risk_score: 70, name: 'Plot 3' });
    expect(rows.map((r) => r.label)).toEqual(['Risk score (0–100)', 'Name']);
  });

  // Roger, 2026-11-23: the table of attributes and values under a tapped hexagon looked immature.
  it('leads a tapped satellite hexagon with its reading, what it means and when it is for', () => {
    const plants = { plants: 'Dense vegetation', ndvi: 0.69, weakest: 0.12, seen_on: '23 Oct 2026', older: true, h3_resolution: 11 };
    const reading = featureReading(plants);
    expect(reading).toEqual({
      value: '0.69',
      label: 'NDVI',
      means: 'Dense vegetation',
      when: 'seen 23 Oct 2026',
      said: ['ndvi', 'plants', 'seen_on'],
    });
    // what the reading says is not said again under it
    const under = featureFieldRows(plants).filter((row) => !reading?.said.includes(row.key));
    expect(under.map((r) => [r.label, r.value])).toEqual([
      ['Its weakest square', '0.12'],
      ['Hexagon size', 'about 2,100 m²'],
    ]);
    expect(featureReading({ moisture: 68.3, band: '65-75%', period: '1-10 Nov 2026', h3_resolution: 10 })).toEqual({
      value: '68%',
      label: 'Soil moisture',
      when: '1-10 Nov 2026',
      said: ['moisture', 'period'],
    });
  });

  it('does not list the flags that only tell the map to draw a hexagon in grey', () => {
    const rows = featureFieldRows({
      ndvi: 0.6,
      seen_on: '19 Nov 2026',
      older: false,
      newer: true,
      note: 'A newer look than most of the photo',
    });
    expect(rows.map((r) => r.label)).toEqual(['NDVI (greenness, -1 to 1)', 'Seen on', 'Note']);
  });

  it('reads anything else as rows: a district, a plot, a layer that only happens to hold a column called ndvi', () => {
    expect(featureReading({ name: 'Plot 3', risk_score: 70 })).toBeNull();
    expect(featureReading({ ndvi: 0.4, district: 'Kayonza' })).toBeNull();
    expect(featureReading({ ndvi: '0.4', plants: 'Dense vegetation', h3_resolution: 10 })).toBeNull();
    // a parcel layer with the same column names is not a hexagon of the satellite view
    expect(featureReading({ ndvi: 0.4, plants: 'Maize', seen_on: '19 Nov 2026' })).toBeNull();
    expect(featureReading({ moisture: 40, period: 'Season A' })).toBeNull();
  });
});
