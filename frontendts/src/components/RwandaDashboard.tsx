import { Activity, AlertTriangle, MapPin, Minus, TrendingDown, TrendingUp } from 'lucide-react';
import { useEffect, useState } from 'react';
import { NdviChart } from '@/components/NdviChart';
import { RwandaMap } from '@/components/RwandaMap';
import { SupersetEmbed } from '@/components/SupersetEmbed';
import { Badge } from '@/components/ui/badge';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import { type NdviTimeseriesPoint, useDistrictSummary, useMLStatus, useNdviTimeseries, useYieldRiskPrediction } from '@/hooks/useRwandaApi';
import { cn } from '@/lib/utils';

// Rwanda districts
const DISTRICTS = [
  'Bugesera',
  'Burera',
  'Gakenke',
  'Gasabo',
  'Gatsibo',
  'Gicumbi',
  'Gisagara',
  'Huye',
  'Kamonyi',
  'Karongi',
  'Kayonza',
  'Kicukiro',
  'Kirehe',
  'Muhanga',
  'Musanze',
  'Ngoma',
  'Ngororero',
  'Nyabihu',
  'Nyagatare',
  'Nyamagabe',
  'Nyamasheke',
  'Nyanza',
  'Nyarugenge',
  'Nyaruguru',
  'Rubavu',
  'Ruhango',
  'Rulindo',
  'Rusizi',
  'Rutsiro',
  'Rwamagana',
];

export function RwandaDashboard() {
  const [selectedDistrict, setSelectedDistrict] = useState<string | undefined>();
  const { data: summary, isLoading: summaryLoading } = useDistrictSummary(selectedDistrict);
  const { data: mlStatus } = useMLStatus();
  const { data: timeseries, isLoading: timeseriesLoading } = useNdviTimeseries(
    selectedDistrict ? { district: selectedDistrict } : undefined,
  );
  const { mutate: predictYieldRisk, data: yieldRisk, isPending: isAnalyzing } = useYieldRiskPrediction();

  // Calculate NDVI trend from timeseries
  const ndviTrend = calculateTrend(timeseries?.timeseries);

  // Auto-run yield risk prediction when timeseries data is available
  useEffect(() => {
    if (timeseries?.timeseries && timeseries.timeseries.length > 0) {
      predictYieldRisk(timeseries.timeseries);
    }
  }, [timeseries, predictYieldRisk]);

  return (
    <div className="flex flex-col gap-4 p-6 max-w-7xl mx-auto">
      {/* Map Section */}
      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2">
            <MapPin className="size-5" />
            Rwanda Agriculture Map
          </CardTitle>
          <CardDescription>H3 hexagonal grid showing NDVI vegetation health across Rwanda</CardDescription>
        </CardHeader>
        <CardContent className="p-0">
          <div className="h-[60vh] w-full">
            <RwandaMap selectedDistrict={selectedDistrict} />
          </div>
        </CardContent>
      </Card>

      {/* Dashboard Section */}
      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2">
            <MapPin className="size-5" />
            District Statistics
          </CardTitle>
          <CardDescription>
            Monitor agricultural health across Rwanda's districts using satellite-derived vegetation indices
          </CardDescription>
        </CardHeader>
        <CardContent>
          {/* District Selector */}
          <div className="space-y-4">
            <div>
              <label htmlFor="district-select" className="block text-sm font-medium mb-2">
                Select District
              </label>
              <select
                id="district-select"
                className="w-full p-2 border rounded-md dark:bg-gray-800 dark:border-gray-700 focus:outline-none focus:ring-2 focus:ring-primary"
                value={selectedDistrict || ''}
                onChange={(e) => setSelectedDistrict(e.target.value || undefined)}
              >
                <option value="">Choose a district...</option>
                {DISTRICTS.map((d) => (
                  <option key={d} value={d}>
                    {d}
                  </option>
                ))}
              </select>
            </div>

            {/* Loading State */}
            {summaryLoading && selectedDistrict && (
              <div className="py-8 text-center">
                <Activity className="size-6 animate-spin mx-auto mb-2 text-muted-foreground" />
                <p className="text-sm text-muted-foreground">Loading district data...</p>
              </div>
            )}

            {/* Summary Stats */}
            {summary && selectedDistrict && !summaryLoading && (
              <div className="space-y-4">
                <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
                  {/* Parcel Count */}
                  <div className="p-4 bg-green-50 dark:bg-green-900/20 rounded-lg border border-green-200 dark:border-green-800">
                    <div className="text-sm font-medium text-green-900 dark:text-green-100 mb-1">Agricultural Parcels</div>
                    <div className="text-2xl font-bold text-green-700 dark:text-green-300">
                      {summary.parcel_count?.toLocaleString() ?? 'N/A'}
                    </div>
                  </div>

                  {/* Average NDVI */}
                  <div className="p-4 bg-blue-50 dark:bg-blue-900/20 rounded-lg border border-blue-200 dark:border-blue-800">
                    <div className="flex items-center justify-between mb-1">
                      <span className="text-sm font-medium text-blue-900 dark:text-blue-100">Average NDVI</span>
                      {ndviTrend && <TrendIcon trend={ndviTrend} />}
                    </div>
                    <div className="text-2xl font-bold text-blue-700 dark:text-blue-300">{summary.avg_ndvi?.toFixed(3) ?? 'N/A'}</div>
                    <div className="text-xs text-blue-600 dark:text-blue-400 mt-1">{getNdviHealthLabel(summary.avg_ndvi)}</div>
                  </div>

                  {/* Yield Risk */}
                  <div className="p-4 bg-amber-50 dark:bg-amber-900/20 rounded-lg border border-amber-200 dark:border-amber-800">
                    <div className="text-sm font-medium text-amber-900 dark:text-amber-100 mb-1">Yield Risk Assessment</div>
                    {isAnalyzing && <div className="text-sm text-muted-foreground">Analyzing...</div>}
                    {yieldRisk && !isAnalyzing && (
                      <div className="flex items-center gap-2">
                        <Badge variant={getRiskVariant(yieldRisk.risk_level)}>{yieldRisk.risk_level.toUpperCase()}</Badge>
                        <span className="text-xs text-muted-foreground">{(yieldRisk.confidence * 100).toFixed(0)}% confidence</span>
                      </div>
                    )}
                    {!yieldRisk && !isAnalyzing && timeseries && <div className="text-sm text-muted-foreground">No data available</div>}
                  </div>
                </div>

                {/* Timeseries Summary */}
                {timeseries?.summary && (
                  <div className="p-4 bg-gray-50 dark:bg-gray-800/50 rounded-lg border">
                    <h3 className="text-sm font-semibold mb-2">NDVI Range (Historical)</h3>
                    <div className="grid grid-cols-3 gap-4 text-sm">
                      <div>
                        <span className="text-muted-foreground">Min:</span>{' '}
                        <span className="font-medium">{timeseries.summary.min.toFixed(3)}</span>
                      </div>
                      <div>
                        <span className="text-muted-foreground">Mean:</span>{' '}
                        <span className="font-medium">{timeseries.summary.mean.toFixed(3)}</span>
                      </div>
                      <div>
                        <span className="text-muted-foreground">Max:</span>{' '}
                        <span className="font-medium">{timeseries.summary.max.toFixed(3)}</span>
                      </div>
                    </div>
                  </div>
                )}

                {/* Recommendations */}
                {yieldRisk?.recommendations && yieldRisk.recommendations.length > 0 && (
                  <div className="p-4 bg-yellow-50 dark:bg-yellow-900/20 rounded-lg border border-yellow-200 dark:border-yellow-800">
                    <div className="flex items-center gap-2 mb-2">
                      <AlertTriangle className="size-4 text-yellow-700 dark:text-yellow-400" />
                      <h3 className="text-sm font-semibold text-yellow-900 dark:text-yellow-100">Recommendations</h3>
                    </div>
                    <ul className="space-y-1 text-sm text-yellow-800 dark:text-yellow-200">
                      {yieldRisk.recommendations.map((rec, idx) => (
                        <li key={idx} className="flex items-start gap-2">
                          <span className="mt-1">•</span>
                          <span>{rec}</span>
                        </li>
                      ))}
                    </ul>
                  </div>
                )}
              </div>
            )}

            {/* Empty State */}
            {!selectedDistrict && (
              <div className="py-8 text-center text-muted-foreground">
                <MapPin className="size-12 mx-auto mb-3 opacity-50" />
                <p>Select a district to view agricultural data</p>
              </div>
            )}
          </div>
        </CardContent>
      </Card>

      {/* NDVI Time Series Chart */}
      {selectedDistrict && <NdviChart timeseries={timeseries?.timeseries} isLoading={timeseriesLoading} title="NDVI Time Series" />}

      {/* Superset Analytics */}
      <SupersetEmbed title="Rwanda Agriculture Analytics" />

      {/* ML Status Footer */}
      {mlStatus && (
        <div className="flex items-center justify-center gap-2 text-xs text-muted-foreground">
          <span className={cn('size-2 rounded-full', mlStatus.ml_ready ? 'bg-green-500 animate-pulse' : 'bg-yellow-500')} />
          <span>
            ML Service: {mlStatus.ml_ready ? 'Ready' : 'Baseline mode'}
            {mlStatus.model_version && ` (v${mlStatus.model_version})`}
          </span>
        </div>
      )}
    </div>
  );
}

// Helper functions

function calculateTrend(timeseries?: NdviTimeseriesPoint[]): 'up' | 'down' | 'stable' | null {
  if (!timeseries || timeseries.length < 2) return null;

  // Compare last 3 points to previous 3 points
  const recentCount = Math.min(3, Math.floor(timeseries.length / 2));
  const recent = timeseries.slice(-recentCount);
  const previous = timeseries.slice(-recentCount * 2, -recentCount);

  if (recent.length === 0 || previous.length === 0) return null;

  const recentAvg = recent.reduce((sum, p) => sum + p.mean_ndvi, 0) / recent.length;
  const previousAvg = previous.reduce((sum, p) => sum + p.mean_ndvi, 0) / previous.length;

  const diff = recentAvg - previousAvg;
  const threshold = 0.02; // 2% change threshold

  if (diff > threshold) return 'up';
  if (diff < -threshold) return 'down';
  return 'stable';
}

function TrendIcon({ trend }: { trend: 'up' | 'down' | 'stable' }) {
  if (trend === 'up') {
    return <TrendingUp className="size-4 text-green-600 dark:text-green-400" />;
  }
  if (trend === 'down') {
    return <TrendingDown className="size-4 text-red-600 dark:text-red-400" />;
  }
  return <Minus className="size-4 text-gray-600 dark:text-gray-400" />;
}

function getNdviHealthLabel(ndvi?: number): string {
  if (!ndvi) return 'Unknown';
  if (ndvi < 0.2) return 'Bare soil / No vegetation';
  if (ndvi < 0.4) return 'Sparse vegetation';
  if (ndvi < 0.6) return 'Moderate vegetation';
  if (ndvi < 0.8) return 'Healthy vegetation';
  return 'Very healthy vegetation';
}

function getRiskVariant(riskLevel: 'low' | 'medium' | 'high'): 'default' | 'secondary' | 'destructive' {
  switch (riskLevel) {
    case 'low':
      return 'secondary';
    case 'medium':
      return 'default';
    case 'high':
      return 'destructive';
  }
}
