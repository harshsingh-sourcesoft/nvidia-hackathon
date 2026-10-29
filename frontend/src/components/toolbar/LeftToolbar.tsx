'use client';
import { useEditorStore } from '@/store/useEditorStore';
import type { EditorTool } from '@/types';

interface ToolDef {
  tool:  EditorTool;
  label: string;
  icon:  string;
  shortcut?: string;
}

const TOOLS: ToolDef[] = [
  { tool: 'select',    label: 'Select',    icon: '↖',  shortcut: 'V' },
  { tool: 'wall',      label: 'Wall',      icon: '▬',  shortcut: 'W' },
  { tool: 'door',      label: 'Door',      icon: '🚪', shortcut: 'D' },
  { tool: 'window',    label: 'Window',    icon: '⬜', shortcut: 'N' },
  { tool: 'furniture', label: 'Furniture', icon: '🪑', shortcut: 'F' },
  { tool: 'pan',       label: 'Pan',       icon: '✋', shortcut: 'H' },
  { tool: 'delete',    label: 'Delete',    icon: '🗑',  shortcut: 'X' },
];

export default function LeftToolbar() {
  const tool    = useEditorStore(s => s.tool);
  const setTool = useEditorStore(s => s.setTool);

  // Keyboard shortcuts
  if (typeof window !== 'undefined') {
    // handled in FloorplanEditor keydown
  }

  return (
    <aside className="flex flex-col items-center gap-1 px-1 py-2 bg-[#13131f] border-r border-[#2e2e4a] w-14 flex-shrink-0">
      {TOOLS.map(({ tool: t, label, icon, shortcut }) => (
        <button
          key={t}
          title={`${label}${shortcut ? ` (${shortcut})` : ''}`}
          onClick={() => setTool(t)}
          className={`
            flex flex-col items-center justify-center w-10 h-10 rounded-lg text-xs gap-0.5
            transition-colors cursor-pointer select-none
            ${tool === t
              ? 'bg-blue-500/25 text-blue-400 ring-1 ring-blue-500/50'
              : 'text-gray-400 hover:text-white hover:bg-white/8'}
          `}
        >
          <span className="text-lg leading-none">{icon}</span>
          <span className="text-[9px] leading-none">{label}</span>
        </button>
      ))}

      <div className="flex-1" />

      {/* Detect rooms button */}
      <button
        title="Auto-detect rooms"
        onClick={() => useEditorStore.getState().runRoomDetection()}
        className="flex flex-col items-center justify-center w-10 h-10 rounded-lg text-xs gap-0.5
                   text-gray-400 hover:text-green-400 hover:bg-green-500/10 transition-colors"
      >
        <span className="text-lg leading-none">⬡</span>
        <span className="text-[9px] leading-none">Rooms</span>
      </button>
    </aside>
  );
}
