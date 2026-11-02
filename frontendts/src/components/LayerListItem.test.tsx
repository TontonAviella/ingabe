import { afterEach, describe, expect, it, vi } from 'vitest';
import { LayerListItem } from '@/components/LayerListItem';
import { mount, unmountAll, unnamedControls } from '@/test/dom';

// One row of the map's layer list: every button a screen reader reaches has a name (the options button is an icon
// only), and a drone photo's row carries its name alone, no "Raster" type word or EPSG code.

afterEach(() => unmountAll());

const nameBox = () => document.querySelector<HTMLInputElement>('input[aria-label="Layer name"]');

async function row(props: Partial<React.ComponentProps<typeof LayerListItem>> = {}) {
  mount(
    <ul>
      <li>
        <LayerListItem
          name="Cyampirita_Orthophoto"
          layerId="L1"
          isVisible
          onToggleVisibility={vi.fn()}
          onRename={vi.fn()}
          dropdownActions={{ zoom: { label: 'Zoom to layer', action: vi.fn() } }}
          {...props}
        />
      </li>
    </ul>,
  );
  await vi.waitFor(() => expect(nameBox()?.value).toBe(props.name ?? 'Cyampirita_Orthophoto'));
}

describe('a layer row', () => {
  it('names every control, the options button included', async () => {
    await row();
    expect(document.querySelector('button[aria-label="Options for Cyampirita_Orthophoto"]')).not.toBeNull();
    expect(unnamedControls()).toEqual([]);
  });

  it('shows the name alone for a drone photo', async () => {
    await row({ normalText: undefined });
    expect(document.body.textContent ?? '').not.toMatch(/Raster|EPSG/);
  });

  it("shows a shape layer's count", async () => {
    await row({ name: 'Plots', normalText: '445' });
    expect(document.body.textContent).toContain('445');
  });
});
