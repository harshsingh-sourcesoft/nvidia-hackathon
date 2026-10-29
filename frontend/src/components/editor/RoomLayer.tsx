'use client';
import { Layer, Line, Text } from 'react-konva';
import type { Room } from '@/types';
import { useEditorStore } from '@/store/useEditorStore';
import { polygonCentroid } from '@/utils/geometry';

interface Props { rooms: Room[] }

export default function RoomLayer({ rooms }: Props) {
  const selection = useEditorStore(s => s.selection);
  const tool      = useEditorStore(s => s.tool);
  const zoom      = useEditorStore(s => s.view.zoom);
  const { select, deselect } = useEditorStore();

  return (
    <Layer listening={tool === 'select'}>
      {/* Filled polygons */}
      {rooms.map(room => {
        const selected = selection.id === room.id && selection.type === 'room';
        const flatPts  = room.polygon.flatMap(p => [p.x, p.y]);
        return (
          <Line
            key={room.id}
            points={flatPts}
            closed
            fill={room.color + (selected ? 'cc' : '55')}
            stroke={selected ? '#4f8ef7' : room.color + 'aa'}
            strokeWidth={selected ? 2 / zoom : 1 / zoom}
            onClick={(e: { cancelBubble: boolean }) => {
              e.cancelBubble = true;
              if (selected) deselect(); else select('room', room.id);
            }}
          />
        );
      })}

      {/* Room name labels */}
      {rooms.map(room => {
        const c = polygonCentroid(room.polygon);
        return (
          <Text
            key={`lbl-${room.id}`}
            x={c.x}
            y={c.y - 10 / zoom}
            text={room.name}
            fontSize={12 / zoom}
            fill="#e2e8f0"
            fontStyle="bold"
            align="center"
            offsetX={room.name.length * 3.5 / zoom}
            listening={false}
          />
        );
      })}

      {/* Area labels */}
      {rooms.map(room => {
        const c         = polygonCentroid(room.polygon);
        const areaLabel = `${room.area.toFixed(1)} m²`;
        return (
          <Text
            key={`area-${room.id}`}
            x={c.x}
            y={c.y + 4 / zoom}
            text={areaLabel}
            fontSize={10 / zoom}
            fill="#94a3b8"
            align="center"
            offsetX={areaLabel.length * 2.8 / zoom}
            listening={false}
          />
        );
      })}
    </Layer>
  );
}
