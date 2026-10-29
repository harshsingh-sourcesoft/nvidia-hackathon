'use client';
import { Suspense, useEffect, useMemo, useRef, useState } from 'react';
import { Canvas, useThree } from '@react-three/fiber';
import type { ThreeEvent } from '@react-three/fiber';
import { OrbitControls, Text, useGLTF } from '@react-three/drei';
import * as THREE from 'three';
import { useEditorStore } from '@/store/useEditorStore';
import IsaacSimDialog from './IsaacSimDialog';
import {
  wallTo3DTransform, roomFloorPoints3D, PIXELS_PER_METER,
  wallLength, wallAngle,
} from '@/utils/geometry';
import type { Wall, Room, FurnitureItem, Door, WindowItem, PolyhavenMaterial } from '@/types';

// ── PolyHaven texture cache (module-level, survives re-mounts) ─────────────────
const _texCache = new Map<string, THREE.Texture>();

function loadTex(url: string): Promise<THREE.Texture> {
  const cached = _texCache.get(url);
  if (cached) return Promise.resolve(cached);
  return new Promise((resolve, reject) => {
    new THREE.TextureLoader().load(
      url,
      t => { _texCache.set(url, t); resolve(t); },
      undefined,
      reject,
    );
  });
}

type TexMaps = { map?: THREE.Texture; roughnessMap?: THREE.Texture; normalMap?: THREE.Texture };

function useWallTextures(ph: PolyhavenMaterial | undefined, lenM: number, hM: number): TexMaps {
  const [maps, setMaps] = useState<TexMaps>({});
  const phId = ph?.id;

  useEffect(() => {
    if (!ph) { setMaps({}); return; }
    let cancelled = false;

    const entries: Array<[keyof TexMaps, string]> = [
      ['map', ph.diffuseUrl],
      ...(ph.roughnessUrl ? [['roughnessMap', ph.roughnessUrl] as [keyof TexMaps, string]] : []),
      ...(ph.normalUrl    ? [['normalMap',    ph.normalUrl]    as [keyof TexMaps, string]] : []),
    ];

    const rx = Math.max(0.25, lenM);
    const ry = Math.max(0.25, hM);

    // Clone each texture so walls don't share the same instance — repeat mutations
    // on one wall would otherwise clobber every other wall using the same material.
    Promise.all(
      entries.map(([key, url]) =>
        loadTex(url).then(t => {
          const c = t.clone();
          c.wrapS = c.wrapT = THREE.RepeatWrapping;
          c.repeat.set(rx, ry);
          c.needsUpdate = true;
          return { key, t: c };
        })
      )
    )
      .then(results => {
        if (cancelled) return;
        const loaded: TexMaps = {};
        results.forEach(({ key, t }) => { loaded[key] = t; });
        setMaps(loaded);
      })
      .catch(() => { if (!cancelled) setMaps({}); });

    return () => { cancelled = true; };
  // lenM / hM included so repeat is rebaked when the wall is resized
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [phId, lenM, hM]);

  return maps;
}

const M = PIXELS_PER_METER;

// ── Segment mesh — one box-piece of a wall that has door/window openings ──────
// BoxGeometry UVs always span [0,1] regardless of physical size, so each segment
// needs its own cloned textures with repeat set to (sLen, sH) in metres, not the
// full-wall repeat that useWallTextures bakes in.
function SegmentMesh({
  rawMaps, sLen, sH, tM, localX, segYCenter,
  hasTexture, wallColor, wallRoughness, selected, matKey,
}: {
  rawMaps: TexMaps;
  sLen: number; sH: number; tM: number;
  localX: number; segYCenter: number;
  hasTexture: boolean; wallColor: string; wallRoughness: number;
  selected: boolean; matKey: string;
}) {
  const [segMaps, setSegMaps] = useState<TexMaps>({});

  useEffect(() => {
    if (!hasTexture) { setSegMaps({}); return; }
    const result: TexMaps = {};
    (['map', 'roughnessMap', 'normalMap'] as const).forEach(k => {
      const t = rawMaps[k];
      if (!t) return;
      const c = t.clone();
      c.wrapS = c.wrapT = THREE.RepeatWrapping;
      c.repeat.set(Math.max(0.25, sLen), Math.max(0.25, sH));
      c.needsUpdate = true;
      result[k] = c;
    });
    setSegMaps(result);
  }, [rawMaps, sLen, sH, hasTexture]);

  const color = selected ? '#6ba3f7' : (hasTexture ? '#ffffff' : wallColor);
  const roughness = hasTexture ? 1.0 : wallRoughness;

  return (
    <mesh position={[localX, segYCenter, 0]} castShadow receiveShadow>
      <boxGeometry args={[sLen, sH, tM]} />
      <meshStandardMaterial
        key={matKey}
        color={color} roughness={roughness} metalness={0}
        map={segMaps.map ?? null}
        roughnessMap={segMaps.roughnessMap ?? null}
        normalMap={segMaps.normalMap ?? null}
        side={THREE.DoubleSide}
        emissive={selected ? '#1a3a7a' : '#000000'}
        emissiveIntensity={selected ? 0.4 : 0}
      />
    </mesh>
  );
}

// ── Wall mesh — supports door/window openings + 3D selection ─────────────────
function WallMesh({ wall, wallDoors, wallWindows }: {
  wall: Wall;
  wallDoors:   Door[];
  wallWindows: WindowItem[];
}) {
  const selection = useEditorStore(s => s.selection);
  const selected  = selection.id === wall.id && selection.type === 'wall';

  const lenDu = wallLength(wall);
  const lenM  = lenDu / M;
  const hM    = wall.height    / M;
  const tM    = wall.thickness / M;
  const angle = wallAngle(wall);
  const cxM   = (wall.start.x + wall.end.x) / 2 / M;
  const czM   = (wall.start.y + wall.end.y) / 2 / M;

  const handleClick = (e: ThreeEvent<MouseEvent>) => {
    e.stopPropagation();
    const store = useEditorStore.getState();
    if (selected) store.deselect(); else store.select('wall', wall.id);
  };

  const rawMaps    = useWallTextures(wall.polyhavenMaterial, lenM, hM);
  const hasTexture = !!rawMaps.map;
  const phId       = wall.polyhavenMaterial?.id ?? 'none';
  const wallColor  = wall.color ?? '#c8b89a';
  const wallRoughness = wall.roughness ?? 0.9;
  const color = selected ? '#6ba3f7' : (hasTexture ? '#ffffff' : wallColor);
  const rough = hasTexture ? 1.0 : wallRoughness;

  // Key forces R3F to destroy + recreate the material when texture state changes
  // (Three.js needs a fresh material to compile the correct shader variant with maps)
  const matKey = `${wall.id}-${phId}-${hasTexture ? 'tex' : 'flat'}`;

  const mat = (
    <meshStandardMaterial
      key={matKey}
      color={color}
      roughness={rough}
      metalness={0}
      map={rawMaps.map ?? null}
      roughnessMap={rawMaps.roughnessMap ?? null}
      normalMap={rawMaps.normalMap ?? null}
      side={THREE.DoubleSide}
      emissive={selected ? '#1a3a7a' : '#000000'}
      emissiveIntensity={selected ? 0.4 : 0}
    />
  );

  const openings = useMemo(() => {
    const list = [
      ...wallDoors.map(d => ({
        t0: Math.max(0, d.positionOnWall - d.width / 2 / lenDu),
        t1: Math.min(1, d.positionOnWall + d.width / 2 / lenDu),
        hMin: 0,
        hMax: Math.min(d.height / M, hM),
      })),
      ...wallWindows.map(w => ({
        t0: Math.max(0, w.positionOnWall - w.width / 2 / lenDu),
        t1: Math.min(1, w.positionOnWall + w.width / 2 / lenDu),
        hMin: Math.min(w.sillHeight / M, hM),
        hMax: Math.min((w.sillHeight + w.height) / M, hM),
      })),
    ].sort((a, b) => a.t0 - b.t0);
    return list;
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [wallDoors, wallWindows, lenDu, hM]);

  if (openings.length === 0) {
    const tf = wallTo3DTransform(wall);
    return (
      <mesh position={tf.position} rotation={[0, tf.rotationY, 0]} scale={tf.scale}
        castShadow receiveShadow onClick={handleClick}>
        <boxGeometry args={[1, 1, 1]} />
        {mat}
      </mesh>
    );
  }

  type Seg = { t0: number; t1: number; hMin: number; hMax: number };
  const segs: Seg[] = [];
  let cur = 0;
  for (const o of openings) {
    if (cur < o.t0 - 0.002) segs.push({ t0: cur,  t1: o.t0, hMin: 0,      hMax: hM    });
    if (o.hMin > 0.002)      segs.push({ t0: o.t0, t1: o.t1, hMin: 0,      hMax: o.hMin });
    if (o.hMax < hM - 0.002) segs.push({ t0: o.t0, t1: o.t1, hMin: o.hMax, hMax: hM    });
    cur = o.t1;
  }
  if (cur < 1 - 0.002) segs.push({ t0: cur, t1: 1, hMin: 0, hMax: hM });

  return (
    <group position={[cxM, 0, czM]} rotation={[0, -angle, 0]} onClick={handleClick}>
      {segs.map((seg, i) => {
        const sLen = (seg.t1 - seg.t0) * lenM;
        const sH   = seg.hMax - seg.hMin;
        const localX = ((seg.t0 + seg.t1) / 2 - 0.5) * lenM;
        return (
          <SegmentMesh
            key={i}
            rawMaps={rawMaps}
            sLen={sLen} sH={sH} tM={tM}
            localX={localX} segYCenter={seg.hMin + sH / 2}
            hasTexture={hasTexture} wallColor={wallColor} wallRoughness={wallRoughness}
            selected={selected}
            matKey={`${wall.id}-seg${i}-${phId}-${hasTexture ? 'tex' : 'flat'}`}
          />
        );
      })}
      {wallWindows.map(w => {
        const t0 = Math.max(0, w.positionOnWall - w.width / 2 / lenDu);
        const t1 = Math.min(1, w.positionOnWall + w.width / 2 / lenDu);
        const hMin = Math.min(w.sillHeight / M, hM);
        const hMax = Math.min((w.sillHeight + w.height) / M, hM);
        const sLen = (t1 - t0) * lenM;
        const sH   = hMax - hMin;
        const localX = ((t0 + t1) / 2 - 0.5) * lenM;
        return (
          <mesh key={`glass-${w.id}`} name="PlanLift_Glass" position={[localX, hMin + sH / 2, 0]}>
            <planeGeometry args={[sLen, sH]} />
            <meshPhysicalMaterial
              name="PlanLift_Glass"
              color="#cfe8f5"
              transparent
              opacity={0.2}
              roughness={0.05}
              metalness={0}
              side={THREE.DoubleSide}
            />
          </mesh>
        );
      })}
      {wallDoors.filter(d => d.doorType === 'sliding').map(d => {
        const t0 = Math.max(0, d.positionOnWall - d.width / 2 / lenDu);
        const t1 = Math.min(1, d.positionOnWall + d.width / 2 / lenDu);
        const dLen = (t1 - t0) * lenM;
        const dH   = Math.min(d.height / M, hM);
        const localX = ((t0 + t1) / 2 - 0.5) * lenM;
        // Tiny per-door epsilon keeps each glass panel's vertex buffer unique so
        // the GLTFExporter does not deduplicate them into a shared GLTF mesh.
        // Shared meshes create USD Xform+instanceable hierarchy instead of a flat
        // Mesh, which breaks material binding. 1e-6 m = 0.001 mm — imperceptible.
        const _dedup = d.id.split('').reduce((s, c) => s + c.charCodeAt(0), 0) * 1e-6;
        return (
          <mesh key={`door-glass-${d.id}`} name="PlanLift_Glass" position={[localX, dH / 2, 0]}>
            <planeGeometry args={[dLen + _dedup, dH]} />
            <meshPhysicalMaterial
              name="PlanLift_Glass"
              color="#cfe8f5"
              transparent
              opacity={0.2}
              roughness={0.05}
              metalness={0}
              side={THREE.DoubleSide}
            />
          </mesh>
        );
      })}
    </group>
  );
}

// ── Corner fillers ────────────────────────────────────────────────────────────
function CornerFillers({ walls }: { walls: Wall[] }) {
  const selection = useEditorStore(s => s.selection);

  const fillers = useMemo(() => {
    const TOL  = 8;
    const seen = new Set<string>();
    const out: { key: string; x: number; z: number; h: number; t: number; color: string; wallIds: string[] }[] = [];
    for (let i = 0; i < walls.length; i++) {
      for (const epA of [walls[i].start, walls[i].end]) {
        for (let j = i + 1; j < walls.length; j++) {
          for (const epB of [walls[j].start, walls[j].end]) {
            if (Math.abs(epA.x - epB.x) < TOL && Math.abs(epA.y - epB.y) < TOL) {
              const key = `${Math.round(epA.x / TOL) * TOL}_${Math.round(epA.y / TOL) * TOL}`;
              if (seen.has(key)) continue;
              seen.add(key);
              out.push({
                key,
                x: epA.x / M, z: epA.y / M,
                h: Math.max(walls[i].height, walls[j].height) / M,
                t: Math.max(walls[i].thickness, walls[j].thickness) / M,
                color: walls[i].color ?? '#c8b89a',
                wallIds: [walls[i].id, walls[j].id],
              });
            }
          }
        }
      }
    }
    return out;
  }, [walls]);

  return (
    <>
      {fillers.map(f => {
        const isSelected = f.wallIds.includes(selection.id ?? '');
        return (
          <mesh key={f.key} position={[f.x, f.h / 2, f.z]} castShadow receiveShadow>
            <boxGeometry args={[f.t, f.h, f.t]} />
            <meshStandardMaterial
              color={isSelected ? '#6ba3f7' : f.color}
              roughness={0.9} metalness={0}
              emissive={isSelected ? '#1a3a7a' : '#000000'}
              emissiveIntensity={isSelected ? 0.4 : 0}
            />
          </mesh>
        );
      })}
    </>
  );
}

// ── Room floor ────────────────────────────────────────────────────────────────
function RoomFloor({ room }: { room: Room }) {
  const pts3d = roomFloorPoints3D(room);
  if (pts3d.length < 3) return null;

  const shape = useMemo(() => {
    const s = new THREE.Shape();
    s.moveTo(pts3d[0][0], pts3d[0][2]);
    for (let i = 1; i < pts3d.length; i++) s.lineTo(pts3d[i][0], pts3d[i][2]);
    s.closePath();
    return s;
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [room.polygon]);

  const geom = useMemo(() => {
    const g = new THREE.ShapeGeometry(shape);
    g.rotateX(-Math.PI / 2);
    return g;
  }, [shape]);

  return (
    <mesh geometry={geom} receiveShadow position={[0, 0.01, 0]}>
      <meshStandardMaterial color={new THREE.Color(room.color)} roughness={0.95} transparent opacity={0.5} />
    </mesh>
  );
}

// ── Furniture box ─────────────────────────────────────────────────────────────
function FurnitureBox({ item }: { item: FurnitureItem }) {
  const selection = useEditorStore(s => s.selection);
  const selected  = selection.id === item.id && selection.type === 'furniture';

  const COLORS: Record<string, string> = {
    sofa: '#4a6fa5', bed: '#7a5c9a', table: '#6b8f5e',
    chair: '#4a6fa5', desk: '#6b8f5e', default: '#5a6a7a',
  };
  const baseColor = COLORS[item.furnitureType] ?? COLORS.default;
  const w = item.width / M, d = item.depth / M, h = item.height / M;

  return (
    <mesh
      position={[item.position.x / M, h / 2, item.position.y / M]}
      rotation={[0, (item.rotation * Math.PI) / 180, 0]}
      castShadow
    >
      <boxGeometry args={[w, h, d]} />
      <meshStandardMaterial color={baseColor} roughness={0.8}
        emissive={selected ? '#1a3a7a' : '#000000'}
        emissiveIntensity={selected ? 0.4 : 0}
      />
    </mesh>
  );
}

// ── Imported GLB ──────────────────────────────────────────────────────────────
function GlbModel({ url }: { url: string }) {
  const { scene } = useGLTF(url);
  return <primitive object={scene} />;
}

// ── GLB exporter — must live inside Canvas to access useThree ─────────────────
function SceneGlbExporter({
  exportRef,
}: {
  exportRef: React.MutableRefObject<(() => Promise<Blob>) | null>;
}) {
  const { scene } = useThree();

  useEffect(() => {
    exportRef.current = () =>
      import('three/examples/jsm/exporters/GLTFExporter.js').then(
        ({ GLTFExporter }) =>
          new Promise<Blob>((resolve, reject) => {
            new GLTFExporter().parse(
              scene,
              result => {
                const blob =
                  result instanceof ArrayBuffer
                    ? new Blob([result], { type: 'model/gltf-binary' })
                    : new Blob([JSON.stringify(result)], { type: 'model/gltf+json' });
                resolve(blob);
              },
              reject,
              { binary: true },
            );
          }),
      );
    return () => {
      exportRef.current = null;
    };
  }, [scene, exportRef]);

  return null;
}

// ── Scene ─────────────────────────────────────────────────────────────────────
function Scene({
  exportRef,
}: {
  exportRef: React.MutableRefObject<(() => Promise<Blob>) | null>;
}) {
  const floors         = useEditorStore(s => s.project.floors);
  const importedGlbUrl = useEditorStore(s => s.importedGlbUrl);

  // Compute cumulative Y offset for each floor (ground = 0, each upper floor sits on top)
  const floorYOffsets = useMemo(() => {
    const offsets: Record<string, number> = {};
    let cumY = 0;
    for (const floor of floors) {
      offsets[floor.id] = cumY;
      cumY += (floor.floorHeight ?? 270) / M;
    }
    return offsets;
  }, [floors]);

  const bounds = useMemo(() => {
    const allWalls = floors.flatMap(f => f.walls);
    if (allWalls.length === 0) return { cx: 0, cz: 0, size: 10 };
    let minX = Infinity, maxX = -Infinity, minZ = Infinity, maxZ = -Infinity;
    for (const w of allWalls) {
      minX = Math.min(minX, w.start.x, w.end.x);
      maxX = Math.max(maxX, w.start.x, w.end.x);
      minZ = Math.min(minZ, w.start.y, w.end.y);
      maxZ = Math.max(maxZ, w.start.y, w.end.y);
    }
    return { cx: ((minX+maxX)/2)/M, cz: ((minZ+maxZ)/2)/M, size: Math.max(maxX-minX,maxZ-minZ)/M };
  }, [floors]);

  const hasAnyWalls = floors.some(f => f.walls.length > 0);

  return (
    <>
      <ambientLight intensity={1.2} />
      <directionalLight position={[10, 20, 10]} intensity={2} castShadow shadow-mapSize={[2048, 2048]} />
      <directionalLight position={[-8, 10, -6]} intensity={0.4} />

      <color attach="background" args={['#9dc8e8']} />
      <fog attach="fog" args={['#c5dff0', 40, 120]} />

      {/* Ground plane */}
      <mesh rotation={[-Math.PI / 2, 0, 0]} receiveShadow position={[0, -0.01, 0]}>
        <planeGeometry args={[300, 300]} />
        <meshStandardMaterial color="#f2f2f2" roughness={0.95} />
      </mesh>

      {/* Render all floors stacked by their Y offset */}
      {floors.map(floor => {
        const yOffset = floorYOffsets[floor.id] ?? 0;
        return (
          <group key={floor.id} position={[0, yOffset, 0]}>
            {floor.rooms.map(r => <RoomFloor key={r.id} room={r} />)}
            {floor.walls.map(w => (
              <WallMesh
                key={w.id}
                wall={w}
                wallDoors={floor.doors.filter(d => d.wallId === w.id)}
                wallWindows={floor.windows.filter(win => win.wallId === w.id)}
              />
            ))}
            <CornerFillers walls={floor.walls} />
            {floor.furniture.map(f => <FurnitureBox key={f.id} item={f} />)}
          </group>
        );
      })}

      {importedGlbUrl && (
        <Suspense fallback={null}>
          <GlbModel url={importedGlbUrl} />
        </Suspense>
      )}

      <OrbitControls
        target={[bounds.cx, 0, bounds.cz]}
        maxPolarAngle={Math.PI / 2}
        minDistance={1}
        maxDistance={100}
      />

      {!hasAnyWalls && !importedGlbUrl && (
        <Text position={[0, 1, 0]} color="#64748b" fontSize={0.4} anchorX="center" anchorY="middle">
          Draw walls in 2D view to see them here
        </Text>
      )}

      <SceneGlbExporter exportRef={exportRef} />
    </>
  );
}

// ── Exported component ────────────────────────────────────────────────────────
export default function Viewer3D() {
  const glbRef            = useRef<HTMLInputElement>(null);
  const exportGlbRef      = useRef<(() => Promise<Blob>) | null>(null);
  const importedGlbUrl    = useEditorStore(s => s.importedGlbUrl);
  const setImportedGlbUrl = useEditorStore(s => s.setImportedGlbUrl);
  const projectName       = useEditorStore(s => s.project.name);
  const floors            = useEditorStore(s => s.project.floors);
  const [showIsaacDialog, setShowIsaacDialog] = useState(false);

  const handleGlbImport = (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    if (!file) return;
    if (importedGlbUrl) URL.revokeObjectURL(importedGlbUrl);
    setImportedGlbUrl(URL.createObjectURL(file));
    e.target.value = '';
  };

  const hasModel = floors.some(f => f.walls.length > 0) || !!importedGlbUrl;

  return (
    <div className="w-full h-full relative">
      <Canvas
        shadows
        camera={{ position: [8, 6, 8], fov: 50 }}
        gl={{ antialias: true, toneMapping: THREE.ACESFilmicToneMapping }}
        onCreated={({ gl }) => { gl.toneMappingExposure = 1.2; }}
      >
        <Scene exportRef={exportGlbRef} />
      </Canvas>

      <div className="absolute bottom-4 right-4 flex items-center gap-2 pointer-events-auto">
        {importedGlbUrl && (
          <button
            onClick={() => { URL.revokeObjectURL(importedGlbUrl); setImportedGlbUrl(null); }}
            className="px-2 py-1.5 rounded text-xs font-medium
                       bg-red-700/80 text-white hover:bg-red-600 border border-red-500/40
                       backdrop-blur-sm shadow-lg"
          >
            ✕ Remove GLB
          </button>
        )}
        <button
          onClick={() => glbRef.current?.click()}
          className="px-3 py-1.5 rounded text-xs font-semibold
                     bg-blue-600/90 text-white hover:bg-blue-500 border border-blue-400/40
                     backdrop-blur-sm shadow-lg flex items-center gap-1.5"
        >
          <span>↑</span>
          <span>{importedGlbUrl ? 'Replace Pipeline GLB' : 'Import Pipeline GLB'}</span>
        </button>
        <input ref={glbRef} type="file" accept=".glb,.gltf" className="hidden" onChange={handleGlbImport} />

        {/* Isaac Sim button — visible when there is something to export */}
        {hasModel && (
          <button
            onClick={() => setShowIsaacDialog(true)}
            className="px-3 py-1.5 rounded text-xs font-semibold
                       bg-[#76b900]/90 text-white hover:bg-[#8fd400] border border-[#76b900]/60
                       backdrop-blur-sm shadow-lg flex items-center gap-1.5"
            title="Export and open this model in NVIDIA Isaac Sim"
          >
            <span>🤖</span>
            <span>Open in Isaac Sim</span>
          </button>
        )}
      </div>

      <div className="absolute top-3 left-1/2 -translate-x-1/2 pointer-events-none">
        <span className="text-xs text-white/40 bg-black/20 px-2 py-0.5 rounded backdrop-blur-sm">
          Click a wall to select · Drag to orbit · Scroll to zoom
        </span>
      </div>

      {showIsaacDialog && (
        <IsaacSimDialog
          onClose={() => setShowIsaacDialog(false)}
          exportGlb={() => exportGlbRef.current!()}
          projectName={projectName}
        />
      )}
    </div>
  );
}
