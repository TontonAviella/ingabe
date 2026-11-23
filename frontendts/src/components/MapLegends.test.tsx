import type { Map as MLMap } from 'maplibre-gl';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { type LayerLegend, MapLegends } from '@/components/MapLegends';
import { byText, click, findButton, findText, mount, unmountAll, unnamedControls } from '@/test/dom';

afterEach(() => {
  unmountAll();
  window.innerWidth = 1024;
});

/** A map whose layers declare these legends (the last one hidden when `hideLast`). */
function mapWith(legends: LayerLegend[], hideLast = false): MLMap {
  const layers = legends.map((legend, i) => ({ id: `layer-${i}`, metadata: { 'mundi:legend': legend } }));
  return {
    getStyle: () => ({ layers }),
    getLayoutProperty: (id: string) => (hideLast && id === `layer-${legends.length - 1}` ? 'none' : 'visible'),
    isStyleLoaded: () => true,
    on: vi.fn(),
    off: vi.fn(),
  } as unknown as MLMap;
}

const SHADES = ['#d73027', '#f46d43', '#fdae61', '#d9ef8b', '#a6d96a', '#66bd63', '#1a9850', '#006837'];
const RANGES = [
  'Below 0.1: bare soil',
  '0.1-0.2: sparse vegetation',
  '0.2-0.3',
  '0.3-0.4: cropland',
  '0.4-0.5',
  '0.5-0.6',
  '0.6-0.7',
  '0.7 and above',
];
const PLANTS: LayerLegend = {
  title: 'Plants from the satellite (NDVI), seen 19 Nov 2026',
  items: SHADES.map((color, i) => ({ label: RANGES[i], color })),
  name: 'Plants · NDVI',
  when: '19 Nov 2026',
  scale: {
    colors: SHADES,
    low: 0,
    high: 0.8,
    low_word: 'bare',
    high_word: 'dense',
    ticks: [
      { at: 0.3, label: '0.3' },
      { at: 0.6, label: '0.6' },
    ],
    mark: { at: 0.46, label: '0.46', says: 'The middle square of this photo reads 0.46.' },
  },
  notes: [{ label: 'Grey: an older look', color: '#9ca3af' }],
};
const SOIL: LayerLegend = {
  title: 'Soil moisture, 1-10 Nov 2026 (FAO estimate from 100 m readings), first estimate',
  items: [{ label: '67%, 62-76% across the photo', color: '#a9d6ff' }],
  name: 'Soil moisture',
  when: '1-10 Nov 2026',
  value: '67%',
  value_note: '62-76% across the photo',
  gauge: {
    low: 40,
    high: 75,
    from: 62,
    to: 76,
    low_label: '40%',
    high_label: '75%',
    caption: 'over the year before',
    says: 'Near the top of its own year.',
  },
  notes: [{ label: "FAO's first estimate, 100 m squares" }],
};
const RISK: LayerLegend = {
  title: 'Flood risk',
  items: [
    { label: 'Low', color: '#2c7bb6' },
    { label: 'High', range: 'over 0.7', color: '#d7191c' },
  ],
};

const percent = (el: Element | null, side: 'left' | 'width') => Number.parseFloat((el as HTMLElement).style[side]);

// Roger, 2026-11-23: the key was a white box with two lists of thirteen swatches; it must be professional and
// minimal, and say what it has to say in a creative way.
describe("the map's key", () => {
  it('shows a scale as one strip with the picture own value marked, not as a list of swatches', async () => {
    mount(<MapLegends map={mapWith([PLANTS])} />);
    await findText('Plants · NDVI');
    expect(byText('19 Nov 2026')).not.toBeNull();
    const section = document.querySelector('section[aria-label="Plants from the satellite (NDVI), seen 19 Nov 2026"]') as HTMLElement;
    // every shade is on the strip, and says its range to a pointer held over it
    const strip = [...section.querySelectorAll<HTMLElement>('span[title]')].filter((el) => RANGES.includes(el.title));
    expect(strip.map((el) => el.title)).toEqual(RANGES);
    expect(byText('0.2-0.3')).toBeNull(); // no row for each shade
    // the photo's middle square, 0.46 of the way from 0 to 0.8
    const mark = byText('0.46') as HTMLElement;
    expect(percent(mark, 'left')).toBeCloseTo(57.5, 1);
    expect(mark.title).toBe('The middle square of this photo reads 0.46.');
    expect(byText('bare')).not.toBeNull();
    expect(byText('dense')).not.toBeNull();
    expect(percent(byText('0.3'), 'left')).toBeCloseTo(37.5, 1);
    expect(byText('Grey: an older look')).not.toBeNull();
  });

  it('shows one reading as a figure and a gauge of where it sits in its range', async () => {
    mount(<MapLegends map={mapWith([PLANTS, SOIL])} />);
    await findText('Soil moisture');
    expect(byText('67%')).not.toBeNull();
    expect(byText('62-76% across the photo')).not.toBeNull();
    const gauge = document.querySelector('[title="Near the top of its own year."]') as HTMLElement;
    const band = gauge.querySelector('span.absolute') as HTMLElement;
    // 62% is 62.9% of the way from 40% to 75%; 76% is past the end, so the band ends at the end
    expect(percent(band, 'left')).toBeCloseTo(62.86, 1);
    expect(percent(band, 'left') + percent(band, 'width')).toBeCloseTo(100, 1);
    expect(band.style.backgroundColor).toBe('rgb(169, 214, 255)'); // the ink the map draws it in
    expect(byText('40%')).not.toBeNull();
    expect(byText('75%')).not.toBeNull();
    expect(byText('over the year before')).not.toBeNull();
    expect(byText("FAO's first estimate, 100 m squares")).not.toBeNull();
  });

  it('keeps a short list for a layer that only names its colours, and leaves out a hidden layer', async () => {
    mount(<MapLegends map={mapWith([RISK, SOIL], true)} />);
    await findText('Flood risk');
    expect(byText('Low')).not.toBeNull();
    expect(byText('High (over 0.7)')).not.toBeNull();
    expect(byText('Soil moisture')).toBeNull();
  });

  it('can be put away and brought back, and every control has a name', async () => {
    mount(<MapLegends map={mapWith([PLANTS, SOIL])} />);
    click(await findButton('Hide the legend'));
    click(await findButton('Legend'));
    await findText('Plants · NDVI');
    expect(unnamedControls()).toEqual([]);
  });

  it('keeps the keyboard on the button that takes the place of the one pressed', async () => {
    mount(<MapLegends map={mapWith([PLANTS])} />);
    click(await findButton('Hide the legend'));
    const show = await findButton('Legend');
    await vi.waitFor(() => expect(document.activeElement).toBe(show));
    click(show);
    const hide = await findButton('Hide the legend');
    await vi.waitFor(() => expect(document.activeElement).toBe(hide));
  });

  it('shows two layers with one title as two blocks, and a mark or a band past the end at the end', async () => {
    const other: LayerLegend = { ...PLANTS, scale: { ...PLANTS.scale!, mark: { at: 1.4, label: '0.92', says: 'High.' } } };
    const dry: LayerLegend = { ...SOIL, gauge: { ...SOIL.gauge!, from: 20, to: 30 } };
    mount(<MapLegends map={mapWith([PLANTS, other, dry])} />);
    await findText('0.92');
    expect(document.querySelectorAll('section[aria-label^="Plants from the satellite"]')).toHaveLength(2);
    expect(percent(byText('0.92'), 'left')).toBe(92); // the figure stays inside the card; its tick is at the end
    const band = document.querySelector('[title="Near the top of its own year."] span.absolute') as HTMLElement;
    expect(percent(band, 'left')).toBe(0); // below the year's range: at its start, still wide enough to see
    expect(percent(band, 'width')).toBe(4);
  });

  it('shows a layer that has a reading and no colours to list', async () => {
    mount(<MapLegends map={mapWith([{ ...SOIL, items: [] }])} />);
    await findText('67%');
    expect(byText('Soil moisture')).not.toBeNull();
  });

  it('waits behind its button on a phone, where the map is small', async () => {
    window.innerWidth = 390;
    mount(<MapLegends map={mapWith([PLANTS])} />);
    await findButton('Legend');
    expect(byText('Plants · NDVI')).toBeNull();
  });

  it('shows nothing when no layer on the map has a key', () => {
    const host = mount(<MapLegends map={mapWith([])} />);
    expect(host.textContent).toBe('');
  });
});
