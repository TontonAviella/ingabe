import { describe, expect, it } from 'vitest';
import { inspectionPhotoUrl } from './inspectionPhoto';

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
