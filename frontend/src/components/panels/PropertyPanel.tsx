'use client';
import { useState } from 'react';
import { useEditorStore } from '@/store/useEditorStore';
import { wallLengthMeters } from '@/utils/geometry';
import PolyhavenBrowser from './PolyhavenBrowser';
import type { PolyhavenMaterial } from '@/types';

const FURNITURE_TYPES = ['sofa','bed','chair','table','desk','bathtub','toilet','wardrobe'];

const WALL_PRESETS: { label: string; color: string; roughness: number }[] = [
  { label: 'Plaster',   color: '#c8b89a', roughness: 0.90 },
  { label: 'White',     color: '#f0ede8', roughness: 0.85 },
  { label: 'Brick',     color: '#b5613a', roughness: 0.92 },
  { label: 'Concrete',  color: '#8a8a8a', roughness: 0.95 },
  { label: 'Stone',     color: '#7a7a68', roughness: 0.95 },
  { label: 'Wood',      color: '#8b6914', roughness: 0.70 },
];

export default function PropertyPanel() {
  const selection  = useEditorStore(s => s.selection);
  const showDebug  = useEditorStore(s => s.showDebug);
  const snapConfig = useEditorStore(s => s.snapConfig);
  const { setSnapConfig } = useEditorStore();
  const [phOpen, setPhOpen] = useState(false);

  const wall      = useEditorStore(s => s.selectedWall());
  const room      = useEditorStore(s => s.selectedRoom());
  const door      = useEditorStore(s => s.selectedDoor());
  const win       = useEditorStore(s => s.selectedWindow());
  const furniture = useEditorStore(s => s.selectedFurniture());

  const { updateWall, updateRoom, updateDoor, updateWindow, updateFurniture } =
    useEditorStore.getState();

  // ── No selection ─────────────────────────────────────────────────────────
  if (!selection.id) {
    return (
      <aside className="w-64 flex-shrink-0 bg-[#13131f] border-l border-[#2e2e4a] flex flex-col overflow-y-auto">
        <div className="p-3 border-b border-[#2e2e4a]">
          <h3 className="text-xs font-semibold text-gray-400 uppercase tracking-wide">Properties</h3>
        </div>
        <div className="p-3 text-xs text-gray-500 italic">Nothing selected</div>

        <Section title="Snapping">
          <label className="flex items-center gap-2 text-xs text-gray-300 cursor-pointer">
            <input type="checkbox" checked={snapConfig.enabled}
              onChange={e => setSnapConfig({ enabled: e.target.checked })} />
            Enable snapping
          </label>
          <label className="flex items-center gap-2 text-xs text-gray-300 cursor-pointer mt-1">
            <input type="checkbox" checked={snapConfig.snapToGrid}
              onChange={e => setSnapConfig({ snapToGrid: e.target.checked })} />
            Snap to grid
          </label>
          <label className="flex items-center gap-2 text-xs text-gray-300 cursor-pointer mt-1">
            <input type="checkbox" checked={snapConfig.snapToEndpoints}
              onChange={e => setSnapConfig({ snapToEndpoints: e.target.checked })} />
            Snap to endpoints
          </label>
          <label className="flex items-center gap-2 text-xs text-gray-300 cursor-pointer mt-1">
            <input type="checkbox" checked={snapConfig.snapToMidpoints}
              onChange={e => setSnapConfig({ snapToMidpoints: e.target.checked })} />
            Snap to midpoints
          </label>
          <label className="flex items-center gap-2 text-xs text-gray-300 cursor-pointer mt-1">
            <input type="checkbox" checked={snapConfig.angleSnap}
              onChange={e => setSnapConfig({ angleSnap: e.target.checked })} />
            Angle snap (45°)
          </label>
          <PropRow label="Grid size">
            <input type="number" min="5" max="200" step="5"
              value={snapConfig.gridSize}
              onChange={e => setSnapConfig({ gridSize: Number(e.target.value) })}
              className="prop-input" />
          </PropRow>
        </Section>

        <Section title="Add Furniture">
          <div className="grid grid-cols-2 gap-1">
            {FURNITURE_TYPES.map(ft => (
              <button
                key={ft}
                onClick={() => {
                  useEditorStore.getState().addFurniture({
                    id: `furn_${Date.now()}`,
                    furnitureType: ft,
                    position: { x: 0, y: 0 },
                    rotation: 0,
                    width:  ft === 'sofa' ? 200 : ft === 'bed' ? 160 : 80,
                    depth:  ft === 'sofa' ?  90 : ft === 'bed' ? 200 : 80,
                    height: 75,
                    type: 'furniture',
                    label: ft,
                  });
                }}
                className="text-xs px-2 py-1 rounded bg-[#2e2e4a] text-gray-300
                           hover:bg-blue-600/30 hover:text-white transition-colors capitalize"
              >
                {ft}
              </button>
            ))}
          </div>
        </Section>
      </aside>
    );
  }

  // ── Wall selected ─────────────────────────────────────────────────────────
  if (selection.type === 'wall' && wall) {
    const lenM = wallLengthMeters(wall).toFixed(2);
    return (
      <PanelShell title="Wall">
        <PropRow label="Length">{lenM} m</PropRow>
        <PropRow label="Thickness">
          <NumInput value={wall.thickness} onChange={v => updateWall(wall.id, { thickness: v })} />
          <span className="text-gray-500 text-xs ml-1">du</span>
        </PropRow>
        <PropRow label="Height">
          <NumInput value={wall.height} onChange={v => updateWall(wall.id, { height: v })} />
          <span className="text-gray-500 text-xs ml-1">du</span>
        </PropRow>

        <div className="border-t border-[#2e2e4a] pt-3 mt-1">
          <h4 className="text-xs font-semibold text-gray-500 uppercase tracking-wide mb-2">3D Material</h4>
          <div className="grid grid-cols-3 gap-1 mb-3">
            {WALL_PRESETS.map(p => (
              <button
                key={p.label}
                title={p.label}
                onClick={() => updateWall(wall.id, { color: p.color, roughness: p.roughness, polyhavenMaterial: undefined })}
                className={`flex flex-col items-center gap-1 p-1 rounded transition-colors
                  ${!wall.polyhavenMaterial && wall.color === p.color
                    ? 'ring-1 ring-blue-400 bg-blue-500/10'
                    : 'hover:bg-white/5'}`}
              >
                <div className="w-6 h-6 rounded border border-white/15"
                     style={{ backgroundColor: p.color }} />
                <span className="text-[9px] text-gray-400 leading-none">{p.label}</span>
              </button>
            ))}
          </div>
          <PropRow label="Color">
            <input
              type="color"
              value={wall.color ?? '#c8b89a'}
              onChange={e => updateWall(wall.id, { color: e.target.value })}
              className="w-8 h-6 rounded cursor-pointer border-0 bg-transparent"
            />
            <span className="text-xs text-gray-500">{wall.color ?? '#c8b89a'}</span>
          </PropRow>
          <PropRow label="Roughness">
            <input
              type="range" min="0" max="1" step="0.05"
              value={wall.roughness ?? 0.9}
              onChange={e => updateWall(wall.id, { roughness: Number(e.target.value) })}
              className="flex-1 accent-blue-400"
            />
            <span className="text-xs text-gray-500 w-7 text-right">
              {(wall.roughness ?? 0.9).toFixed(2)}
            </span>
          </PropRow>

          {/* ── PolyHaven material ── */}
          <div className="mt-3 pt-2 border-t border-[#1e1e2e]">
            {wall.polyhavenMaterial && (
              <div className="flex items-center gap-2 mb-2">
                <img
                  src={wall.polyhavenMaterial.thumbnailUrl}
                  alt=""
                  className="w-10 h-10 rounded object-cover flex-shrink-0 border border-white/10"
                />
                <div className="flex-1 min-w-0">
                  <p className="text-xs text-white leading-tight truncate">
                    {wall.polyhavenMaterial.name}
                  </p>
                  <p className="text-[10px] text-gray-500 mt-0.5">
                    PolyHaven · {wall.polyhavenMaterial.resolution}
                  </p>
                </div>
                <button
                  title="Remove PolyHaven material"
                  onClick={() => updateWall(wall.id, { polyhavenMaterial: undefined })}
                  className="text-gray-500 hover:text-red-400 px-1 transition-colors text-sm"
                >
                  ✕
                </button>
              </div>
            )}
            <button
              onClick={() => setPhOpen(true)}
              className="w-full py-1.5 rounded text-xs font-medium border border-[#3e3e5e]
                         text-gray-300 hover:text-white hover:border-blue-500/60
                         hover:bg-blue-600/10 transition-colors"
            >
              {wall.polyhavenMaterial ? '↺ Change PBR Material' : '+ Browse PolyHaven Materials'}
            </button>
          </div>
        </div>

        {phOpen && (
          <PolyhavenBrowser
            onApply={(mat: PolyhavenMaterial) => {
              updateWall(wall.id, { polyhavenMaterial: mat });
              setPhOpen(false);
            }}
            onClose={() => setPhOpen(false)}
          />
        )}

        {showDebug && (
          <div className="mt-2 p-2 bg-gray-900 rounded text-[10px] text-pink-300 font-mono break-all">
            {JSON.stringify({ id: wall.id, start: wall.start, end: wall.end }, null, 2)}
          </div>
        )}
        <DeleteBtn onClick={() => useEditorStore.getState().deleteWall(wall.id)} />
      </PanelShell>
    );
  }

  // ── Room selected ─────────────────────────────────────────────────────────
  if (selection.type === 'room' && room) {
    return (
      <PanelShell title="Room">
        <PropRow label="Name">
          <input
            value={room.name}
            onChange={e => updateRoom(room.id, { name: e.target.value })}
            className="prop-input"
          />
        </PropRow>
        <PropRow label="Area">{room.area.toFixed(2)} m²</PropRow>
        <PropRow label="Vertices">{room.polygon.length}</PropRow>
      </PanelShell>
    );
  }

  // ── Door selected ─────────────────────────────────────────────────────────
  if (selection.type === 'door' && door) {
    return (
      <PanelShell title="Door">
        <PropRow label="Width">
          <NumInput value={door.width} onChange={v => updateDoor(door.id, { width: v })} />
          <span className="text-gray-500 text-xs ml-1">mm</span>
        </PropRow>
        <PropRow label="Height">
          <NumInput value={door.height} onChange={v => updateDoor(door.id, { height: v })} />
          <span className="text-gray-500 text-xs ml-1">mm</span>
        </PropRow>
        <PropRow label="Position">
          <NumInput
            value={Math.round(door.positionOnWall * 100)}
            onChange={v => updateDoor(door.id, { positionOnWall: v / 100 })}
          />
          <span className="text-gray-500 text-xs ml-1">%</span>
        </PropRow>
        <DeleteBtn onClick={() => useEditorStore.getState().deleteDoor(door.id)} />
      </PanelShell>
    );
  }

  // ── Window selected ───────────────────────────────────────────────────────
  if (selection.type === 'window' && win) {
    return (
      <PanelShell title="Window">
        <PropRow label="Width">
          <NumInput value={win.width} onChange={v => updateWindow(win.id, { width: v })} />
          <span className="text-gray-500 text-xs ml-1">mm</span>
        </PropRow>
        <PropRow label="Height">
          <NumInput value={win.height} onChange={v => updateWindow(win.id, { height: v })} />
          <span className="text-gray-500 text-xs ml-1">mm</span>
        </PropRow>
        <PropRow label="Sill H.">
          <NumInput value={win.sillHeight} onChange={v => updateWindow(win.id, { sillHeight: v })} />
          <span className="text-gray-500 text-xs ml-1">mm</span>
        </PropRow>
        <PropRow label="Position">
          <NumInput
            value={Math.round(win.positionOnWall * 100)}
            onChange={v => updateWindow(win.id, { positionOnWall: v / 100 })}
          />
          <span className="text-gray-500 text-xs ml-1">%</span>
        </PropRow>
        <DeleteBtn onClick={() => useEditorStore.getState().deleteWindow(win.id)} />
      </PanelShell>
    );
  }

  // ── Furniture selected ────────────────────────────────────────────────────
  if (selection.type === 'furniture' && furniture) {
    return (
      <PanelShell title="Furniture">
        <PropRow label="Type">
          <select
            value={furniture.furnitureType}
            onChange={e => updateFurniture(furniture.id, { furnitureType: e.target.value })}
            className="prop-input"
          >
            {FURNITURE_TYPES.map(ft => <option key={ft} value={ft}>{ft}</option>)}
          </select>
        </PropRow>
        <PropRow label="Label">
          <input
            value={furniture.label ?? ''}
            onChange={e => updateFurniture(furniture.id, { label: e.target.value })}
            className="prop-input"
          />
        </PropRow>
        <PropRow label="Width">
          <NumInput value={furniture.width} onChange={v => updateFurniture(furniture.id, { width: v })} />
        </PropRow>
        <PropRow label="Depth">
          <NumInput value={furniture.depth} onChange={v => updateFurniture(furniture.id, { depth: v })} />
        </PropRow>
        <PropRow label="Rotation">
          <NumInput value={furniture.rotation} onChange={v => updateFurniture(furniture.id, { rotation: v })} />
          <span className="text-gray-500 text-xs ml-1">°</span>
        </PropRow>

        {showDebug && (
          <div className="mt-2 p-2 bg-gray-900 rounded text-[10px] text-pink-300 font-mono break-all">
            {JSON.stringify(furniture, null, 2)}
          </div>
        )}
        <DeleteBtn onClick={() => useEditorStore.getState().deleteFurniture(furniture.id)} />
      </PanelShell>
    );
  }

  return null;
}

// ── Sub-components ────────────────────────────────────────────────────────────

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div className="p-3 border-b border-[#2e2e4a]">
      <h4 className="text-xs font-semibold text-gray-500 uppercase tracking-wide mb-2">{title}</h4>
      {children}
    </div>
  );
}

function PanelShell({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <aside className="w-64 flex-shrink-0 bg-[#13131f] border-l border-[#2e2e4a] flex flex-col overflow-y-auto">
      <div className="p-3 border-b border-[#2e2e4a]">
        <h3 className="text-xs font-semibold text-gray-400 uppercase tracking-wide">{title}</h3>
      </div>
      <div className="p-3 flex flex-col gap-3">{children}</div>
    </aside>
  );
}

function PropRow({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="flex items-center gap-2">
      <span className="text-xs text-gray-400 w-20 flex-shrink-0">{label}</span>
      <div className="flex items-center gap-1 flex-1">{children}</div>
    </div>
  );
}

function NumInput({ value, onChange, min, step }: {
  value: number;
  onChange: (v: number) => void;
  min?: number;
  step?: number;
}) {
  return (
    <input
      type="number"
      value={value}
      min={min ?? 0}
      step={step ?? 1}
      onChange={e => onChange(Number(e.target.value))}
      className="w-20 bg-[#1e1e2e] border border-[#2e2e4a] rounded px-2 py-1
                 text-xs text-white focus:outline-none focus:border-blue-400"
    />
  );
}

function DeleteBtn({ onClick }: { onClick: () => void }) {
  return (
    <button
      onClick={onClick}
      className="mt-2 w-full py-1.5 rounded text-xs font-medium
                 bg-red-900/30 text-red-400 hover:bg-red-600/30 hover:text-red-300
                 border border-red-900/50 transition-colors"
    >
      Delete
    </button>
  );
}
