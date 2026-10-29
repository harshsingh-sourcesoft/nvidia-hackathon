'use client';
import { useCallback } from 'react';
import { Layer, Rect, Text, Group } from 'react-konva';
import type Konva from 'konva';
import type { FurnitureItem } from '@/types';
import { useEditorStore } from '@/store/useEditorStore';

interface Props { furniture: FurnitureItem[] }

const FURNITURE_COLORS: Record<string, string> = {
  sofa:    '#4a6fa5',
  bed:     '#7a5c9a',
  table:   '#6b8f5e',
  chair:   '#4a6fa5',
  desk:    '#6b8f5e',
  bathtub: '#5c8fa8',
  toilet:  '#a8a85c',
  default: '#5a6a7a',
};

export default function FurnitureLayer({ furniture }: Props) {
  const tool      = useEditorStore(s => s.tool);
  const selection = useEditorStore(s => s.selection);
  const zoom      = useEditorStore(s => s.view.zoom);
  const { select, deselect, updateFurniture, deleteFurniture } = useEditorStore();

  const handleClick = useCallback((e: Konva.KonvaEventObject<MouseEvent>, id: string) => {
    e.cancelBubble = true;
    if (tool === 'delete') { deleteFurniture(id); return; }
    if (selection.id === id) deselect(); else select('furniture', id);
  }, [tool, selection.id, select, deselect, deleteFurniture]);

  const handleDragEnd = useCallback((
    e: Konva.KonvaEventObject<DragEvent>,
    id: string,
  ) => {
    updateFurniture(id, {
      position: { x: e.target.x(), y: e.target.y() },
    });
  }, [updateFurniture]);

  return (
    <Layer>
      {furniture.map(item => {
        const selected = selection.id === item.id && selection.type === 'furniture';
        const color    = FURNITURE_COLORS[item.furnitureType] ?? FURNITURE_COLORS.default;
        const hw = item.width / 2;
        const hd = item.depth / 2;

        return (
          <Group
            key={item.id}
            x={item.position.x}
            y={item.position.y}
            rotation={item.rotation}
            draggable={tool === 'select'}
            onClick={e => handleClick(e, item.id)}
            onDragEnd={e => handleDragEnd(e, item.id)}
          >
            <Rect
              x={-hw}
              y={-hd}
              width={item.width}
              height={item.depth}
              fill={color + (selected ? 'dd' : '88')}
              stroke={selected ? '#4f8ef7' : color}
              strokeWidth={(selected ? 2 : 1) / zoom}
              cornerRadius={3 / zoom}
            />
            <Text
              x={-hw}
              y={-hd}
              width={item.width}
              height={item.depth}
              text={item.label ?? item.furnitureType}
              fontSize={10 / zoom}
              fill="#e2e8f0"
              align="center"
              verticalAlign="middle"
              listening={false}
            />
          </Group>
        );
      })}
    </Layer>
  );
}
