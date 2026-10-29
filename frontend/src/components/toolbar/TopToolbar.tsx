'use client';
import { useRef, useState } from 'react';
import { useEditorStore } from '@/store/useEditorStore';
import FloorConfigModal, { type FloorConfig } from './FloorConfigModal';
import { genId, areaScaleFactor, scaleFloorGeometry } from '@/utils/geometry';
import type { Floor, Room, Wall } from '@/types';

const BACKENDS = {
  cubicasa: { label: 'CubiCasa',   url: 'http://localhost:8003/analyze', color: 'bg-green-600/80 border-green-400/40 hover:bg-green-500' },
  mrcnn:    { label: 'Mask R-CNN', url: 'http://localhost:8001/analyze', color: 'bg-purple-600/80 border-purple-400/40 hover:bg-purple-500' },
} as const;

type Backend = keyof typeof BACKENDS;

export default function TopToolbar() {
  const viewMode  = useEditorStore(s => s.view.mode);
  const history   = useEditorStore(s => s.history);
  const histIdx   = useEditorStore(s => s.historyIndex);
  const projName  = useEditorStore(s => s.project.name);
  const showDebug = useEditorStore(s => s.showDebug);
  const importedGlbUrl = useEditorStore(s => s.importedGlbUrl);
  const bgImageDataUrl = useEditorStore(s => s.bgImageDataUrl);
  const bgImageOpacity = useEditorStore(s => s.bgImageOpacity);
  const floors         = useEditorStore(s => s.project.floors);
  const activeFloorId  = useEditorStore(s => s.project.activeFloorId);

  const {
    setViewMode, undo, redo,
    saveToLocalStorage, loadFromLocalStorage,
    exportJSON, importJSON, newProject, setProjectName, toggleDebug,
    setImportedGlbUrl, setBgImage, setBgImageOpacity, importPipelineWalls,
    importPipelineWallsToFloor, setActiveFloor, updateFloor,
    setFloorBgImage,
  } = useEditorStore();

  const fileRef = useRef<HTMLInputElement>(null);
  const glbRef  = useRef<HTMLInputElement>(null);
  const pngRef  = useRef<HTMLInputElement>(null);

  const [backend,  setBackend]  = useState<Backend>('cubicasa');
  const [analyzing, setAnalyzing] = useState(false);
  const [analyzeErr, setAnalyzeErr] = useState('');
  const [pendingFiles, setPendingFiles] = useState<File[] | null>(null); // multi-floor modal

  // ── Single-floor analysis ────────────────────────────────────────────────────
  const analyzeSingleFile = async (file: File, floorId?: string) => {
    const dataUrl = await new Promise<string>(res => {
      const reader = new FileReader();
      reader.onload = ev => res(ev.target?.result as string);
      reader.readAsDataURL(file);
    });

    if (floorId) setFloorBgImage(floorId, dataUrl);
    else setBgImage(dataUrl);

    const form = new FormData();
    form.append('file', file, file.name);
    const response = await fetch(BACKENDS[backend].url, { method: 'POST', body: form });
    if (!response.ok) {
      const err = await response.json().catch(() => ({ error: `HTTP ${response.status}` }));
      throw new Error(err.error ?? `HTTP ${response.status}`);
    }
    return response.json();
  };

  // ── File input change handler ────────────────────────────────────────────────
  // Always shows the config modal (1 file or many) so a Total Area override
  // is available regardless of how many images you upload at once — a
  // single-floor upload used to skip straight to analysis with no chance to
  // correct its scale.
  const handleAnalyzePng = async (e: React.ChangeEvent<HTMLInputElement>) => {
    const files = Array.from(e.target.files ?? []);
    e.target.value = '';
    if (files.length === 0) return;
    setAnalyzeErr('');
    setPendingFiles(files);
  };

  // Applies a Total Area override (if the user entered one) to a backend
  // analysis result before it's imported. Returns the walls/rooms unchanged
  // when no area was entered or the current detected area is unusable.
  const applyAreaOverride = (
    walls: Wall[], rooms: Room[], areaM2: number | undefined,
  ): { walls: Wall[]; rooms: Room[] } => {
    if (!areaM2) return { walls, rooms };
    const factor = areaScaleFactor(rooms, areaM2);
    return factor ? scaleFloorGeometry(walls, rooms, factor) : { walls, rooms };
  };

  // ── Floor config confirm ─────────────────────────────────────────────────────
  const handleFloorConfigConfirm = async (configs: FloorConfig[]) => {
    setPendingFiles(null);
    setAnalyzeErr('');
    setAnalyzing(true);

    if (configs.length === 1) {
      // Single floor: analyze into the current active floor — same target
      // as the old immediate-analyze flow, just with a confirm step first
      // for ceiling height and the optional area override.
      const cfg = configs[0];
      try {
        const data = await analyzeSingleFile(cfg.file);
        if (Array.isArray(data.walls) && data.walls.length > 0) {
          const { walls, rooms } = applyAreaOverride(data.walls, data.rooms ?? [], cfg.areaM2);
          importPipelineWalls(walls, data.doors ?? [], data.windows ?? [], rooms);
          updateFloor(activeFloorId, { name: cfg.name, floorHeight: cfg.height });
        }
        setViewMode('3d');
      } catch (err: unknown) {
        setAnalyzeErr(`Pipeline error: ${err instanceof Error ? err.message : String(err)}`);
      } finally {
        setAnalyzing(false);
      }
      return;
    }

    // Multiple floors: build new empty floors with user-configured names/heights
    const newFloors: Floor[] = configs.map((cfg, i) => ({
      id: genId('floor'),
      name: cfg.name,
      level: i,
      floorHeight: cfg.height,
      walls: [], rooms: [], doors: [], windows: [], furniture: [],
    }));

    // Rebuild project with these floors (spread current project to keep name/version/metadata)
    const currentProject = JSON.parse(exportJSON());
    importJSON(JSON.stringify({
      ...currentProject,
      floors: newFloors,
      activeFloorId: newFloors[0].id,
    }));

    // Analyze each floor image sequentially, import results into the correct floor
    try {
      for (let i = 0; i < configs.length; i++) {
        const floorId = newFloors[i].id;
        const data = await analyzeSingleFile(configs[i].file, floorId);
        if (Array.isArray(data.walls) && data.walls.length > 0) {
          const { walls, rooms } = applyAreaOverride(data.walls, data.rooms ?? [], configs[i].areaM2);
          importPipelineWallsToFloor(floorId, walls, data.doors ?? [], data.windows ?? [], rooms);
          // Restore floor metadata that importPipelineWallsToFloor doesn't touch
          updateFloor(floorId, { name: configs[i].name, level: i, floorHeight: configs[i].height });
        }
      }
      setActiveFloor(newFloors[0].id);
      setViewMode('3d');
    } catch (err: unknown) {
      setAnalyzeErr(`Pipeline error: ${err instanceof Error ? err.message : String(err)}`);
    } finally {
      setAnalyzing(false);
    }
  };

  const handleExport = () => {
    const json = exportJSON();
    const blob = new Blob([json], { type: 'application/json' });
    const url  = URL.createObjectURL(blob);
    const a    = document.createElement('a');
    a.href = url; a.download = `${projName.replace(/\s+/g, '_')}.json`; a.click();
    URL.revokeObjectURL(url);
  };

  const handleImportJson = (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0]; if (!file) return;
    const reader = new FileReader();
    reader.onload = ev => importJSON(ev.target?.result as string);
    reader.readAsText(file); e.target.value = '';
  };

  const handleGlbImport = (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0]; if (!file) return;
    if (importedGlbUrl) URL.revokeObjectURL(importedGlbUrl);
    setImportedGlbUrl(URL.createObjectURL(file)); e.target.value = '';
  };

  const canUndo = histIdx >= 0;
  const canRedo = histIdx < history.length - 1;
  const multiFloor = floors.length > 1;

  return (
    <>
      {/* ── Floor Config Modal ── */}
      {pendingFiles && (
        <FloorConfigModal
          files={pendingFiles}
          onConfirm={handleFloorConfigConfirm}
          onCancel={() => setPendingFiles(null)}
        />
      )}

      <header className="flex items-center gap-2 px-3 h-12 bg-[#13131f] border-b border-[#2e2e4a] flex-shrink-0 z-10 overflow-x-auto">
        {/* Logo */}
        <span className="text-blue-400 font-bold text-sm tracking-wide mr-1 flex-shrink-0">PlanLift</span>

        {/* Project name */}
        <input
          value={projName}
          onChange={e => setProjectName(e.target.value)}
          className="bg-transparent border-b border-transparent hover:border-gray-600
                     focus:border-blue-400 outline-none text-sm text-gray-200 w-28
                     transition-colors px-1 flex-shrink-0"
        />

        <div className="w-px h-6 bg-[#2e2e4a] mx-1 flex-shrink-0" />

        {/* ── Floor selector dropdown (shown when 2+ floors exist) ── */}
        {multiFloor && (
          <>
            <div className="flex items-center gap-1.5 flex-shrink-0">
              <span className="text-[10px] text-gray-500 uppercase tracking-wide">Floor</span>
              <select
                value={activeFloorId}
                onChange={e => setActiveFloor(e.target.value)}
                className="bg-[#1e1e2e] border border-[#2e2e4a] rounded text-xs text-white px-2 py-1
                           outline-none focus:border-blue-500 cursor-pointer"
              >
                {floors.map(f => (
                  <option key={f.id} value={f.id}>
                    {f.name} {f.floorHeight ? `(${f.floorHeight} cm)` : ''}
                  </option>
                ))}
              </select>
            </div>
            <div className="w-px h-6 bg-[#2e2e4a] mx-1 flex-shrink-0" />
          </>
        )}

        {/* View toggle */}
        <div className="flex rounded-lg overflow-hidden border border-[#2e2e4a] flex-shrink-0">
          {(['2d', '3d'] as const).map(m => (
            <button key={m} onClick={() => setViewMode(m)}
              className={`px-3 py-1 text-xs font-semibold transition-colors
                ${viewMode === m ? 'bg-blue-600 text-white' : 'bg-[#1e1e2e] text-gray-400 hover:text-white'}`}>
              {m.toUpperCase()}
            </button>
          ))}
        </div>

        <div className="w-px h-6 bg-[#2e2e4a] mx-1 flex-shrink-0" />

        {/* ── Analyze Floor Plan ── */}
        <div className="flex items-center gap-1 flex-shrink-0">
          {/* Backend selector */}
          <div className="flex rounded overflow-hidden border border-[#2e2e4a] flex-shrink-0"
               title="Choose AI backend">
            {(Object.keys(BACKENDS) as Backend[]).map(b => (
              <button key={b} onClick={() => setBackend(b)} disabled={analyzing}
                className={`px-2 py-1 text-[10px] font-semibold transition-colors
                  ${backend === b
                    ? (b === 'cubicasa' ? 'bg-green-700 text-white' : 'bg-purple-700 text-white')
                    : 'bg-[#1e1e2e] text-gray-500 hover:text-gray-300'}`}>
                {BACKENDS[b].label}
              </button>
            ))}
          </div>

          {/* Analyze button */}
          <button
            onClick={() => pngRef.current?.click()}
            disabled={analyzing}
            title="Upload floor plan image(s) → analyze → load into editor. Select multiple images for multi-floor buildings."
            className={`flex items-center gap-1.5 px-3 py-1 rounded text-xs font-semibold
              transition-colors border flex-shrink-0 text-white
              ${analyzing
                ? 'bg-yellow-600/40 text-yellow-300 border-yellow-500/40 cursor-wait'
                : BACKENDS[backend].color}`}
          >
            <span>{analyzing ? '⏳' : '🏠'}</span>
            <span>{analyzing ? 'Analyzing…' : 'Analyze Floor Plan'}</span>
          </button>
          {/* multiple allows selecting several images at once */}
          <input ref={pngRef} type="file" accept=".png,.jpg,.jpeg,.bmp,.tiff"
            multiple className="hidden" onChange={handleAnalyzePng} />

          {analyzeErr && (
            <span className="text-xs text-red-400 max-w-[160px] truncate" title={analyzeErr}>
              ⚠ {analyzeErr}
            </span>
          )}
        </div>

        {/* Background trace opacity (2D only) */}
        {viewMode === '2d' && bgImageDataUrl && (
          <div className="flex items-center gap-1 flex-shrink-0">
            <span className="text-xs text-gray-500">Trace:</span>
            <input type="range" min="0" max="1" step="0.05"
              value={bgImageOpacity}
              onChange={e => setBgImageOpacity(Number(e.target.value))}
              className="w-16 accent-blue-400" title="Background image opacity" />
            <button onClick={() => setBgImage(null)}
              className="text-xs text-gray-500 hover:text-red-400" title="Remove background">✕</button>
          </div>
        )}

        <div className="w-px h-6 bg-[#2e2e4a] mx-1 flex-shrink-0" />

        {/* Undo / Redo */}
        <button onClick={undo} disabled={!canUndo} title="Undo (Ctrl+Z)" className="topbtn disabled:opacity-30 flex-shrink-0">↩</button>
        <button onClick={redo} disabled={!canRedo} title="Redo (Ctrl+Y)" className="topbtn disabled:opacity-30 flex-shrink-0">↪</button>

        <div className="w-px h-6 bg-[#2e2e4a] mx-1 flex-shrink-0" />

        <button onClick={saveToLocalStorage}   title="Save"   className="topbtn flex-shrink-0">💾</button>
        <button onClick={loadFromLocalStorage}  title="Load"   className="topbtn flex-shrink-0">📂</button>

        <div className="w-px h-6 bg-[#2e2e4a] mx-1 flex-shrink-0" />

        <button onClick={handleExport}                    title="Export project JSON" className="topbtn text-xs px-2 flex-shrink-0">Export</button>
        <button onClick={() => fileRef.current?.click()}  title="Import project JSON" className="topbtn text-xs px-2 flex-shrink-0">Import</button>
        <input ref={fileRef} type="file" accept=".json" className="hidden" onChange={handleImportJson} />

        <div className="w-px h-6 bg-[#2e2e4a] mx-1 flex-shrink-0" />

        {/* Manual GLB */}
        <button onClick={() => glbRef.current?.click()}
          title="Manually load a GLB file into the 3D viewer"
          className={`topbtn text-xs px-2 flex-shrink-0 ${importedGlbUrl ? 'text-green-400' : ''}`}>
          {importedGlbUrl ? 'GLB ✓' : 'Load GLB'}
        </button>
        {importedGlbUrl && (
          <button onClick={() => { URL.revokeObjectURL(importedGlbUrl); setImportedGlbUrl(null); }}
            className="topbtn text-xs px-1 text-red-400 flex-shrink-0" title="Remove GLB">✕</button>
        )}
        <input ref={glbRef} type="file" accept=".glb,.gltf" className="hidden" onChange={handleGlbImport} />

        <div className="w-px h-6 bg-[#2e2e4a] mx-1 flex-shrink-0" />

        <button onClick={newProject} title="New project" className="topbtn text-xs px-2 flex-shrink-0">New</button>

        <div className="flex-1" />

        <button onClick={toggleDebug}
          className={`topbtn text-xs px-2 flex-shrink-0 ${showDebug ? 'text-pink-400' : ''}`}>
          Debug
        </button>
      </header>
    </>
  );
}
