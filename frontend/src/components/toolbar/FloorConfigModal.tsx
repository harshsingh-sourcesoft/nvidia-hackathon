'use client';
import { useState } from 'react';
import { sqFtToSqM } from '@/utils/geometry';

export interface FloorConfig {
  name: string;
  height: number; // cm (design-units = cm here, 270 = 2.7 m)
  file: File;
  /** Optional total-floor-area override, already converted to m². Undefined
   * when the field was left empty — callers should fall back to whatever
   * scale the pipeline detected instead of rescaling. */
  areaM2?: number;
}

interface RowState {
  name: string;
  height: number;
  file: File;
  areaValue: string;   // raw text input; '' = no override
  areaUnit: 'm2' | 'ft2';
}

interface Props {
  files: File[];
  onConfirm: (configs: FloorConfig[]) => void;
  onCancel: () => void;
}

export default function FloorConfigModal({ files, onConfirm, onCancel }: Props) {
  const [rows, setRows] = useState<RowState[]>(
    files.map((f, i) => ({
      name: i === 0 ? 'Ground Floor' : `Floor ${i}`,
      height: 270,
      file: f,
      areaValue: '',
      areaUnit: 'm2',
    }))
  );

  const update = <K extends keyof RowState>(i: number, key: K, value: RowState[K]) =>
    setRows(prev => prev.map((r, j) => j === i ? { ...r, [key]: value } : r));

  const handleConfirm = () => {
    const configs: FloorConfig[] = rows.map(r => {
      const raw = parseFloat(r.areaValue);
      const areaM2 = Number.isFinite(raw) && raw > 0
        ? (r.areaUnit === 'ft2' ? sqFtToSqM(raw) : raw)
        : undefined;
      return { name: r.name, height: r.height, file: r.file, areaM2 };
    });
    onConfirm(configs);
  };

  return (
    <div className="fixed inset-0 bg-black/70 flex items-center justify-center z-50 backdrop-blur-sm">
      <div className="bg-[#1e1e2e] border border-[#2e2e4a] rounded-xl w-[520px] max-h-[80vh] flex flex-col shadow-2xl">

        {/* Header */}
        <div className="px-6 py-4 border-b border-[#2e2e4a]">
          <h2 className="text-white font-semibold text-base">Configure Building Floors</h2>
          <p className="text-gray-400 text-xs mt-1">
            {files.length} floor plan image{files.length > 1 ? 's' : ''} detected — set the name, ceiling height, and (optionally) the real total area for each floor.
          </p>
        </div>

        {/* Floor rows */}
        <div className="overflow-y-auto flex-1 px-6 py-4 space-y-3">
          {rows.map((row, i) => (
            <div key={i} className="bg-[#13131f] rounded-lg p-3 border border-[#2e2e4a]">
              <div className="flex items-center gap-2 mb-2.5">
                <span className="text-[10px] font-bold text-blue-400 bg-blue-500/15 px-2 py-0.5 rounded uppercase tracking-wide">
                  {i === 0 ? 'Ground' : `Level ${i}`}
                </span>
                <span className="text-gray-500 text-xs truncate flex-1">{row.file.name}</span>
              </div>
              <div className="flex gap-3">
                <div className="flex-1">
                  <label className="text-[10px] text-gray-400 uppercase tracking-wide block mb-1">Floor Name</label>
                  <input
                    value={row.name}
                    onChange={e => update(i, 'name', e.target.value)}
                    className="w-full bg-[#0d0d1a] border border-[#2e2e4a] rounded px-2 py-1.5 text-sm text-white
                               outline-none focus:border-blue-500 transition-colors"
                  />
                </div>
                <div className="w-32">
                  <label className="text-[10px] text-gray-400 uppercase tracking-wide block mb-1">Ceiling Height</label>
                  <div className="relative">
                    <input
                      type="number"
                      min={150} max={600} step={10}
                      value={row.height}
                      onChange={e => update(i, 'height', Number(e.target.value))}
                      className="w-full bg-[#0d0d1a] border border-[#2e2e4a] rounded px-2 py-1.5 pr-8 text-sm text-white
                                 outline-none focus:border-blue-500 transition-colors"
                    />
                    <span className="absolute right-2 top-1/2 -translate-y-1/2 text-[10px] text-gray-500">cm</span>
                  </div>
                </div>
              </div>
              <div className="mt-2.5">
                <label className="text-[10px] text-gray-400 uppercase tracking-wide block mb-1">
                  Total Floor Area <span className="normal-case text-gray-600">(optional — leave blank to keep detected size)</span>
                </label>
                <div className="flex gap-2">
                  <input
                    type="number"
                    min={0} step={1}
                    placeholder="e.g. 10000"
                    value={row.areaValue}
                    onChange={e => update(i, 'areaValue', e.target.value)}
                    className="flex-1 bg-[#0d0d1a] border border-[#2e2e4a] rounded px-2 py-1.5 text-sm text-white
                               outline-none focus:border-blue-500 transition-colors"
                  />
                  <select
                    value={row.areaUnit}
                    onChange={e => update(i, 'areaUnit', e.target.value as 'm2' | 'ft2')}
                    className="bg-[#0d0d1a] border border-[#2e2e4a] rounded px-2 py-1.5 text-sm text-white
                               outline-none focus:border-blue-500 transition-colors"
                  >
                    <option value="m2">m²</option>
                    <option value="ft2">ft²</option>
                  </select>
                </div>
              </div>
            </div>
          ))}
        </div>

        {/* Footer */}
        <div className="px-6 py-4 border-t border-[#2e2e4a] flex justify-end gap-3">
          <button
            onClick={onCancel}
            className="px-4 py-2 rounded text-sm text-gray-400 hover:text-white hover:bg-white/8 transition-colors"
          >
            Cancel
          </button>
          <button
            onClick={handleConfirm}
            className="px-5 py-2 rounded text-sm font-semibold bg-blue-600 text-white
                       hover:bg-blue-500 transition-colors flex items-center gap-2"
          >
            <span>🏗</span>
            <span>Configure &amp; Analyze</span>
          </button>
        </div>
      </div>
    </div>
  );
}
