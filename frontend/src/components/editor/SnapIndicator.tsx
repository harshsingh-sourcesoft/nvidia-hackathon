'use client';
import { Layer, Circle, Line } from 'react-konva';
import { useEditorStore } from '@/store/useEditorStore';

export default function SnapIndicator() {
  const snap = useEditorStore(s => s.snap);
  const zoom = useEditorStore(s => s.view.zoom);

  if (!snap.active || !snap.point) return null;

  const { x, y } = snap.point;
  const r  = 6 / zoom;
  const lw = 1.5 / zoom;

  const color = snap.type === 'endpoint'  ? '#22c55e'
              : snap.type === 'midpoint'  ? '#f59e0b'
              : snap.type === 'angle'     ? '#a78bfa'
              : /* grid */                  '#38bdf8';

  return (
    <Layer listening={false}>
      <Circle x={x} y={y} radius={r} stroke={color} strokeWidth={lw} fill="transparent" />
      {/* Cross-hair */}
      <Line points={[x - r * 1.6, y, x + r * 1.6, y]} stroke={color} strokeWidth={lw} />
      <Line points={[x, y - r * 1.6, x, y + r * 1.6]} stroke={color} strokeWidth={lw} />
    </Layer>
  );
}
