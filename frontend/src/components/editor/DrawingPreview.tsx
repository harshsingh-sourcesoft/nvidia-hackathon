'use client';
import { Layer, Line, Circle, Text } from 'react-konva';
import { useEditorStore } from '@/store/useEditorStore';
import { distance, formatLength, midpoint, wallAngle } from '@/utils/geometry';

export default function DrawingPreview() {
  const drawing = useEditorStore(s => s.drawing);
  const zoom    = useEditorStore(s => s.view.zoom);
  const tool    = useEditorStore(s => s.tool);

  if (tool !== 'wall' || !drawing.isDrawing || !drawing.startPoint || !drawing.currentPoint) {
    return null;
  }

  const { startPoint: sp, currentPoint: ep } = drawing;
  const len      = distance(sp, ep);
  const mp       = midpoint(sp, ep);
  const angle    = wallAngle({ start: sp, end: ep, id: '', thickness: 15, height: 270, type: 'wall' });
  const angleDeg = (angle * 180) / Math.PI;

  return (
    <Layer listening={false}>
      {/* Preview wall line */}
      <Line
        points={[sp.x, sp.y, ep.x, ep.y]}
        stroke="#4f8ef7"
        strokeWidth={15}
        lineCap="square"
        opacity={0.5}
      />
      {/* Centreline */}
      <Line
        points={[sp.x, sp.y, ep.x, ep.y]}
        stroke="#93c5fd"
        strokeWidth={1 / zoom}
        dash={[6 / zoom, 4 / zoom]}
      />
      {/* Start dot */}
      <Circle
        x={sp.x}
        y={sp.y}
        radius={5 / zoom}
        fill="#4f8ef7"
      />
      {/* End dot */}
      <Circle
        x={ep.x}
        y={ep.y}
        radius={4 / zoom}
        fill="#93c5fd"
        stroke="#4f8ef7"
        strokeWidth={1.5 / zoom}
      />
      {/* Length label */}
      {len > 15 && (
        <Text
          x={mp.x}
          y={mp.y}
          text={formatLength(len)}
          fontSize={11 / zoom}
          fill="#93c5fd"
          fontStyle="bold"
          align="center"
          offsetX={formatLength(len).length * 3.5 / zoom}
          offsetY={(15 / 2 + 14) / zoom}
          rotation={angleDeg}
        />
      )}
    </Layer>
  );
}
