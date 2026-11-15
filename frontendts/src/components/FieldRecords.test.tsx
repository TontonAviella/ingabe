import { afterEach, describe, expect, it, vi } from 'vitest';
import type { FieldRecordRow, KeptOutline } from '@/hooks/useFieldTaps';
import { byButton, byText, click, findButton, findText, mount, unmountAll } from '@/test/dom';
import { FieldRecords } from './FieldRecords';

const mutate = vi.fn();
let rows: FieldRecordRow[] = [];

vi.mock('@/hooks/useFieldTaps', () => ({
  useFieldRecords: () => ({ data: { rows, not_linked: rows.filter((r) => r.field_id === null).length } }),
  useLinkFieldRecord: () => ({ mutate, isPending: false, error: null }),
}));

const field = { id: 'f7', number: 7 } as KeptOutline;
const row = (over: Partial<FieldRecordRow>): FieldRecordRow => ({
  key: 'd:soil:0',
  kind: 'soil',
  label: '7',
  value: 'pH 5.1',
  document: 'Lab',
  said: 'Soil sample 7: pH 5.1 (Lab)',
  field: null,
  field_id: null,
  by: null,
  suggest_id: null,
  suggest_why: null,
  note: null,
  ...over,
});

describe('FieldRecords', () => {
  afterEach(() => {
    mutate.mockClear();
    unmountAll();
  });

  it('shows nothing when the farm has no documents', async () => {
    rows = [];
    const host = mount(<FieldRecords layerId="L1" field={field} />);
    await new Promise((r) => setTimeout(r, 20));
    expect(host.firstChild).toBeNull();
  });

  it('offers a row with the same number and links it only when the reader says so', async () => {
    rows = [row({ suggest_id: 'f7', suggest_why: 'the document uses the same number; that alone does not make it the same land' })];
    mount(<FieldRecords layerId="L1" field={field} />);
    await findButton(/Link a record to this field \(1 not linked/);
    expect(byText(/Soil sample 7: pH 5.1/)).toBeNull(); // not shown as this field's record
    click(byButton(/Link a record to this field \(1 not linked/) as HTMLButtonElement);
    await findText(/May be this field: the document uses the same number/);
    expect(mutate).not.toHaveBeenCalled();
    click(byButton('This is Field 7') as HTMLButtonElement);
    expect(mutate).toHaveBeenCalledWith({ row: 'd:soil:0', field_id: 'f7', linked: true });
  });

  it('shows a linked row with how it was linked, and lets the reader take it off', async () => {
    rows = [row({ field: 7, field_id: 'f7', by: 'document' }), row({ key: 'd:soil:1', field: 8, field_id: 'f8', by: 'person' })];
    mount(<FieldRecords layerId="L1" field={field} />);
    await findText(/Soil sample 7: pH 5.1/);
    expect(byButton(/Link a record/)).toBeNull(); // the other row belongs to another field
    click(byButton('Not this field') as HTMLButtonElement);
    expect(mutate).toHaveBeenCalledWith({ row: 'd:soil:0', field_id: 'f7', linked: false });
  });
});
