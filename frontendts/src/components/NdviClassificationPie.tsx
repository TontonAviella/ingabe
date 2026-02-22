import { Cell, Legend, Pie, PieChart, ResponsiveContainer, Tooltip } from 'recharts';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';

export interface NdviClassificationPieProps {
  classification?: Record<string, { count: number; percentage: number }>;
  isLoading?: boolean;
}

const NDVI_COLORS: Record<string, string> = {
  'Bare soil': '#ef4444',
  'Sparse vegetation': '#f97316',
  'Moderate vegetation': '#fbbf24',
  'Healthy vegetation': '#22c55e',
  'Very healthy vegetation': '#10b981',
  Water: '#3b82f6',
  Unknown: '#9ca3af',
};

export function NdviClassificationPie({ classification, isLoading }: NdviClassificationPieProps) {
  if (isLoading) {
    return (
      <Card>
        <CardHeader>
          <CardTitle>Land Cover Classification</CardTitle>
        </CardHeader>
        <CardContent>
          <div className="h-[300px] flex items-center justify-center bg-gray-50 dark:bg-gray-800/50 rounded-lg animate-pulse">
            <p className="text-sm text-muted-foreground">Loading classification data...</p>
          </div>
        </CardContent>
      </Card>
    );
  }

  if (!classification || Object.keys(classification).length === 0) {
    return (
      <Card>
        <CardHeader>
          <CardTitle>Land Cover Classification</CardTitle>
        </CardHeader>
        <CardContent>
          <div className="h-[300px] flex items-center justify-center bg-gray-50 dark:bg-gray-800/50 rounded-lg">
            <p className="text-sm text-muted-foreground">No classification data available</p>
          </div>
        </CardContent>
      </Card>
    );
  }

  const chartData = Object.entries(classification).map(([name, data]) => ({
    name,
    value: data.percentage,
    count: data.count,
  }));

  const dominantClass = chartData.reduce((max, item) => (item.value > max.value ? item : max));

  return (
    <Card>
      <CardHeader>
        <CardTitle>Land Cover Classification</CardTitle>
      </CardHeader>
      <CardContent>
        <ResponsiveContainer width="100%" height={300}>
          <PieChart>
            <Pie
              data={chartData}
              cx="50%"
              cy="50%"
              labelLine={false}
              label={renderCustomLabel}
              outerRadius={80}
              innerRadius={50}
              fill="#8884d8"
              dataKey="value"
            >
              {chartData.map((entry) => (
                <Cell key={entry.name} fill={NDVI_COLORS[entry.name] || NDVI_COLORS['Unknown']} />
              ))}
            </Pie>
            <Tooltip content={<CustomTooltip />} />
            <Legend
              verticalAlign="bottom"
              height={36}
              formatter={(value, entry) => {
                const data = entry.payload as unknown as { value: number; count: number };
                return `${value} (${data.value.toFixed(1)}%)`;
              }}
            />
          </PieChart>
        </ResponsiveContainer>
        <div className="mt-4 text-center">
          <p className="text-sm text-muted-foreground">
            Dominant Class: <span className="font-semibold text-foreground">{dominantClass.name}</span>
          </p>
          <p className="text-xs text-muted-foreground mt-1">{dominantClass.value.toFixed(1)}% of area</p>
        </div>
      </CardContent>
    </Card>
  );
}

interface CustomLabelProps {
  cx: number;
  cy: number;
  midAngle: number;
  innerRadius: number;
  outerRadius: number;
  percent: number;
}

function renderCustomLabel({ cx, cy, midAngle, innerRadius, outerRadius, percent }: CustomLabelProps) {
  const RADIAN = Math.PI / 180;
  const radius = innerRadius + (outerRadius - innerRadius) * 0.5;
  const x = cx + radius * Math.cos(-midAngle * RADIAN);
  const y = cy + radius * Math.sin(-midAngle * RADIAN);

  if (percent < 0.05) return null;

  return (
    <text x={x} y={y} fill="white" textAnchor={x > cx ? 'start' : 'end'} dominantBaseline="central" className="text-xs font-semibold">
      {`${(percent * 100).toFixed(0)}%`}
    </text>
  );
}

interface TooltipPayload {
  name: string;
  value: number;
  payload: {
    name: string;
    value: number;
    count: number;
  };
}

function CustomTooltip({ active, payload }: { active?: boolean; payload?: TooltipPayload[] }) {
  if (!active || !payload || payload.length === 0) {
    return null;
  }

  const data = payload[0].payload;

  return (
    <div className="bg-white dark:bg-gray-800 border border-gray-200 dark:border-gray-700 rounded-lg p-3 shadow-lg">
      <p className="text-sm font-medium mb-1">{data.name}</p>
      <p className="text-sm text-muted-foreground">
        Coverage: <span className="font-semibold text-foreground">{data.value.toFixed(1)}%</span>
      </p>
      <p className="text-xs text-muted-foreground">Parcels: {data.count.toLocaleString()}</p>
    </div>
  );
}
