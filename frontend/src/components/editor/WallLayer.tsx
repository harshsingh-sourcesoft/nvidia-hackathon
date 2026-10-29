'use client';
import { useCallback } from 'react';
import { Layer, Line, Circle, Text, Group } from 'react-konva';
import type Konva from 'konva';
// eslint-disable-next-line @typescript-eslint/no-explicit-any
type KE = Konva.KonvaEventObject<any>;
import type { Wall, Door, WindowItem } from '@/types';
import { useEditorStore } from '@/store/useEditorStore';
import {
  midpoint, wallLength, formatLength, pointOnWall, wallAngle,
  wallRatioFromPoint, genId,
} from '@/utils/geometry';

interface Props {
  walls:   Wall[];
  doors:   Door[];
  windows: WindowItem[];
}

export default function WallLayer({ walls, doors, windows }: Props) {
  const tool      = useEditorStore(s => s.tool);
  const selection = useEditorStore(s => s.selection);
  const showDebug = useEditorStore(s => s.showDebug);
  const zoom      = useEditorStore(s => s.view.zoom);
  const { select, deselect } = useEditorStore();

  const toWorld = (e: KE) => {
    const stage = e.target.getStage()!;
    const pos   = stage.getPointerPosition()!;
    return stage.getAbsoluteTransform().copy().invert().point(pos);
  };

  const handleWallClick = useCallback((e: KE, id: string) => {
    // IMPORTANT: only cancelBubble for tools that actually handle the click here.
    // For 'wall' drawing tool we let the event bubble up to Stage so finalizeWall fires.
    if (tool === 'select') {
      e.cancelBubble = true;
      if (selection.id === id) deselect(); else select('wall', id);
    } else if (tool === 'delete') {
      e.cancelBubble = true;
      useEditorStore.getState().deleteWall(id);
    } else if (tool === 'door' || tool === 'window') {
      e.cancelBubble = true;
      const world = toWorld(e);
      const wall  = useEditorStore.getState().activeFloor().walls.find(w => w.id === id);
      if (!wall) return;
      const ratio = Math.max(0.05, Math.min(0.95, wallRatioFromPoint(wall, world)));
      if (tool === 'door') {
        useEditorStore.getState().addDoor({
          id: genId('door'), wallId: id, positionOnWall: ratio,
          width: 90, height: 210, type: 'door',
        });
      } else {
        useEditorStore.getState().addWindow({
          id: genId('win'), wallId: id, positionOnWall: ratio,
          width: 120, height: 120, sillHeight: 90, type: 'window',
        });
      }
    }
    // 'wall' tool: no cancelBubble → event bubbles to Stage → finalizeWall / startWallDraw
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [tool, selection.id, select, deselect]);

  const handleEndpointDrag = useCallback((
    e: Konva.KonvaEventObject<DragEvent>,
    wallId: string,
    endpoint: 'start' | 'end',
  ) => {
    const pos = e.target.position();
    useEditorStore.getState().updateWall(wallId, { [endpoint]: { x: pos.x, y: pos.y } });
  }, []);

  return (
    <Layer>
      {walls.map(wall => {
        const selected = selection.id === wall.id && selection.type === 'wall';
        const mp       = midpoint(wall.start, wall.end);
        const len      = wallLength(wall);
        const angle    = wallAngle(wall);
        const angleDeg = (angle * 180) / Math.PI;

        const wallDoors   = doors.filter(d => d.wallId === wall.id);
        const wallWindows = windows.filter(w => w.wallId === wall.id);
        const isDoorWindowTool = tool === 'door' || tool === 'window';

        return (
          <Group key={wall.id}>
            {/* Wall body */}
            <Line
              points={[wall.start.x, wall.start.y, wall.end.x, wall.end.y]}
              stroke={selected ? '#4f8ef7' : '#c8b89a'}
              strokeWidth={wall.thickness}
              lineCap="square"
              lineJoin="miter"
              hitStrokeWidth={Math.max(wall.thickness, 20)}
              onClick={e => handleWallClick(e, wall.id)}
              onTap={e => handleWallClick(e, wall.id)}
              shadowColor={selected ? '#4f8ef7' : 'transparent'}
              shadowBlur={selected ? 8 : 0}
              shadowOpacity={0.6}
              draggable={selected && tool === 'select'}
              onDragEnd={e => {
                const dx = e.target.x();
                const dy = e.target.y();
                e.target.position({ x: 0, y: 0 });
                if (Math.abs(dx) > 0.5 || Math.abs(dy) > 0.5) {
                  useEditorStore.getState().moveWall(wall.id, dx, dy);
                }
              }}
              onMouseEnter={(e: KE) => {
                const stage = e.target.getStage();
                if (!stage) return;
                if (isDoorWindowTool) stage.container().style.cursor = 'cell';
                else if (selected && tool === 'select') stage.container().style.cursor = 'move';
              }}
              onMouseLeave={(e: KE) => {
                const stage = e.target.getStage();
                if (stage) stage.container().style.cursor = '';
              }}
            />

            {/* Centreline dash */}
            <Line
              points={[wall.start.x, wall.start.y, wall.end.x, wall.end.y]}
              stroke={selected ? '#6ba3f7' : '#9a8472'}
              strokeWidth={1 / zoom}
              dash={[4 / zoom, 4 / zoom]}
              listening={false}
            />

            {/* Length label */}
            {len > 20 && (
              <Text
                x={mp.x} y={mp.y}
                text={formatLength(len)}
                fontSize={11 / zoom}
                fill={selected ? '#93c5fd' : '#a8b4c4'}
                align="center"
                offsetX={formatLength(len).length * 3 / zoom}
                offsetY={(wall.thickness / 2 + 14) / zoom}
                rotation={angleDeg}
                listening={false}
              />
            )}

            {showDebug && (
              <Text x={mp.x + 4} y={mp.y + 4} text={wall.id}
                fontSize={8 / zoom} fill="#f472b6" listening={false} />
            )}

            {/* Door markers */}
            {wallDoors.map(door => {
              // hw in design-units: door half-width is physically door.width/2 du
              const hw = door.width / 2;
              const pos = pointOnWall(wall, door.positionOnWall);
              return (
                <Group key={door.id} onClick={(e: KE) => {
                  e.cancelBubble = true;
                  if (tool === 'select') select('door', door.id);
                  else if (tool === 'delete') useEditorStore.getState().deleteDoor(door.id);
                }}>
                  <Line
                    points={[
                      pos.x - Math.cos(angle) * hw, pos.y - Math.sin(angle) * hw,
                      pos.x + Math.cos(angle) * hw, pos.y + Math.sin(angle) * hw,
                    ]}
                    stroke="#1e1e2e" strokeWidth={wall.thickness + 2} lineCap="butt" listening={false}
                  />
                  <Line
                    points={[
                      pos.x - Math.cos(angle) * hw, pos.y - Math.sin(angle) * hw,
                      pos.x + Math.cos(angle) * hw, pos.y + Math.sin(angle) * hw,
                    ]}
                    stroke={selection.id === door.id ? '#4f8ef7' : '#f59e0b'}
                    strokeWidth={2 / zoom} lineCap="round" hitStrokeWidth={16 / zoom}
                  />
                </Group>
              );
            })}

            {/* Window markers */}
            {wallWindows.map(win => {
              const hw  = win.width / 2;
              const pos = pointOnWall(wall, win.positionOnWall);
              return (
                <Group key={win.id} onClick={(e: KE) => {
                  e.cancelBubble = true;
                  if (tool === 'select') select('window', win.id);
                  else if (tool === 'delete') useEditorStore.getState().deleteWindow(win.id);
                }}>
                  <Line
                    points={[
                      pos.x - Math.cos(angle) * hw, pos.y - Math.sin(angle) * hw,
                      pos.x + Math.cos(angle) * hw, pos.y + Math.sin(angle) * hw,
                    ]}
                    stroke="#1e1e2e" strokeWidth={wall.thickness + 2} lineCap="butt" listening={false}
                  />
                  <Line
                    points={[
                      pos.x - Math.cos(angle) * hw, pos.y - Math.sin(angle) * hw,
                      pos.x + Math.cos(angle) * hw, pos.y + Math.sin(angle) * hw,
                    ]}
                    stroke={selection.id === win.id ? '#4f8ef7' : '#38bdf8'}
                    strokeWidth={3 / zoom} lineCap="square" hitStrokeWidth={16 / zoom}
                  />
                </Group>
              );
            })}

            {/* Endpoint handles */}
            {selected && [
              { pt: wall.start, ep: 'start' as const },
              { pt: wall.end,   ep: 'end'   as const },
            ].map(({ pt, ep }) => (
              <Circle
                key={ep} x={pt.x} y={pt.y}
                radius={6 / zoom} fill="#1e1e2e"
                stroke="#4f8ef7" strokeWidth={2 / zoom}
                draggable
                onDragMove={e => handleEndpointDrag(e, wall.id, ep)}
                onMouseEnter={(e: KE) => { const s = e.target.getStage(); if (s) s.container().style.cursor = 'crosshair'; }}
                onMouseLeave={(e: KE) => { const s = e.target.getStage(); if (s) s.container().style.cursor = ''; }}
              />
            ))}
          </Group>
        );
      })}
    </Layer>
  );
}
