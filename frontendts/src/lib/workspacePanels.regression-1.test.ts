import { describe, expect, it } from 'vitest';
import { toggleMobileWorkspacePanel } from './workspacePanels';

describe('mobile map workspace panels', () => {
  it('opens the requested panel and closes it on a second tap', () => {
    expect(toggleMobileWorkspacePanel(null, 'layers')).toBe('layers');
    expect(toggleMobileWorkspacePanel('layers', 'layers')).toBeNull();
  });

  it('switches between layers and history instead of stacking both panels', () => {
    expect(toggleMobileWorkspacePanel('layers', 'history')).toBe('history');
    expect(toggleMobileWorkspacePanel('history', 'layers')).toBe('layers');
  });
});
