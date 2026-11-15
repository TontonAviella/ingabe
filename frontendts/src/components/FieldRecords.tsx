import { useState } from 'react';
import { type FieldRecordRow, type KeptOutline, useFieldRecords, useLinkFieldRecord } from '@/hooks/useFieldTaps';

const small = 'inline-flex min-h-11 cursor-pointer items-center px-2 text-[12.5px] text-fg-soft underline sm:min-h-9';
const button =
  'min-h-11 shrink-0 cursor-pointer rounded-[12px] bg-panel-2 px-3 text-[13px] font-semibold text-fg hover:bg-panel-3 disabled:opacity-50 sm:min-h-9';

/**
 * The rows of the farm's documents (lab samples, harvests) that belong to one kept field, and the rows not linked
 * to any field yet. The server joins a row to a field by the position its document prints or by the reader's word
 * here; a matching name or number is only offered (src/services/record_links.py).
 */
export function FieldRecords({ layerId, field }: { layerId: string; field: KeptOutline }) {
  const records = useFieldRecords(layerId);
  const link = useLinkFieldRecord(layerId);
  const [open, setOpen] = useState(false);
  const rows = records.data?.rows ?? [];
  if (!rows.length) return null;
  const here = rows.filter((r) => r.field_id === field.id);
  // a row the app thinks may be this field comes first; then the rows linked to no field
  const offered = rows
    .filter((r) => r.field_id === null)
    .sort((a, b) => Number(b.suggest_id === field.id) - Number(a.suggest_id === field.id));
  const set = (row: FieldRecordRow, linked: boolean) => link.mutate({ row: row.key, field_id: field.id, linked });

  return (
    <div className="flex flex-col gap-1.5">
      {here.map((row) => (
        <div key={row.key} className="flex items-start justify-between gap-2">
          <p className="m-0 text-[12.5px] text-fg-soft">
            {row.said}
            <span className="text-fg-muted">
              {row.by === 'document' ? ' · linked by the position printed in the document' : ' · linked by a person'}
            </span>
          </p>
          <button type="button" className={small} disabled={link.isPending} onClick={() => set(row, false)}>
            Not this field
          </button>
        </div>
      ))}
      {offered.length > 0 && (
        <button type="button" className={`${small} self-start`} aria-expanded={open} onClick={() => setOpen(!open)}>
          {open ? 'Hide' : 'Link a record to this field'} ({offered.length} not linked to a field yet)
        </button>
      )}
      {open && offered.length > 0 && (
        <ul className="m-0 flex max-h-56 list-none flex-col gap-1.5 overflow-y-auto p-0">
          {offered.map((row) => (
            <li key={row.key} className="flex items-center justify-between gap-2 rounded-[12px] border border-white/10 px-2 py-1.5">
              <span className="text-[12.5px]">
                <span className="font-semibold">
                  {row.kind === 'soil' ? 'Soil sample' : 'Harvest'} {row.label}
                </span>
                <span className="text-fg-soft"> · {row.value}</span>
                {row.document && <span className="text-fg-muted"> · {row.document}</span>}
                {row.suggest_id === field.id && row.suggest_why && (
                  <span className="block text-fg-muted">May be this field: {row.suggest_why}.</span>
                )}
                {row.note && <span className="block text-fg-muted">{row.note}.</span>}
              </span>
              <button type="button" className={button} disabled={link.isPending} onClick={() => set(row, true)}>
                This is Field {field.number}
              </button>
            </li>
          ))}
        </ul>
      )}
      {link.error && (
        <p role="alert" className="m-0 text-[13px] text-caramel-hi">
          {link.error.message}
        </p>
      )}
    </div>
  );
}
