import { describe, expect, it } from 'vitest';
import { inspectionPhotoUrl, isPhotoInspectionLayer } from './inspectionPhoto';

const SHA = 'a'.repeat(64);

describe('inspectionPhotoUrl', () => {
  it('points at the layer photo route for a camera point', () => {
    expect(inspectionPhotoUrl('LabcDEF12345', { sha256: SHA, photo_key: `inspections/P1/LabcDEF12345/${SHA}.jpg` })).toBe(
      `/api/layer/LabcDEF12345/photos/${SHA}`,
    );
  });

  it('gives nothing for ordinary features or a malformed hash', () => {
    expect(inspectionPhotoUrl('LabcDEF12345', { name: 'Field 3' })).toBeNull();
    expect(inspectionPhotoUrl('LabcDEF12345', { sha256: SHA })).toBeNull();
    expect(inspectionPhotoUrl('LabcDEF12345', { sha256: '../x', photo_key: 'k' })).toBeNull();
    expect(inspectionPhotoUrl(undefined, { sha256: SHA, photo_key: 'k' })).toBeNull();
    expect(inspectionPhotoUrl('LabcDEF12345', null)).toBeNull();
  });
});

describe('isPhotoInspectionLayer', () => {
  it('knows the camera points made from a ZIP of photos or one photo', () => {
    expect(isPhotoInspectionLayer({ id: 'L1', name: 'Mast 12', type: 'vector', metadata: { original_format: 'photo_zip' } })).toBe(true);
    expect(isPhotoInspectionLayer({ id: 'L2', name: 'One shot', type: 'vector', metadata: { original_format: 'photo' } })).toBe(true);
  });

  it('leaves out other layers', () => {
    expect(isPhotoInspectionLayer({ id: 'L3', name: 'Sites', type: 'vector', metadata: { original_format: 'geojson' } })).toBe(false);
    expect(isPhotoInspectionLayer({ id: 'L4', name: 'Ortho', type: 'raster', metadata: { original_format: 'photo' } })).toBe(false);
    expect(isPhotoInspectionLayer({ id: 'L5', name: 'Old', type: 'vector' })).toBe(false);
  });
});
