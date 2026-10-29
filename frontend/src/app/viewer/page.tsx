'use client';

import { Suspense, useCallback, useEffect, useRef, useState } from 'react';
import { useSearchParams } from 'next/navigation';
import { Canvas, useThree } from '@react-three/fiber';
import { OrbitControls, useGLTF, Environment, Html, useProgress } from '@react-three/drei';
import * as THREE from 'three';

// ── Loading indicator (runs inside Canvas) ────────────────────────────────────
function Loader() {
  const { progress } = useProgress();
  return (
    <Html center>
      <div style={{
        color: '#ddd', fontSize: 13,
        background: 'rgba(0,0,0,0.55)', padding: '7px 20px',
        borderRadius: 20, backdropFilter: 'blur(6px)', whiteSpace: 'nowrap',
      }}>
        Loading {Math.round(progress)}%
      </div>
    </Html>
  );
}

// ── GLB model ─────────────────────────────────────────────────────────────────
function GLBModel({ url, onBox }: { url: string; onBox: (b: THREE.Box3) => void }) {
  const { scene } = useGLTF(url);

  useEffect(() => {
    scene.traverse(n => {
      const mesh = n as THREE.Mesh;
      if (mesh.isMesh) { mesh.castShadow = true; mesh.receiveShadow = true; }
    });
    onBox(new THREE.Box3().setFromObject(scene));
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [scene]);

  return <primitive object={scene} />;
}

type CameraCmd = { kind: 'reset' | 'top'; id: number };

// ── Scene (must be inside Canvas to use useThree) ─────────────────────────────
function Scene({
  modelUrl, wireframe, cameraCmd, orbitRef,
}: {
  modelUrl: string;
  wireframe: boolean;
  cameraCmd: CameraCmd | null;
  orbitRef: React.RefObject<any>;
}) {
  const { camera, scene } = useThree();
  const [modelBox, setModelBox] = useState<THREE.Box3 | null>(null);

  // Apply wireframe to every mesh in the scene
  useEffect(() => {
    scene.traverse(n => {
      const mesh = n as THREE.Mesh;
      if (!mesh.isMesh) return;
      const mats = Array.isArray(mesh.material) ? mesh.material : [mesh.material];
      (mats as THREE.MeshStandardMaterial[]).forEach(m => { m.wireframe = wireframe; });
    });
  }, [wireframe, scene]);

  // Handle reset / top-view commands
  useEffect(() => {
    if (!cameraCmd || !orbitRef.current) return;
    const box    = modelBox ?? new THREE.Box3(new THREE.Vector3(-10, 0, -10), new THREE.Vector3(10, 5, 10));
    const size   = box.getSize(new THREE.Vector3()).length();
    const center = box.getCenter(new THREE.Vector3());
    if (cameraCmd.kind === 'reset') {
      camera.position.set(center.x + size * 0.7, center.y + size * 0.6, center.z + size * 0.7);
    } else {
      camera.position.set(center.x, center.y + size * 1.5, center.z + 0.001);
    }
    orbitRef.current.target.copy(center);
    orbitRef.current.update();
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [cameraCmd]);

  // Auto-frame on first load
  const handleBox = useCallback((box: THREE.Box3) => {
    setModelBox(box);
    const size   = box.getSize(new THREE.Vector3()).length();
    const center = box.getCenter(new THREE.Vector3());
    camera.position.set(center.x + size * 0.7, center.y + size * 0.6, center.z + size * 0.7);
    if (orbitRef.current) {
      orbitRef.current.target.copy(center);
      orbitRef.current.update();
    }
  }, [camera, orbitRef]);

  return (
    <>
      <ambientLight intensity={1.5} />
      <directionalLight position={[20, 35, 15]} intensity={3} castShadow
        shadow-mapSize-width={2048} shadow-mapSize-height={2048} />
      <directionalLight position={[-10, 12, -8]} intensity={0.5} />

      <color attach="background" args={['#9dc8e8']} />
      <fog attach="fog" args={['#c5dff0', 120, 380]} />

      <mesh rotation={[-Math.PI / 2, 0, 0]} position={[0, -0.01, 0]} receiveShadow>
        <planeGeometry args={[400, 400]} />
        <meshStandardMaterial color="#f2f2f2" roughness={0.9} />
      </mesh>

      <Environment preset="apartment" />

      <Suspense fallback={<Loader />}>
        <GLBModel url={modelUrl} onBox={handleBox} />
      </Suspense>

      <OrbitControls ref={orbitRef} enableDamping dampingFactor={0.08} maxPolarAngle={Math.PI / 2} />
    </>
  );
}

// ── Canvas wrapper ────────────────────────────────────────────────────────────
function ViewerCanvas({ modelUrl }: { modelUrl: string }) {
  const [wireframe, setWireframe] = useState(false);
  const [cameraCmd, setCameraCmd] = useState<CameraCmd | null>(null);
  const orbitRef = useRef<any>(null);

  const btnCls =
    'px-4 py-2 rounded-2xl bg-white/10 text-white/80 border border-white/20 ' +
    'text-xs hover:bg-white/20 transition-colors backdrop-blur-sm cursor-pointer';

  return (
    <div className="w-screen h-screen relative overflow-hidden">
      <Canvas
        shadows
        camera={{ position: [15, 18, 15], fov: 45 }}
        gl={{ antialias: true, toneMapping: THREE.ACESFilmicToneMapping }}
        onCreated={({ gl }) => {
          gl.toneMappingExposure = 1.4;
          gl.outputColorSpace    = THREE.SRGBColorSpace;
        }}
      >
        <Scene
          modelUrl={modelUrl}
          wireframe={wireframe}
          cameraCmd={cameraCmd}
          orbitRef={orbitRef}
        />
      </Canvas>

      {/* Top hint */}
      <div className="absolute top-3 left-1/2 -translate-x-1/2 pointer-events-none">
        <span className="text-xs text-white/60 bg-black/30 px-3 py-1 rounded-full backdrop-blur-sm">
          Drag to orbit · Scroll to zoom · Right-drag to pan
        </span>
      </div>

      {/* Bottom controls */}
      <div className="absolute bottom-4 left-1/2 -translate-x-1/2 flex gap-2">
        <button className={btnCls} onClick={() => setCameraCmd({ kind: 'reset', id: Date.now() })}>
          ⟳ Reset view
        </button>
        <button className={btnCls} onClick={() => setCameraCmd({ kind: 'top', id: Date.now() })}>
          ⊡ Top view
        </button>
        <button className={btnCls} onClick={() => setWireframe(w => !w)}>
          ⬡ Wireframe
        </button>
      </div>
    </div>
  );
}

// ── Route content ─────────────────────────────────────────────────────────────
function ViewerContent() {
  const params   = useSearchParams();
  const modelUrl = params.get('model') ?? '';

  if (!modelUrl) {
    return (
      <div className="w-screen h-screen bg-[#1a1a1a] flex items-center justify-center">
        <p className="text-white/50 text-sm">No model specified — add ?model=URL to the address bar.</p>
      </div>
    );
  }

  return <ViewerCanvas modelUrl={modelUrl} />;
}

// ── Page — Suspense required for useSearchParams in Next.js 14 ────────────────
export default function ViewerPage() {
  return (
    <Suspense fallback={
      <div className="w-screen h-screen bg-[#1a1a1a] flex items-center justify-center">
        <p className="text-white/40 text-sm">Loading…</p>
      </div>
    }>
      <ViewerContent />
    </Suspense>
  );
}
