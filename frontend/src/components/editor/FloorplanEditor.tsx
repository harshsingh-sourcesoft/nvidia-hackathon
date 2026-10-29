'use client';
import { useCallback, useEffect, useRef, useState } from 'react';
import { Stage } from 'react-konva';
import type Konva from 'konva';
import { useEditorStore } from '@/store/useEditorStore';
import { snapPoint, nearestWall, wallRatioFromPoint, genId } from '@/utils/geometry';
import BackgroundLayer from './BackgroundLayer';
import GridLayer     from './GridLayer';
import RoomLayer     from './RoomLayer';
import WallLayer     from './WallLayer';
import FurnitureLayer from './FurnitureLayer';
import DrawingPreview from './DrawingPreview';
import SnapIndicator  from './SnapIndicator';
import type { Point2D } from '@/types';

export default function FloorplanEditor() {
  const containerRef = useRef<HTMLDivElement>(null);
  const stageRef     = useRef<Konva.Stage>(null);
  const [size, setSize] = useState({ w: 800, h: 600 });

  // Store
  const tool        = useEditorStore(s => s.tool);
  const drawing     = useEditorStore(s => s.drawing);
  const snapConfig  = useEditorStore(s => s.snapConfig);
  const activeFloor = useEditorStore(s => s.activeFloor());

  const {
    startWallDraw, updateWallPreview, finalizeWall, cancelWallDraw,
    setZoom, setOffset, updateSnap, deselect, setTool,
    deleteSelected, undo, redo, addDoor, addWindow,
  } = useEditorStore();

  // ── Resize observer ─────────────────────────────────────────────────────
  useEffect(() => {
    const el = containerRef.current;
    if (!el) return;
    const ro = new ResizeObserver(entries => {
      const { width, height } = entries[0].contentRect;
      setSize({ w: width, h: height });
      if (stageRef.current) {
        const stage = stageRef.current;
        if (stage.x() === 0 && stage.y() === 0) {
          stage.position({ x: width / 2, y: height / 2 });
          setOffset(width / 2, height / 2);
        }
      }
    });
    ro.observe(el);
    return () => ro.disconnect();
  }, [setOffset]);

  // ── Keyboard shortcuts ───────────────────────────────────────────────────
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.target instanceof HTMLInputElement) return;
      if (e.ctrlKey || e.metaKey) {
        switch (e.key) {
          case 'z': case 'Z': e.preventDefault(); e.shiftKey ? redo() : undo(); return;
          case 'y':           e.preventDefault(); redo(); return;
        }
        return;
      }
      switch (e.key) {
        case 'Escape':    cancelWallDraw(); deselect(); break;
        case 'Delete':
        case 'Backspace': e.preventDefault(); deleteSelected(); break;
        case 'v': case 'V': setTool('select');    break;
        case 'w': case 'W': setTool('wall');      break;
        case 'd': case 'D': setTool('door');      break;
        case 'n': case 'N': setTool('window');    break;
        case 'f': case 'F': setTool('furniture'); break;
        case 'h': case 'H': setTool('pan');       break;
        case 'x': case 'X': setTool('delete');    break;
      }
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [cancelWallDraw, deselect, deleteSelected, undo, redo, setTool]);

  // ── Helpers ──────────────────────────────────────────────────────────────

  const toWorld = useCallback((): Point2D => {
    const stage = stageRef.current!;
    const pos   = stage.getPointerPosition()!;
    const tr    = stage.getAbsoluteTransform().copy().invert();
    return tr.point(pos);
  }, []);

  const snapped = useCallback((raw: Point2D, shiftHeld = false): Point2D => {
    const { point, snapType } = snapPoint(
      raw, activeFloor.walls, snapConfig,
      drawing.startPoint, shiftHeld,
    );
    updateSnap(point, snapType);
    return point;
  }, [activeFloor.walls, snapConfig, drawing.startPoint, updateSnap]);

  // ── Canvas cursor ────────────────────────────────────────────────────────
  const cursor =
    tool === 'pan'    ? 'grab' :
    tool === 'delete' ? 'not-allowed' :
    tool === 'wall'   ? 'crosshair' :
    tool === 'door'   ? 'cell' :
    tool === 'window' ? 'cell' :
    'default';

  // ── Wheel zoom ───────────────────────────────────────────────────────────
  const handleWheel = useCallback((e: Konva.KonvaEventObject<WheelEvent>) => {
    e.evt.preventDefault();
    const stage = stageRef.current!;
    const old   = stage.scaleX();
    const factor = e.evt.deltaY < 0 ? 1.08 : 1 / 1.08;
    const next  = Math.min(Math.max(old * factor, 0.05), 8);
    const ptr   = stage.getPointerPosition()!;
    const newPos = {
      x: ptr.x - (ptr.x - stage.x()) * (next / old),
      y: ptr.y - (ptr.y - stage.y()) * (next / old),
    };
    stage.scale({ x: next, y: next });
    stage.position(newPos);
    setZoom(next);
    setOffset(newPos.x, newPos.y);
  }, [setZoom, setOffset]);

  // ── Stage drag (pan) ─────────────────────────────────────────────────────
  const handleDragEnd = useCallback((e: Konva.KonvaEventObject<DragEvent>) => {
    if (e.target !== stageRef.current) return;
    setOffset(e.target.x(), e.target.y());
  }, [setOffset]);

  // ── Mouse move ───────────────────────────────────────────────────────────
  const handleMouseMove = useCallback((e: Konva.KonvaEventObject<MouseEvent>) => {
    const raw  = toWorld();
    const snap = snapped(raw, e.evt.shiftKey);
    if (tool === 'wall' && drawing.isDrawing) {
      updateWallPreview(snap);
    }
  }, [toWorld, snapped, tool, drawing.isDrawing, updateWallPreview]);

  // ── Stage click ──────────────────────────────────────────────────────────
  const handleStageClick = useCallback((e: Konva.KonvaEventObject<MouseEvent>) => {
    const raw = toWorld();

    // Door / window: find nearest wall and place openings even on empty space
    if (tool === 'door' || tool === 'window') {
      // WallLayer already handles clicks ON wall shapes (with cancelBubble).
      // This branch fires only when clicking on empty space near a wall.
      const w = nearestWall(raw, activeFloor.walls, 40);
      if (w) {
        const ratio = wallRatioFromPoint(w, raw);
        const clamped = Math.max(0.05, Math.min(0.95, ratio));
        if (tool === 'door') {
          addDoor({ id: genId('door'), wallId: w.id, positionOnWall: clamped, width: 90, height: 210, type: 'door' });
        } else {
          addWindow({ id: genId('win'), wallId: w.id, positionOnWall: clamped, width: 120, height: 120, sillHeight: 90, type: 'window' });
        }
      }
      return;
    }

    // Wall drawing must fire regardless of what shape was clicked —
    // snapping handles aligning to existing endpoints.
    if (tool === 'wall') {
      const snap = snapped(raw, e.evt.shiftKey);
      if (!drawing.isDrawing) startWallDraw(snap);
      else finalizeWall(snap);
      return;
    }

    // For all other tools only act on the Stage background (not on shapes)
    if (e.target !== stageRef.current) return;

    if (tool === 'select') {
      deselect();
    }
  }, [toWorld, snapped, tool, drawing.isDrawing, startWallDraw, finalizeWall, deselect,
      activeFloor.walls, addDoor, addWindow]);

  // Right-click ends wall drawing
  const handleContextMenu = useCallback((e: Konva.KonvaEventObject<PointerEvent>) => {
    e.evt.preventDefault();
    cancelWallDraw();
  }, [cancelWallDraw]);

  // ── Render ───────────────────────────────────────────────────────────────
  return (
    <div
      ref={containerRef}
      className="w-full h-full"
      style={{ cursor }}
    >
      <Stage
        ref={stageRef}
        width={size.w}
        height={size.h}
        onWheel={handleWheel}
        onClick={handleStageClick}
        onMouseMove={handleMouseMove}
        onContextMenu={handleContextMenu}
        onDragEnd={handleDragEnd}
        draggable={tool === 'pan'}
      >
        <BackgroundLayer />
        <GridLayer stageWidth={size.w} stageHeight={size.h} />
        <RoomLayer rooms={activeFloor.rooms} />
        <WallLayer
          walls={activeFloor.walls}
          doors={activeFloor.doors}
          windows={activeFloor.windows}
        />
        <FurnitureLayer furniture={activeFloor.furniture} />
        <DrawingPreview />
        <SnapIndicator />
      </Stage>
    </div>
  );
}
