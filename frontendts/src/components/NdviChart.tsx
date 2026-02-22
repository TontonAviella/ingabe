import { Area, AreaChart, CartesianGrid, ReferenceLine, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';

export interface NdviChartProps {
  timeseries?: Array<{ date: string; mean_ndvi: number }>;
  isLoading?: boolean;
  title?: string;
}

export function NdviChart({ timeseries, isLoading, title = 'NDVI Time Series' }: NdviChartProps) {
  if (isLoading) {
    return (
      <Card>
        <CardHeader>
          <CardTitle>{title}</CardTitle>
        </CardHeader>
        <CardContent>
          <div className="h-[300px] flex items-center justify-center bg-gray-50 dark:bg-gray-800/50 rounded-lg animate-pulse">
            <p className="text-sm text-muted-foreground">Loading chart data...</p>
          </div>
        </CardContent>
      </Card>
    );
  }

  if (!timeseries || timeseries.length === 0) {
    return (
      <Card>
        <CardHeader>
          <CardTitle>{title}</CardTitle>
        </CardHeader>
        <CardContent>
          <div className="h-[300px] flex items-center justify-center bg-gray-50 dark:bg-gray-800/50 rounded-lg">
            <p className="text-sm text-muted-foreground">No time series data available</p>
          </div>
        </CardContent>
      </Card>
    );
  }

  const chartData = timeseries.map((point) => ({
    date: point.date,
    ndvi: Number(point.mean_ndvi.toFixed(3)),
    formattedDate: formatChartDate(point.date),
  }));

  return (
    <Card>
      <CardHeader>
        <CardTitle>{title}</CardTitle>
      </CardHeader>
      <CardContent>
        <ResponsiveContainer width="100%" height={300}>
          <AreaChart data={chartData} margin={{ top: 10, right: 30, left: 0, bottom: 0 }}>
            <defs>
              <linearGradient id="ndviGradient" x1="0" y1="0" x2="0" y2="1">
                <stop offset="0%" stopColor="#22c55e" stopOpacity={0.8} />
                <stop offset="50%" stopColor="#fbbf24" stopOpacity={0.6} />
                <stop offset="100%" stopColor="#ef4444" stopOpacity={0.8} />
              </linearGradient>
            </defs>
            <CartesianGrid strokeDasharray="3 3" className="stroke-gray-200 dark:stroke-gray-700" />
            <XAxis dataKey="formattedDate" className="text-xs" tick={{ fill: 'currentColor', className: 'fill-muted-foreground' }} />
            <YAxis
              domain={[0, 1]}
              ticks={[0, 0.2, 0.4, 0.6, 0.8, 1.0]}
              className="text-xs"
              tick={{ fill: 'currentColor', className: 'fill-muted-foreground' }}
              label={{ value: 'NDVI', angle: -90, position: 'insideLeft', className: 'fill-muted-foreground' }}
            />
            <Tooltip content={<CustomTooltip />} />
            <ReferenceLine
              y={0.2}
              stroke="#ef4444"
              strokeDasharray="5 5"
              label={{
                value: 'Bare Soil Threshold',
                position: 'insideBottomRight',
                className: 'text-xs fill-red-600 dark:fill-red-400',
              }}
            />
            <ReferenceLine
              y={0.6}
              stroke="#22c55e"
              strokeDasharray="5 5"
              label={{
                value: 'Healthy Vegetation',
                position: 'insideTopRight',
                className: 'text-xs fill-green-600 dark:fill-green-400',
              }}
            />
            <Area type="monotone" dataKey="ndvi" stroke="#3b82f6" strokeWidth={2} fill="url(#ndviGradient)" />
          </AreaChart>
        </ResponsiveContainer>
      </CardContent>
    </Card>
  );
}

function formatChartDate(dateString: string): string {
  try {
    const date = new Date(dateString);
    const month = date.toLocaleString('en-US', { month: 'short' });
    const day = String(date.getDate()).padStart(2, '0');
    return `${month} ${day}`;
  } catch {
    return dateString.slice(0, 10);
  }
}

function getNdviHealthLabel(ndvi: number): string {
  if (ndvi < 0.2) return 'Bare soil / No vegetation';
  if (ndvi < 0.4) return 'Sparse vegetation';
  if (ndvi < 0.6) return 'Moderate vegetation';
  if (ndvi < 0.8) return 'Healthy vegetation';
  return 'Very healthy vegetation';
}

interface TooltipPayload {
  value: number;
  payload: {
    date: string;
    ndvi: number;
    formattedDate: string;
  };
}

function CustomTooltip({ active, payload }: { active?: boolean; payload?: TooltipPayload[] }) {
  if (!active || !payload || payload.length === 0) {
    return null;
  }

  const data = payload[0].payload;
  const ndvi = data.ndvi;

  return (
    <div className="bg-white dark:bg-gray-800 border border-gray-200 dark:border-gray-700 rounded-lg p-3 shadow-lg">
      <p className="text-sm font-medium mb-1">{data.formattedDate}</p>
      <p className="text-sm text-muted-foreground mb-1">
        NDVI: <span className="font-semibold text-foreground">{ndvi.toFixed(3)}</span>
      </p>
      <p className="text-xs text-muted-foreground">{getNdviHealthLabel(ndvi)}</p>
    </div>
  );
}
