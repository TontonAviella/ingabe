import { apiFetch } from '@mundi/ee';
import { AlertTriangle, Loader2, Satellite } from 'lucide-react';
import React, { useState } from 'react';
import { useNavigate, useParams } from 'react-router-dom';
import { toast } from 'sonner';
import { Button } from '@/components/ui/button';
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog';
import { Input } from '@/components/ui/input';

const COLLECTIONS = [
  { value: 'sentinel-2-l2a', label: 'Sentinel-2 L2A', description: 'Free, 10m resolution, 5-day revisit — available now' },
  { value: 'planetscope', label: 'PlanetScope', description: '3.7m daily imagery — crop identification & field monitoring (requires BYOC import)' },
  { value: 'skysat', label: 'SkySat', description: '50cm on-demand — detailed field inspection (requires BYOC import)' },
] as const;

const VISUALIZATIONS = [
  { value: 'TRUE-COLOR', label: 'True Color' },
  { value: 'NDVI', label: 'NDVI (Vegetation Index)' },
  { value: 'FALSE-COLOR', label: 'False Color (NIR)' },
  { value: 'NDRE', label: 'NDRE (Chlorophyll)' },
] as const;

interface AddSatelliteLayerProps {
  isOpen: boolean;
  onClose: () => void;
  mapId?: string;
  onSuccess?: () => void;
}

function defaultDateRange(): { from: string; to: string } {
  const now = new Date();
  const to = now.toISOString().slice(0, 10);
  const from = new Date(now.getTime() - 30 * 24 * 60 * 60 * 1000).toISOString().slice(0, 10);
  return { from, to };
}

export const AddSatelliteLayer: React.FC<AddSatelliteLayerProps> = ({ isOpen, onClose, mapId, onSuccess }) => {
  const navigate = useNavigate();
  const { projectId } = useParams<{ projectId: string }>();
  const dates = defaultDateRange();

  const [form, setForm] = useState({
    collection: 'sentinel-2-l2a',
    visualization: 'TRUE-COLOR',
    dateFrom: dates.from,
    dateTo: dates.to,
    maxcc: 20,
    name: '',
  });
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const autoName = () => {
    const coll = COLLECTIONS.find((c) => c.value === form.collection);
    const viz = VISUALIZATIONS.find((v) => v.value === form.visualization);
    const monthFrom = new Date(form.dateFrom).toLocaleDateString('en', { month: 'short', year: 'numeric' });
    const monthTo = new Date(form.dateTo).toLocaleDateString('en', { month: 'short', year: 'numeric' });
    const dateStr = monthFrom === monthTo ? monthFrom : `${monthFrom} - ${monthTo}`;
    return `${coll?.label ?? form.collection} ${viz?.label ?? form.visualization} \u2014 ${dateStr}`;
  };

  const handleSubmit = async () => {
    if (!mapId) {
      toast.error('No map ID available');
      return;
    }

    setLoading(true);
    setError(null);

    const layerName = form.name.trim() || autoName();

    try {
      const response = await apiFetch(`/api/maps/${mapId}/layers/satellite`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          name: layerName,
          collection: form.collection,
          layer: form.visualization,
          date_from: form.dateFrom,
          date_to: form.dateTo,
          maxcc: form.maxcc,
        }),
      });

      if (response.ok) {
        const data = await response.json();
        toast.success('Satellite layer added!');
        handleClose();
        onSuccess?.();

        if (data.dag_child_map_id && projectId) {
          setTimeout(() => {
            navigate(`/project/${projectId}/${data.dag_child_map_id}`);
          }, 1000);
        }
      } else {
        const errorData = await response.json().catch(() => ({ detail: response.statusText }));
        const detail = errorData.detail;
        setError(typeof detail === 'string' ? detail : (detail ? JSON.stringify(detail) : response.statusText));
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Network error occurred');
    } finally {
      setLoading(false);
    }
  };

  const handleClose = () => {
    const dates = defaultDateRange();
    setForm({
      collection: 'sentinel-2-l2a',
      visualization: 'TRUE-COLOR',
      dateFrom: dates.from,
      dateTo: dates.to,
      maxcc: 20,
      name: '',
    });
    setError(null);
    onClose();
  };

  return (
    <Dialog
      open={isOpen}
      onOpenChange={(open) => {
        if (!open) handleClose();
      }}
    >
      <DialogContent className="sm:max-w-[520px]">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2">
            <Satellite className="h-5 w-5" />
            Add Satellite Imagery
          </DialogTitle>
          <DialogDescription>Add live satellite imagery. Sentinel-2 provides free 10m global coverage. PlanetScope (3.7m daily) available after BYOC import.</DialogDescription>
        </DialogHeader>

        <div className="grid gap-4 py-4">
          {/* Collection selector */}
          <div className="space-y-2">
            <label className="text-sm font-medium">Collection</label>
            <div className="grid grid-cols-3 gap-2">
              {COLLECTIONS.map((c) => (
                <Button
                  key={c.value}
                  type="button"
                  variant={form.collection === c.value ? 'default' : 'outline'}
                  size="sm"
                  onClick={() => setForm((prev) => ({ ...prev, collection: c.value }))}
                  className="hover:cursor-pointer text-xs"
                >
                  {c.label}
                </Button>
              ))}
            </div>
            <p className="text-xs text-gray-500">{COLLECTIONS.find((c) => c.value === form.collection)?.description}</p>
          </div>

          {/* Visualization selector */}
          <div className="space-y-2">
            <label className="text-sm font-medium">Visualization</label>
            <div className="grid grid-cols-2 gap-2">
              {VISUALIZATIONS.map((v) => (
                <Button
                  key={v.value}
                  type="button"
                  variant={form.visualization === v.value ? 'default' : 'outline'}
                  size="sm"
                  onClick={() => setForm((prev) => ({ ...prev, visualization: v.value }))}
                  className="hover:cursor-pointer text-xs"
                >
                  {v.label}
                </Button>
              ))}
            </div>
          </div>

          {/* Date range */}
          <div className="grid grid-cols-2 gap-3">
            <div className="space-y-1">
              <label className="text-sm font-medium">From</label>
              <Input
                type="date"
                value={form.dateFrom}
                onChange={(e) => setForm((prev) => ({ ...prev, dateFrom: e.target.value }))}
              />
            </div>
            <div className="space-y-1">
              <label className="text-sm font-medium">To</label>
              <Input type="date" value={form.dateTo} onChange={(e) => setForm((prev) => ({ ...prev, dateTo: e.target.value }))} />
            </div>
          </div>

          {/* Cloud coverage slider */}
          <div className="space-y-2">
            <label className="text-sm font-medium">Max Cloud Cover: {form.maxcc}%</label>
            <input
              type="range"
              min={0}
              max={100}
              value={form.maxcc}
              onChange={(e) => setForm((prev) => ({ ...prev, maxcc: parseInt(e.target.value) }))}
              className="w-full accent-primary"
            />
          </div>

          {/* Name override */}
          <div className="space-y-1">
            <label className="text-sm font-medium">Layer Name (optional)</label>
            <Input
              placeholder={autoName()}
              value={form.name}
              onChange={(e) => setForm((prev) => ({ ...prev, name: e.target.value }))}
            />
          </div>

          {error && (
            <div className="flex items-start gap-3 p-3 bg-red-50 border border-red-200 rounded-md">
              <AlertTriangle className="h-5 w-5 text-red-500 mt-0.5 flex-shrink-0" />
              <div className="text-sm text-red-700">{error}</div>
            </div>
          )}
        </div>

        <DialogFooter>
          <Button type="button" variant="outline" onClick={handleClose} className="hover:cursor-pointer">
            Cancel
          </Button>
          <Button type="button" onClick={handleSubmit} className="hover:cursor-pointer" disabled={loading}>
            {loading ? (
              <>
                <Loader2 className="h-4 w-4 mr-2 animate-spin" />
                Adding Layer...
              </>
            ) : (
              'Add Satellite Layer'
            )}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
};
