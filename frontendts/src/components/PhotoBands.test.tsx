import { afterEach, describe, expect, it } from 'vitest';
import type { FieldFlight } from '@/hooks/useFieldTaps';
import type { PhotoBands } from '@/hooks/usePhotoBands';
import { byButton, byText, click, findButton, findText, mount, unmountAll } from '@/test/dom';
import { FieldFlights } from './FieldFlights';
import { PhotoBandsForm } from './PhotoBands';

const bands = {
  band_count: 5,
  working: false,
  failed: false,
  presets: [{ label: 'Blue, green, red, red edge, near-infrared', order: ['blue', 'green', 'red', 'red_edge', 'nir'] }],
  choices: [{ id: 'other', label: 'Something else' }],
} as unknown as PhotoBands;

afterEach(unmountAll);

describe('a photo whose file does not name its bands', () => {
  it('asks a reader nothing about bands until they say the photo is theirs', async () => {
    mount(<PhotoBandsForm layerId="L1" bands={bands} />);
    await findText('This photo cannot be measured yet');
    expect(byText('Which band is which?')).toBeNull();
    expect(document.body.querySelector('input, select')).toBeNull();
    click(await findButton('I flew or processed this photo'));
    await findText('Which band is which?');
    // nothing is chosen for them, and nothing can be saved until they choose
    expect(byButton('Save the band names')?.disabled).toBe(true);
  });
});

describe('one field across flights', () => {
  const flight = (day_said: string, said: string, own = false): FieldFlight => ({
    day: day_said,
    flown: !day_said.startsWith('added'),
    day_said,
    photo: `${day_said}.tif`,
    this: own,
    measured: true,
    said,
  });

  it('sets the flights side by side in the server words, this photo marked', async () => {
    mount(
      <FieldFlights
        layerId="L1"
        flownOn="2026-04-02"
        note="No verdict."
        flights={[flight('14 March 2026', 'health index (NDVI) 0.45'), flight('2 April 2026', 'health index (NDVI) 0.71', true)]}
      />,
    );
    await findText('No verdict.');
    const lines = [...document.body.querySelectorAll('li')].map((li) => li.textContent);
    expect(lines).toEqual(['14 March 2026: health index (NDVI) 0.45', '2 April 2026 (this photo): health index (NDVI) 0.71']);
    expect(byButton('Save')).toBeNull(); // nothing to save until the day is changed
  });

  it('says a photo with no flight day goes by the day it was added', async () => {
    mount(<FieldFlights layerId="L1" flownOn={null} flights={[]} />);
    await findText(/goes by the day it was added/);
    expect(document.body.querySelector('ul')).toBeNull();
    expect(document.body.querySelector('input[type="date"]')).not.toBeNull();
  });
});
