import { apiFetch } from '@mundi/ee';
import { Loader2 } from 'lucide-react';
import React, { useEffect, useState } from 'react';
import { toast } from 'sonner';
import { Button } from '@/components/ui/button';
import { Dialog, DialogContent, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';

// ─── Palette interpolation ──────────────────────────────────────────────────

/** Sequential / diverging anchor palettes (light → dark or diverging). */
const PALETTES: Record<string, string[]> = {
  Blues: ['#f7fbff', '#c6dbef', '#6baed6', '#2171b5', '#08306b'],
  Reds: ['#fff5f0', '#fcbba1', '#fb6a4a', '#cb181d', '#67000d'],
  Greens: ['#f7fcf5', '#c7e9c0', '#74c476', '#238b45', '#00441b'],
  Oranges: ['#fff5eb', '#fdd0a2', '#fd8d3c', '#d94801', '#7f2704'],
  Purples: ['#fcfbfd', '#dadaeb', '#9e9ac8', '#6a51a3', '#3f007d'],
  Viridis: ['#440154', '#3b528b', '#21908d', '#5dc963', '#fde725'],
  Plasma: ['#0d0887', '#6a00a8', '#b12a90', '#e16462', '#fca636', '#f0f921'],
  'RdYlGn (diverging)': ['#a50026', '#f46d43', '#ffffbf', '#74c476', '#1a9850'],
  'RdBu (diverging)': ['#67001f', '#d6604d', '#f7f7f7', '#4393c3', '#053061'],
};

function hexToRgb(hex: string): [number, number, number] {
  const h = hex.replace('#', '');
  return [parseInt(h.slice(0, 2), 16), parseInt(h.slice(2, 4), 16), parseInt(h.slice(4, 6), 16)];
}

function rgbToHex(r: number, g: number, b: number): string {
  return '#' + [r, g, b].map((v) => Math.round(v).toString(16).padStart(2, '0')).join('');
}

function interpolateColors(palette: string[], k: number): string[] {
  if (k === 1) return [palette[Math.floor(palette.length / 2)]];
  return Array.from({ length: k }, (_, i) => {
    const t = i / (k - 1);
    const pos = t * (palette.length - 1);
    const lo = Math.floor(pos);
    const hi = Math.min(lo + 1, palette.length - 1);
    const f = pos - lo;
    const [r1, g1, b1] = hexToRgb(palette[lo]);
    const [r2, g2, b2] = hexToRgb(palette[hi]);
    return rgbToHex(r1 + (r2 - r1) * f, g1 + (g2 - g1) * f, b1 + (b2 - b1) * f);
  });
}

// ─── MapLibre step expression builder ──────────────────────────────────────

/**
 * Build a MapLibre GL `['step', ...]` expression for k classes.
 * breaks has k+1 values: [min, b1, b2, …, bk-1, max]
 * colors has k values, one per class.
 *
 * Result:
 *   ['step', ['get', column],
 *     color0,          // v < b1
 *     b1, color1,      // b1 ≤ v < b2
 *     …
 *     bk-1, colork-1   // v ≥ bk-1
 *   ]
 */
function buildStepExpression(column: string, breaks: number[], colors: string[]): unknown[] {
  const expr: unknown[] = ['step', ['get', column], colors[0]];
  for (let i = 1; i < colors.length; i++) {
    expr.push(breaks[i], colors[i]);
  }
  return expr;
}

// ─── Types ──────────────────────────────────────────────────────────────────

interface ColumnStatsResponse {
  column: string;
  method: string;
  k: number;
  breaks: number[];
  min: number;
  max: number;
}

export interface ChoroplethDialogProps {
  layerId: string;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /** Called when user clicks Apply — passes the MapLibre step expression. */
  onApply: (layerId: string, column: string, expression: unknown[]) => void;
}

// ─── Component ──────────────────────────────────────────────────────────────

export const ChoroplethDialog: React.FC<ChoroplethDialogProps> = ({ layerId, open, onOpenChange, onApply }) => {
  const [column, setColumn] = useState('');
  const [method, setMethod] = useState<'quantile' | 'equal_interval'>('quantile');
  const [k, setK] = useState(5);
  const [palette, setPalette] = useState('Blues');
  const [columns, setColumns] = useState<string[]>([]);
  const [columnsLoading, setColumnsLoading] = useState(false);
  const [loading, setLoading] = useState(false);
  const [stats, setStats] = useState<ColumnStatsResponse | null>(null);
  const [previewColors, setPreviewColors] = useState<string[]>([]);

  useEffect(() => {
    if (!open) return;
    let cancelled = false;
    setColumnsLoading(true);
    setColumns([]);
    setColumn('');
    setStats(null);
    apiFetch(`/api/layer/${layerId}/attributes?limit=1`)
      .then((res) => res.json())
      .then((data) => {
        if (cancelled) return;
        setColumns(data.field_names ?? []);
      })
      .catch(() => {
        if (!cancelled) setColumns([]);
      })
      .finally(() => {
        if (!cancelled) setColumnsLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [open, layerId]);

  const computeBreaks = async () => {
    const col = column.trim();
    if (!col) {
      toast.error('Please enter a column name');
      return;
    }
    setLoading(true);
    setStats(null);
    try {
      const params = new URLSearchParams({ column: col, method, k: String(k) });
      const res = await apiFetch(`/api/layer/${layerId}/column-stats?${params}`);
      if (!res.ok) {
        const err = await res.json().catch(() => ({ detail: res.statusText }));
        throw new Error(err.detail ?? res.statusText);
      }
      const data: ColumnStatsResponse = await res.json();
      const colors = interpolateColors(PALETTES[palette], data.k);
      setStats(data);
      setPreviewColors(colors);
      toast.success(`Computed ${data.k} classes for "${col}"`);
    } catch (e: unknown) {
      toast.error(`Failed to compute breaks: ${e instanceof Error ? e.message : String(e)}`);
    } finally {
      setLoading(false);
    }
  };

  const handleApply = () => {
    if (!stats) return;
    const colors = interpolateColors(PALETTES[palette], stats.k);
    const expr = buildStepExpression(stats.column, stats.breaks, colors);
    onApply(layerId, stats.column, expr);
    toast.success('Choropleth applied');
    onOpenChange(false);
  };

  // Re-compute preview colors when palette changes (without re-fetching breaks)
  const handlePaletteChange = (val: string) => {
    setPalette(val);
    if (stats) {
      setPreviewColors(interpolateColors(PALETTES[val], stats.k));
    }
  };

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-md">
        <DialogHeader>
          <DialogTitle>Choropleth Classification</DialogTitle>
        </DialogHeader>

        <div className="space-y-4 py-2">
          {/* Column */}
          <div className="space-y-1">
            <Label htmlFor="choropleth-column">Numeric column</Label>
            <select
              id="choropleth-column"
              className="w-full rounded-md border border-input bg-background px-3 py-2 text-sm ring-offset-background focus:outline-none focus:ring-2 focus:ring-ring focus:ring-offset-2"
              value={column}
              disabled={columnsLoading}
              onChange={(e) => {
                setColumn(e.target.value);
                setStats(null);
              }}
            >
              {columnsLoading ? (
                <option value="">Loading columns…</option>
              ) : columns.length === 0 ? (
                <option value="">No columns found</option>
              ) : (
                <>
                  <option value="">Select a column</option>
                  {columns.map((col) => (
                    <option key={col} value={col}>
                      {col}
                    </option>
                  ))}
                </>
              )}
            </select>
          </div>

          {/* Method + k side-by-side */}
          <div className="flex gap-3">
            <div className="flex-1 space-y-1">
              <Label>Classification method</Label>
              <select
                className="w-full rounded-md border border-input bg-background px-3 py-2 text-sm ring-offset-background focus:outline-none focus:ring-2 focus:ring-ring focus:ring-offset-2"
                value={method}
                onChange={(e) => {
                  setMethod(e.target.value as 'quantile' | 'equal_interval');
                  setStats(null);
                }}
              >
                <option value="quantile">Quantile</option>
                <option value="equal_interval">Equal interval</option>
              </select>
            </div>

            <div className="w-24 space-y-1">
              <Label htmlFor="choropleth-k">Classes (k)</Label>
              <Input
                id="choropleth-k"
                type="number"
                min={2}
                max={20}
                value={k}
                onChange={(e) => {
                  const val = Math.min(20, Math.max(2, Number(e.target.value) || 5));
                  setK(val);
                  setStats(null);
                }}
              />
            </div>
          </div>

          {/* Color palette */}
          <div className="space-y-1">
            <Label>Color palette</Label>
            <div className="flex items-center gap-2">
              <select
                className="flex-1 rounded-md border border-input bg-background px-3 py-2 text-sm ring-offset-background focus:outline-none focus:ring-2 focus:ring-ring focus:ring-offset-2"
                value={palette}
                onChange={(e) => handlePaletteChange(e.target.value)}
              >
                {Object.keys(PALETTES).map((name) => (
                  <option key={name} value={name}>
                    {name}
                  </option>
                ))}
              </select>
              {/* Live swatch preview */}
              <div className="flex h-6 w-24 flex-shrink-0 overflow-hidden rounded-sm border border-gray-400">
                {PALETTES[palette].map((c, i) => (
                  <div key={i} className="flex-1" style={{ backgroundColor: c }} />
                ))}
              </div>
            </div>
          </div>

          {/* Compute button */}
          <Button variant="secondary" className="w-full" onClick={computeBreaks} disabled={loading || !column.trim()}>
            {loading ? <Loader2 className="mr-2 h-4 w-4 animate-spin" /> : null}
            Compute breaks
          </Button>

          {/* Results preview */}
          {stats && (
            <div className="space-y-2 rounded-md border border-gray-700 p-3">
              <p className="text-xs text-gray-400">
                Range: {stats.min.toFixed(4)} – {stats.max.toFixed(4)}
              </p>
              <div className="space-y-1">
                {previewColors.map((color, i) => {
                  const lo = stats.breaks[i];
                  const hi = stats.breaks[i + 1];
                  return (
                    <div key={i} className="flex items-center gap-2 text-xs">
                      <span
                        className="inline-block h-4 w-6 flex-shrink-0 rounded-sm border border-gray-500"
                        style={{ backgroundColor: color }}
                      />
                      <span className="text-gray-300">
                        {lo.toFixed(4)} – {hi.toFixed(4)}
                      </span>
                    </div>
                  );
                })}
              </div>
            </div>
          )}
        </div>

        <DialogFooter>
          <Button variant="ghost" onClick={() => onOpenChange(false)}>
            Cancel
          </Button>
          <Button onClick={handleApply} disabled={!stats}>
            Apply
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
};
