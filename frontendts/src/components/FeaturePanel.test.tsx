import { afterEach, describe, expect, it, vi } from 'vitest';
import { FeaturePanel } from '@/components/FeaturePanel';
import { byButton, byText, click, findText, mount, unmountAll, unnamedControls } from '@/test/dom';

afterEach(unmountAll);

const nothing = vi.fn();

// Roger, 2026-11-23: the table of attributes and values under a tapped hexagon looked immature.
describe('what was tapped on the map', () => {
  it('leads a satellite hexagon with its reading and keeps the rest small under it', async () => {
    const properties = {
      plants: 'Cropland or moderate vegetation',
      ndvi: 0.52,
      weakest: 0.31,
      seen_on: '19 Nov 2026',
      older: false,
      h3_resolution: 10,
    };
    mount(<FeaturePanel layerName="Plants (NDVI) - Cyampirita" properties={properties} onZoom={nothing} onClose={nothing} />);
    await findText('0.52');
    expect(byText('NDVI')).not.toBeNull();
    expect(byText('Cropland or moderate vegetation')).not.toBeNull();
    expect(byText(/seen 19 Nov 2026/)).not.toBeNull();
    expect(byText('Its weakest square')).not.toBeNull();
    expect(byText('about 1.5 ha')).not.toBeNull();
    // no table of attributes: the reading is not listed again, and nothing is headed "Attribute" or "Value"
    expect(document.querySelector('table')).toBeNull();
    expect(byText('NDVI (greenness, -1 to 1)')).toBeNull();
    expect(byText('Attribute')).toBeNull();
    expect(byText('Plants (NDVI) - Cyampirita')).not.toBeNull();
  });

  it('reads soil moisture as a whole figure for its ten days', async () => {
    const properties = { moisture: 67.4, band: '65-75%', period: '1-10 Nov 2026', h3_resolution: 9, note: "FAO's first estimate" };
    mount(<FeaturePanel layerName="Soil moisture - Cyampirita" properties={properties} onZoom={nothing} onClose={nothing} />);
    await findText('67%');
    expect(byText('Soil moisture')).not.toBeNull();
    expect(byText('1-10 Nov 2026')).not.toBeNull();
    expect(byText("FAO's first estimate")).not.toBeNull();
    expect(byText('65-75%')).toBeNull(); // the blue it was once drawn in
  });

  it('reads anything else as a short list, shows a camera point its photo, and names its two buttons', async () => {
    const onZoom = vi.fn();
    const onClose = vi.fn();
    mount(<FeaturePanel properties={{ name: 'Plot 3', crop: 'Maize' }} photoUrl="/photo.jpg" onZoom={onZoom} onClose={onClose} />);
    await findText('On the map'); // the layer is no longer on the map
    expect(byText('Plot 3')).not.toBeNull();
    expect(byText('Crop')).not.toBeNull();
    expect(document.querySelector('img[alt="The photo taken here"]')).not.toBeNull();
    click(byButton('Zoom to it') as HTMLButtonElement);
    click(byButton('Close') as HTMLButtonElement);
    expect(onZoom).toHaveBeenCalledTimes(1);
    expect(onClose).toHaveBeenCalledTimes(1);
    expect(unnamedControls()).toEqual([]);
  });
});
