import { Map as MLMap, Marker } from 'maplibre-gl';
import React, { useEffect, useRef } from 'react';
import { createRoot, Root } from 'react-dom/client';
import { Cell, Pie, PieChart, Tooltip } from 'recharts';

export interface PieSlice {
  name: string;
  value: number;
  color: string;
}

export interface PieChartData {
  center: [number, number]; // [lng, lat]
  slices: PieSlice[];
}

interface BufferPieOverlayProps {
  map: MLMap;
  center: [number, number];
  slices: PieSlice[];
  onRemove?: () => void;
}

function PieContent({ slices, onRemove }: { slices: PieSlice[]; onRemove?: () => void }) {
  // Filter out zero-value slices for cleaner display
  const data = slices.filter((s) => s.value > 0);

  if (data.length === 0) return null;

  return (
    <div style={{ position: 'relative', width: 160, height: 160 }}>
      {onRemove && (
        <button
          onClick={(e) => {
            e.stopPropagation();
            onRemove();
          }}
          style={{
            position: 'absolute',
            top: 0,
            right: 0,
            zIndex: 10,
            width: 20,
            height: 20,
            borderRadius: '50%',
            border: '1px solid #666',
            background: '#1e1e2e',
            color: '#ccc',
            fontSize: 12,
            lineHeight: '18px',
            textAlign: 'center',
            cursor: 'pointer',
            padding: 0,
          }}
          title="Remove pie chart"
        >
          x
        </button>
      )}
      <PieChart width={160} height={160}>
        <Pie
          data={data}
          cx="50%"
          cy="50%"
          innerRadius={30}
          outerRadius={60}
          dataKey="value"
          labelLine={false}
          label={({ cx, cy, midAngle, innerRadius, outerRadius, percent }) => {
            if (percent < 0.05) return null;
            const RADIAN = Math.PI / 180;
            const radius = innerRadius + (outerRadius - innerRadius) * 0.5;
            const x = cx + radius * Math.cos(-midAngle * RADIAN);
            const y = cy + radius * Math.sin(-midAngle * RADIAN);
            return (
              <text
                x={x}
                y={y}
                fill="white"
                textAnchor="middle"
                dominantBaseline="central"
                style={{ fontSize: 10, fontWeight: 600 }}
              >
                {`${(percent * 100).toFixed(0)}%`}
              </text>
            );
          }}
        >
          {data.map((entry, i) => (
            <Cell key={i} fill={entry.color} />
          ))}
        </Pie>
        <Tooltip
          content={({ active, payload }) => {
            if (!active || !payload || payload.length === 0) return null;
            const d = payload[0].payload as PieSlice;
            return (
              <div
                style={{
                  background: '#1e1e2e',
                  border: '1px solid #444',
                  borderRadius: 6,
                  padding: '6px 10px',
                  fontSize: 12,
                  color: '#eee',
                }}
              >
                <div style={{ fontWeight: 600 }}>{d.name}</div>
                <div>{d.value.toFixed(1)}%</div>
              </div>
            );
          }}
        />
      </PieChart>
    </div>
  );
}

export const BufferPieOverlay: React.FC<BufferPieOverlayProps> = ({ map, center, slices, onRemove }) => {
  const markerRef = useRef<Marker | null>(null);
  const rootRef = useRef<Root | null>(null);

  useEffect(() => {
    const el = document.createElement('div');
    el.style.pointerEvents = 'auto';

    const root = createRoot(el);
    rootRef.current = root;
    root.render(<PieContent slices={slices} onRemove={onRemove} />);

    const marker = new Marker({ element: el, anchor: 'center' }).setLngLat(center).addTo(map);

    markerRef.current = marker;

    return () => {
      marker.remove();
      // Defer unmount to avoid React warning about synchronous unmount
      setTimeout(() => root.unmount(), 0);
    };
  }, [map, center, slices, onRemove]);

  return null;
};
