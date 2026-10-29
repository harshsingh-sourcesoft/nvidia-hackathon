'use client';
import { useMemo } from 'react';
import { Layer, Line, Rect } from 'react-konva';
import { useEditorStore } from '@/store/useEditorStore';

interface Props {
  stageWidth:  number;
  stageHeight: number;
}

export default function GridLayer({ stageWidth, stageHeight }: Props) {
  const zoom         = useEditorStore(s => s.view.zoom);
  const offsetX      = useEditorStore(s => s.view.offsetX);
  const offsetY      = useEditorStore(s => s.view.offsetY);
  const gridSize     = useEditorStore(s => s.snapConfig.gridSize);
  const activeFloor  = useEditorStore(s => s.activeFloor());

  // Only render grid lines on the base floor (level 0)
  const isBaseFloor = activeFloor.level === 0;

  const { majorLines, minorLines } = useMemo(() => {
    if (!isBaseFloor) return { majorLines: [], minorLines: [] };

    const left   = (-offsetX) / zoom;
    const top    = (-offsetY) / zoom;
    const right  = (stageWidth  - offsetX) / zoom;
    const bottom = (stageHeight - offsetY) / zoom;

    const majorSize = gridSize * 4;
    const minor: number[][] = [];
    const major: number[][] = [];

    const startX = Math.floor(left / gridSize) * gridSize;
    for (let x = startX; x <= right; x += gridSize) {
      const isMajor = Math.abs(x % majorSize) < 0.5;
      (isMajor ? major : minor).push([x, top, x, bottom]);
    }

    const startY = Math.floor(top / gridSize) * gridSize;
    for (let y = startY; y <= bottom; y += gridSize) {
      const isMajor = Math.abs(y % majorSize) < 0.5;
      (isMajor ? major : minor).push([left, y, right, y]);
    }

    return { majorLines: major, minorLines: minor };
  }, [isBaseFloor, zoom, offsetX, offsetY, stageWidth, stageHeight, gridSize]);

  return (
    <Layer listening={false}>
      {/* Canvas background */}
      <Rect
        x={(-offsetX) / zoom}
        y={(-offsetY) / zoom}
        width={stageWidth  / zoom}
        height={stageHeight / zoom}
        fill="#181826"
      />
      {/* Minor grid — very subtle dark lines */}
      {minorLines.map((pts, i) => (
        <Line key={`mn-${i}`} points={pts}
          stroke="#1e1e30" strokeWidth={1 / zoom} listening={false} />
      ))}
      {/* Major grid — slightly more visible */}
      {majorLines.map((pts, i) => (
        <Line key={`mj-${i}`} points={pts}
          stroke="#28284a" strokeWidth={1 / zoom} listening={false} />
      ))}
    </Layer>
  );
}
