'use client';
import { useState, useEffect, useCallback, useMemo } from 'react';
import type { PolyhavenMaterial } from '@/types';

const PH_API = 'https://api.polyhaven.com';
const PH_CDN = 'https://cdn.polyhaven.com';

type Resolution = '1k' | '2k';

interface AssetEntry {
  name: string;
  categories: string[];
  tags: string[];
}
type AssetList = Record<string, AssetEntry>;

interface Props {
  onApply: (material: PolyhavenMaterial) => void;
  onClose: () => void;
}

export default function PolyhavenBrowser({ onApply, onClose }: Props) {
  const [assets, setAssets]         = useState<AssetList>({});
  const [loadingList, setLoading]   = useState(true);
  const [fetchError, setFetchError] = useState<string | null>(null);
  const [search, setSearch]         = useState('');
  const [category, setCategory]     = useState('');
  const [resolution, setResolution] = useState<Resolution>('1k');

  const [selectedId, setSelectedId]     = useState<string | null>(null);
  const [selectedName, setSelectedName] = useState('');

  const [applying, setApplying]   = useState(false);
  const [applyError, setApplyError] = useState<string | null>(null);

  useEffect(() => {
    fetch(`${PH_API}/assets?t=textures`)
      .then(r => r.json())
      .then((data: AssetList) => { setAssets(data); setLoading(false); })
      .catch(() => {
        setFetchError('Could not reach PolyHaven — check your internet connection.');
        setLoading(false);
      });
  }, []);

  const categories = useMemo(() => {
    const counts: Record<string, number> = {};
    Object.values(assets).forEach(a =>
      a.categories?.forEach(c => { counts[c] = (counts[c] ?? 0) + 1; }),
    );
    return Object.entries(counts).sort((a, b) => b[1] - a[1]).map(([c]) => c);
  }, [assets]);

  const filtered = useMemo(() => {
    const q = search.toLowerCase();
    return Object.entries(assets).filter(([id, a]) => {
      const matchCat    = !category || a.categories?.includes(category);
      const matchSearch =
        !q ||
        id.toLowerCase().includes(q) ||
        a.name.toLowerCase().includes(q) ||
        a.tags?.some(t => t.toLowerCase().includes(q));
      return matchCat && matchSearch;
    });
  }, [assets, search, category]);

  const handleApply = useCallback(async () => {
    if (!selectedId || !selectedName) return;
    setApplying(true);
    setApplyError(null);

    try {
      const res = await fetch(`${PH_API}/files/${selectedId}`);
      if (!res.ok) throw new Error(`PolyHaven API error: ${res.status}`);
      const files = await res.json();

      // Actual PolyHaven structure: { MapName: { resolution: { format: { url, size, md5 } } } }
      // e.g. files["Diffuse"]["1k"]["jpg"]["url"]
      type FormatMap = Record<string, { url: string; size: number; md5: string }>;
      type ResMap    = Record<string, FormatMap>;
      type FileMap   = Record<string, ResMap>;
      const f = files as FileMap;

      // Pick the best URL for a given map name (tries preferred resolution first)
      const pickUrl = (...mapNames: string[]): string | undefined => {
        for (const name of mapNames) {
          const resMap = f[name];
          if (!resMap) continue;
          const resData: FormatMap = resMap[resolution] ?? resMap['1k'] ?? resMap[Object.keys(resMap)[0]];
          if (!resData) continue;
          const url = resData['jpg']?.url ?? resData['png']?.url;
          if (url) return url;
        }
        return undefined;
      };

      const diffuseUrl   = pickUrl('Diffuse', 'diffuse', 'Color', 'color');
      const roughnessUrl = pickUrl('Rough', 'rough', 'Roughness', 'roughness');
      // nor_gl = OpenGL convention — matches Three.js; nor_dx = DirectX, skip
      const normalUrl    = pickUrl('nor_gl', 'Normal (GL)', 'Normal_GL', 'Normal');

      if (!diffuseUrl) {
        throw new Error(
          `No diffuse map. Available keys: [${Object.keys(f).join(', ')}]`
        );
      }

      onApply({
        id: selectedId,
        name: selectedName,
        thumbnailUrl: `${PH_CDN}/asset_img/thumbs/${selectedId}.png?width=200`,
        diffuseUrl,
        roughnessUrl,
        normalUrl,
        resolution,
      });
    } catch (e) {
      const msg = e instanceof Error ? e.message : String(e);
      console.error('[PH] apply failed:', e);
      setApplyError(msg);
    } finally {
      setApplying(false);
    }
  }, [selectedId, selectedName, resolution, onApply]);

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/70 backdrop-blur-sm"
      onClick={e => { if (e.target === e.currentTarget) onClose(); }}
    >
      <div
        className="w-[840px] max-w-[95vw] max-h-[88vh] bg-[#0e0e1a] border border-[#2e2e4a]
                   rounded-xl flex flex-col shadow-2xl overflow-hidden"
      >
        {/* ── Header ── */}
        <div className="flex items-center justify-between px-5 py-3 border-b border-[#2e2e4a]">
          <div>
            <span className="text-sm font-semibold text-white">PolyHaven Material Browser</span>
            <span className="ml-2 text-xs text-gray-500">free CC0 PBR textures</span>
          </div>
          <div className="flex items-center gap-3">
            <div className="flex gap-0.5 bg-[#1a1a2e] rounded-lg p-0.5">
              {(['1k', '2k'] as Resolution[]).map(r => (
                <button
                  key={r}
                  onClick={() => setResolution(r)}
                  className={`px-3 py-0.5 rounded-md text-xs font-medium transition-colors
                    ${resolution === r ? 'bg-blue-600 text-white' : 'text-gray-400 hover:text-white'}`}
                >
                  {r}
                </button>
              ))}
            </div>
            <button
              onClick={onClose}
              className="w-7 h-7 flex items-center justify-center rounded
                         text-gray-400 hover:text-white hover:bg-white/5 transition-colors text-lg"
            >
              ✕
            </button>
          </div>
        </div>

        {/* ── Filters ── */}
        <div className="flex items-center gap-2 px-4 py-2 border-b border-[#2e2e4a]">
          <input
            type="search"
            placeholder="Search by name or tag…"
            value={search}
            onChange={e => { setSearch(e.target.value); setApplyError(null); }}
            autoFocus
            className="flex-1 bg-[#1a1a2e] border border-[#2e2e4a] rounded-md px-3 py-1.5
                       text-sm text-white placeholder-gray-600
                       focus:outline-none focus:border-blue-500"
          />
          <select
            value={category}
            onChange={e => setCategory(e.target.value)}
            className="bg-[#1a1a2e] border border-[#2e2e4a] rounded-md px-2 py-1.5
                       text-sm text-gray-300 focus:outline-none focus:border-blue-500 max-w-[140px]"
          >
            <option value="">All categories</option>
            {categories.map(c => <option key={c} value={c}>{c}</option>)}
          </select>
          {!loadingList && (
            <span className="text-xs text-gray-600 whitespace-nowrap">
              {filtered.length} / {Object.keys(assets).length}
            </span>
          )}
        </div>

        {/* ── Grid ── */}
        <div className="overflow-y-auto flex-1 p-3">
          {loadingList ? (
            <div className="flex flex-col items-center justify-center h-60 gap-3">
              <div className="w-8 h-8 border-2 border-blue-400 border-t-transparent rounded-full animate-spin" />
              <p className="text-sm text-gray-500">Loading PolyHaven library…</p>
            </div>
          ) : fetchError ? (
            <div className="flex items-center justify-center h-60 text-red-400 text-sm px-8 text-center">
              {fetchError}
            </div>
          ) : filtered.length === 0 ? (
            <div className="flex items-center justify-center h-60 text-gray-500 text-sm">
              No materials match
            </div>
          ) : (
            <div className="grid grid-cols-4 gap-2">
              {filtered.map(([id, asset]) => (
                <MaterialCard
                  key={id}
                  id={id}
                  name={asset.name}
                  selected={selectedId === id}
                  onSelect={() => { setSelectedId(id); setSelectedName(asset.name); setApplyError(null); }}
                />
              ))}
            </div>
          )}
        </div>

        {/* ── Footer ── */}
        <div className="border-t border-[#2e2e4a] bg-[#0a0a14]">
          {/* Error row */}
          {applyError && (
            <div className="px-4 py-2 bg-red-950/60 border-b border-red-900/40
                            text-xs text-red-300 flex items-start gap-2">
              <span className="text-red-400 flex-shrink-0 mt-0.5">⚠</span>
              <span className="break-all">{applyError}</span>
            </div>
          )}

          {/* Apply bar */}
          <div className="flex items-center gap-3 px-4 py-3">
            {selectedId ? (
              <>
                <img
                  src={`${PH_CDN}/asset_img/thumbs/${selectedId}.png?width=80`}
                  alt=""
                  className="w-9 h-9 rounded object-cover flex-shrink-0 border border-white/10"
                />
                <div className="flex-1 min-w-0">
                  <p className="text-xs text-white truncate font-medium">{selectedName}</p>
                  <p className="text-[10px] text-gray-500">{resolution} · PBR</p>
                </div>
              </>
            ) : (
              <p className="flex-1 text-xs text-gray-600 italic">
                Click a material above to select it
              </p>
            )}

            <button
              onClick={onClose}
              className="px-3 py-1.5 rounded text-xs font-medium border border-[#3e3e5e]
                         text-gray-400 hover:text-white hover:border-gray-500 transition-colors"
            >
              Cancel
            </button>
            <button
              onClick={handleApply}
              disabled={!selectedId || applying}
              className="px-4 py-1.5 rounded text-xs font-semibold transition-colors
                         disabled:opacity-40 disabled:cursor-not-allowed
                         bg-blue-600 hover:bg-blue-500 active:bg-blue-700
                         text-white flex items-center gap-2 min-w-[120px] justify-center"
            >
              {applying
                ? <><span className="w-3 h-3 border-2 border-white border-t-transparent rounded-full animate-spin" /> Applying…</>
                : 'Apply Material'
              }
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}

// ── Material card ─────────────────────────────────────────────────────────────

function MaterialCard({
  id, name, selected, onSelect,
}: {
  id: string;
  name: string;
  selected: boolean;
  onSelect: () => void;
}) {
  const [imgError, setImgError] = useState(false);

  return (
    <button
      onClick={onSelect}
      title={name}
      className={`relative group rounded-lg overflow-hidden border-2 transition-all
                  focus:outline-none text-left
                  ${selected
                    ? 'border-blue-500 ring-1 ring-blue-500/30 scale-[1.02] shadow-lg shadow-blue-500/20'
                    : 'border-[#2e2e4a] hover:border-[#4e4e6e] hover:scale-[1.01]'}`}
    >
      {imgError ? (
        <div className="w-full aspect-square bg-[#1a1a2e] flex items-center justify-center p-2">
          <span className="text-[10px] text-gray-500 text-center leading-tight">{name}</span>
        </div>
      ) : (
        <img
          src={`${PH_CDN}/asset_img/thumbs/${id}.png?width=200`}
          alt={name}
          loading="lazy"
          onError={() => setImgError(true)}
          className="w-full aspect-square object-cover bg-[#1a1a2e]"
        />
      )}

      {selected && (
        <div className="absolute top-1.5 right-1.5 w-5 h-5 rounded-full bg-blue-500
                        flex items-center justify-center shadow-md pointer-events-none">
          <span className="text-white text-[10px] font-bold">✓</span>
        </div>
      )}

      <div
        className="absolute inset-x-0 bottom-0 bg-gradient-to-t from-black/90 via-black/40 to-transparent
                   p-2 opacity-0 group-hover:opacity-100 transition-opacity pointer-events-none"
      >
        <p className="text-[11px] text-white font-medium leading-tight line-clamp-2">{name}</p>
      </div>
    </button>
  );
}
