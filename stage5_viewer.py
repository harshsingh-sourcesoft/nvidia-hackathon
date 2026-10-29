"""
Stage 5 — Interactive 3D Viewer with Furniture Library
Launches a self-contained Three.js viewer in your browser.
No Node.js, no npm — pure Python HTTP server + inline HTML.

Features:
  • Orbit / zoom / pan controls
  • Furniture sidebar — drag items into the 3D scene
  • Click placed furniture to select → drag to move, R to rotate, Delete to remove
  • Top-view / wireframe toggle
"""

import http.server
import os
import socket
import sys
import threading
import webbrowser
from pathlib import Path
import argparse


HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"/>
<title>PlanLift 3D Viewer</title>
<style>
* { margin:0; padding:0; box-sizing:border-box; }
body { background:#1a1a1a; font-family:'Segoe UI',sans-serif; overflow:hidden; display:flex; height:100vh; }

/* ── Sidebar ── */
#sidebar {
  width:190px; min-width:190px; height:100vh; background:#202020;
  border-right:1px solid #2e2e2e; display:flex; flex-direction:column; overflow:hidden;
}
#sidebar-header {
  padding:16px 14px 6px; font-size:11px; text-transform:uppercase;
  letter-spacing:1.2px; color:#666; font-weight:700;
}
#sidebar-hint {
  padding:0 14px 10px; font-size:10px; color:#444;
}
#furniture-list {
  flex:1 1 0; min-height:80px; overflow-y:auto; display:flex; flex-direction:column; gap:3px; padding:4px 8px 12px;
}
#furniture-list::-webkit-scrollbar { width:4px; }
#furniture-list::-webkit-scrollbar-thumb { background:#333; border-radius:2px; }

.fcard {
  background:#272727; border:1px solid #313131; border-radius:8px;
  padding:9px 11px; cursor:grab; display:flex; align-items:center; gap:10px;
  user-select:none; transition:background .12s, border-color .12s, transform .1s;
}
.fcard:hover  { background:#303030; border-color:#484848; }
.fcard:active { transform:scale(0.97); cursor:grabbing; }
.ficon { font-size:20px; line-height:1; flex-shrink:0; }
.fname { font-size:12px; color:#bbb; font-weight:500; line-height:1.3; }
.fsize { font-size:10px; color:#555; }

/* ── Wall Material panel ── */
#mat-section {
  border-top:1px solid #2a2a2a; padding:10px 8px 12px; flex-shrink:0; overflow:hidden;
}
#mat-header {
  font-size:11px; text-transform:uppercase; letter-spacing:1.2px;
  color:#666; font-weight:700; padding:0 6px 8px;
}
#mat-list { display:flex; flex-direction:column; gap:5px; }
.matcard {
  background:#272727; border:1px solid #313131; border-radius:8px;
  padding:7px 10px; cursor:pointer; display:flex; align-items:center; gap:10px;
  user-select:none; transition:background .12s, border-color .12s;
}
.matcard:hover  { background:#303030; border-color:#484848; }
.matcard.active { border-color:#508cff; background:#1e2a40; }
.matthumb {
  width:36px; height:36px; border-radius:5px; flex-shrink:0;
  background-size:cover; background-position:center; border:1px solid #3a3a3a;
}
.mname { font-size:11px; color:#bbb; font-weight:500; line-height:1.3; }

/* ── Drag ghost ── */
#ghost {
  position:fixed; pointer-events:none; display:none; z-index:9999;
  background:rgba(80,140,255,0.88); color:#fff; padding:6px 14px;
  border-radius:20px; font-size:12px; font-weight:600;
  box-shadow:0 4px 16px rgba(0,0,0,0.5); transform:translate(-50%,-50%);
  white-space:nowrap;
}

/* ── Canvas area ── */
#canvas-wrap { flex:1; position:relative; overflow:hidden; background:#a8c8e8; }
canvas { display:block; width:100% !important; height:100% !important; }

/* ── HUD ── */
#hud {
  position:absolute; top:14px; left:0; right:0;
  display:flex; justify-content:center; pointer-events:none;
}
#hud-text {
  background:rgba(0,0,0,0.55); color:#ddd; padding:7px 20px;
  border-radius:20px; font-size:12px; backdrop-filter:blur(6px);
}

/* ── Bottom bar ── */
#controls {
  position:absolute; bottom:18px; left:50%; transform:translateX(-50%);
  display:flex; gap:8px; align-items:center;
}
button {
  background:rgba(255,255,255,0.1); color:#ccc; border:1px solid rgba(255,255,255,0.18);
  padding:7px 18px; border-radius:16px; cursor:pointer; font-size:12px;
  transition:background .14s;
}
button:hover { background:rgba(255,255,255,0.2); }

/* ── Selection hint ── */
#sel-hint {
  position:absolute; bottom:60px; left:50%; transform:translateX(-50%);
  background:rgba(80,140,255,0.85); color:#fff; padding:5px 16px;
  border-radius:12px; font-size:11px; display:none; white-space:nowrap;
  backdrop-filter:blur(4px);
}
</style>
</head>
<body>

<div id="sidebar">
  <div id="sidebar-header">Furniture</div>
  <div id="sidebar-hint">Drag &amp; drop into scene</div>
  <div id="furniture-list"></div>
  <div id="mat-section">
    <div id="mat-header">Wall Material</div>
    <div id="mat-list">
      <div class="matcard active" data-mat="original">
        <div class="matthumb" style="background:#5a4a3a;"></div>
        <div class="mname">Original</div>
      </div>
      <div class="matcard" data-mat="patterned_concrete">
        <div class="matthumb" style="background-image:url('/Materials/patterned_concrete_wall_diff_4k.jpg');"></div>
        <div class="mname">Patterned Concrete</div>
      </div>
      <div class="matcard" data-mat="precast_concrete">
        <div class="matthumb" style="background-image:url('/Materials/precast_concrete_wall_diff_4k.jpg');"></div>
        <div class="mname">Precast Concrete</div>
      </div>
    </div>
  </div>
</div>

<div id="canvas-wrap">
  <div id="hud"><div id="hud-text">Loading model…</div></div>
  <div id="controls">
    <button onclick="resetCamera()">⟳ Reset view</button>
    <button onclick="toggle2D()">⊡ Top view</button>
    <button onclick="toggleWireframe()">⬡ Wireframe</button>
  </div>
  <div id="sel-hint">Selected — Drag: move &nbsp;|&nbsp; Scroll: rotate &nbsp;|&nbsp; Right-drag: rotate &nbsp;|&nbsp; R: snap 90° &nbsp;|&nbsp; Del: remove</div>
</div>

<div id="ghost"></div>

<script type="importmap">
{
  "imports": {
    "three":        "https://cdn.jsdelivr.net/npm/three@0.162.0/build/three.module.js",
    "three/addons/":"https://cdn.jsdelivr.net/npm/three@0.162.0/examples/jsm/"
  }
}
</script>
<script type="module">
import * as THREE from 'three';
import { GLTFLoader }      from 'three/addons/loaders/GLTFLoader.js';
import { OrbitControls }   from 'three/addons/controls/OrbitControls.js';
import { RoomEnvironment } from 'three/addons/environments/RoomEnvironment.js';

// ── Renderer ──────────────────────────────────────────────────────────────────
const wrap = document.getElementById('canvas-wrap');
const renderer = new THREE.WebGLRenderer({ antialias:true });
renderer.setPixelRatio(devicePixelRatio);
renderer.shadowMap.enabled   = true;
renderer.shadowMap.type      = THREE.PCFSoftShadowMap;
renderer.toneMapping         = THREE.ACESFilmicToneMapping;
renderer.toneMappingExposure = 1.4;
renderer.outputColorSpace    = THREE.SRGBColorSpace;
wrap.appendChild(renderer.domElement);

const scene  = new THREE.Scene();

const camera = new THREE.PerspectiveCamera(45, 1, 0.01, 500);
camera.position.set(15, 18, 15);

const controls = new OrbitControls(camera, renderer.domElement);
controls.enableDamping  = true;
controls.dampingFactor  = 0.08;

// ── Sky background + fog ─────────────────────────────────────────────────────
scene.background = new THREE.Color(0x9dc8e8);
scene.fog        = new THREE.Fog(0xc5dff0, 120, 380);

// ── Environment map (IBL — makes MeshStandardMaterial textures visible) ──────
const pmrem = new THREE.PMREMGenerator(renderer);
scene.environment = pmrem.fromScene(new RoomEnvironment(), 0.04).texture;
pmrem.dispose();

// ── Lights ────────────────────────────────────────────────────────────────────
scene.add(new THREE.AmbientLight(0xffffff, 1.5));
const sun = new THREE.DirectionalLight(0xfff8e0, 3.0);
sun.position.set(20, 35, 15);
sun.castShadow = true;
sun.shadow.mapSize.setScalar(2048);
Object.assign(sun.shadow.camera, { near:0.1, far:150, left:-50, right:50, top:50, bottom:-50 });
scene.add(sun);

// ── Ground ────────────────────────────────────────────────────────────────────
const groundMesh = new THREE.Mesh(
  new THREE.PlaneGeometry(400, 400),
  new THREE.MeshStandardMaterial({ color: 0xf2f2f2, roughness: 0.9, metalness: 0 })
);
groundMesh.rotation.x = -Math.PI / 2;
groundMesh.receiveShadow = true;
groundMesh.position.y = -0.01;
scene.add(groundMesh);

// Invisible floor plane for drop-raycasting
const floorMesh = new THREE.Mesh(
  new THREE.PlaneGeometry(300, 300),
  new THREE.MeshBasicMaterial({ visible:false, side:THREE.DoubleSide })
);
floorMesh.rotation.x = -Math.PI / 2;
scene.add(floorMesh);

// ── Load floor-plan model ─────────────────────────────────────────────────────
const hudText  = document.getElementById('hud-text');
let   modelBox = null;
let   wallMeshes = [];

new GLTFLoader().load('__MODEL_URL__',
  gltf => {
    const model = gltf.scene;
    const box   = new THREE.Box3().setFromObject(model);
    const c     = box.getCenter(new THREE.Vector3());
    model.position.sub(c);
    model.position.y += (box.max.y - box.min.y) / 2;
    model.traverse(n => { if (n.isMesh) { n.castShadow = true; n.receiveShadow = true; } });
    scene.add(model);
    // collect wall meshes (height > 1 m) and cache their original materials
    wallMeshes = [];
    model.traverse(n => {
      if (!n.isMesh) return;
      const b = new THREE.Box3().setFromObject(n);
      if (b.max.y - b.min.y > 1.0) {
        n.userData.originalMat = n.material;
        wallMeshes.push(n);
      }
    });
    modelBox = new THREE.Box3().setFromObject(model);
    const sz = modelBox.getSize(new THREE.Vector3()).length();
    camera.position.set(sz * 0.7, sz * 0.6, sz * 0.7);
    controls.target.copy(new THREE.Vector3(0, modelBox.max.y * 0.4, 0));
    controls.update();
    hudText.textContent = 'Orbit: drag  |  Zoom: scroll  |  Pan: right-drag  |  Drop furniture from sidebar';
  },
  xhr => { hudText.textContent = `Loading… ${Math.round(xhr.loaded/xhr.total*100)}%`; },
  err => { hudText.textContent = 'Error: ' + err.message; }
);

// ── Material palette ──────────────────────────────────────────────────────────
const M = {
  wood:   new THREE.MeshLambertMaterial({ color: 0x9C6B3C }),
  wood2:  new THREE.MeshLambertMaterial({ color: 0x6B4226 }),
  fabric: new THREE.MeshLambertMaterial({ color: 0x4A6FA5 }),
  fab2:   new THREE.MeshLambertMaterial({ color: 0x7A9CC5 }),
  white:  new THREE.MeshLambertMaterial({ color: 0xF2EFE8 }),
  sheet:  new THREE.MeshLambertMaterial({ color: 0xE4DFD5 }),
  ceram:  new THREE.MeshLambertMaterial({ color: 0xF4F4F0 }),
  water:  new THREE.MeshLambertMaterial({ color: 0x7AB8C0 }),
  metal:  new THREE.MeshLambertMaterial({ color: 0xA8B0B8 }),
  glass:  new THREE.MeshLambertMaterial({ color: 0x90C8D0, transparent:true, opacity:0.55 }),
  dark:   new THREE.MeshLambertMaterial({ color: 0x1A1A1A }),
};

// ── Wall material application ─────────────────────────────────────────────────
const _tl = new THREE.TextureLoader();
const _wallMatCache = {};

// Box-project UVs from the mesh's own world-space bounding box.
// tilesH = how many texture tiles to show along the wall HEIGHT.
// Width tiles = tilesH * (faceWidth / faceHeight) so tiles are always square,
// independent of model unit scale.
function _worldUVs(mesh, tilesH) {
  mesh.updateWorldMatrix(true, false);
  const geo  = mesh.geometry;
  const pos  = geo.attributes.position;
  const norm = geo.attributes.normal;
  if (!pos) return;

  // Build world-space bounding box
  const box = new THREE.Box3();
  const vp  = new THREE.Vector3();
  for (let i = 0; i < pos.count; i++) {
    vp.fromBufferAttribute(pos, i).applyMatrix4(mesh.matrixWorld);
    box.expandByPoint(vp);
  }
  const sz = new THREE.Vector3();
  box.getSize(sz);

  const arr = new Float32Array(pos.count * 2);
  const vn  = new THREE.Vector3();
  for (let i = 0; i < pos.count; i++) {
    vp.fromBufferAttribute(pos, i).applyMatrix4(mesh.matrixWorld);
    if (norm) vn.fromBufferAttribute(norm, i); else vn.set(0, 1, 0);
    const ax = Math.abs(vn.x), ay = Math.abs(vn.y), az = Math.abs(vn.z);

    let u, v, tU, tV;
    if (ay > ax && ay > az) {
      // Horizontal face — tile based on XZ footprint
      u  = (vp.x - box.min.x) / Math.max(sz.x, 1e-4);
      v  = (vp.z - box.min.z) / Math.max(sz.z, 1e-4);
      tV = tilesH;  tU = tilesH * (sz.x / Math.max(sz.z, 1e-4));
    } else if (ax >= az) {
      // X-facing wall — width = Z extent, height = Y extent
      u  = (vp.z - box.min.z) / Math.max(sz.z, 1e-4);
      v  = (vp.y - box.min.y) / Math.max(sz.y, 1e-4);
      tV = tilesH;  tU = tilesH * (sz.z / Math.max(sz.y, 1e-4));
    } else {
      // Z-facing wall — width = X extent, height = Y extent
      u  = (vp.x - box.min.x) / Math.max(sz.x, 1e-4);
      v  = (vp.y - box.min.y) / Math.max(sz.y, 1e-4);
      tV = tilesH;  tU = tilesH * (sz.x / Math.max(sz.y, 1e-4));
    }
    arr[i * 2]     = u * tU;
    arr[i * 2 + 1] = v * tV;
  }
  geo.setAttribute('uv', new THREE.BufferAttribute(arr, 2));
}

function applyWallMat(name) {
  if (!wallMeshes.length) return;
  if (name === 'original') {
    wallMeshes.forEach(n => { n.material = n.userData.originalMat; });
    return;
  }
  const apply = mat => wallMeshes.forEach(n => {
    _worldUVs(n, 2);     // 2 tile-rows per wall height, width scales to keep tiles square
    n.material = mat;
  });
  if (_wallMatCache[name]) { apply(_wallMatCache[name]); return; }
  const base    = `/Materials/${name}_wall_`;
  const diffTex = _tl.load(base + 'diff_4k.jpg');
  const bumpTex = _tl.load(base + 'disp_4k.png');
  diffTex.wrapS = diffTex.wrapT = THREE.RepeatWrapping;
  bumpTex.wrapS = bumpTex.wrapT = THREE.RepeatWrapping;
  const mat = new THREE.MeshStandardMaterial({
    map: diffTex, bumpMap: bumpTex, bumpScale: 0.4,
    roughness: 0.85, metalness: 0
  });
  _wallMatCache[name] = mat;
  apply(mat);
}

document.querySelectorAll('.matcard').forEach(card => {
  card.addEventListener('click', () => {
    document.querySelectorAll('.matcard').forEach(c => c.classList.remove('active'));
    card.classList.add('active');
    applyWallMat(card.dataset.mat);
  });
});

// helpers
const mkBox = (mat, w, h, d, x=0, y=0, z=0) => {
  const m = new THREE.Mesh(new THREE.BoxGeometry(w,h,d), mat);
  m.position.set(x,y,z); m.castShadow=true; m.receiveShadow=true; return m; };
const mkCyl = (mat, r, h, x=0, y=0, z=0, segs=12) => {
  const m = new THREE.Mesh(new THREE.CylinderGeometry(r,r,h,segs), mat);
  m.position.set(x,y,z); m.castShadow=true; return m; };
const grp = (...parts) => { const g=new THREE.Group(); parts.forEach(p=>g.add(p)); return g; };

// ── Furniture definitions ─────────────────────────────────────────────────────
const FURNITURE = [
  {
    type:'chair', icon:'🪑', name:'Chair', dims:'50 × 50 cm',
    build() {
      const legs = [-0.18,0.18].flatMap(x=>[-0.18,0.18].map(z=>mkCyl(M.wood2,.025,.44,x,.22,z)));
      return grp(...legs,
        mkBox(M.wood, .50,.05,.50, 0,.46, 0),       // seat
        mkBox(M.wood, .50,.46,.05, 0,.70,-.225),     // back-rest
      );
    }
  },
  {
    type:'sofa', icon:'🛋️', name:'Sofa', dims:'200 × 90 cm',
    build() {
      return grp(
        mkBox(M.wood,  2.08,.08,1.00, 0,.04,  0),   // base plinth
        mkBox(M.fabric,1.80,.20, .80, 0,.20,  0),   // seat cushion
        mkBox(M.fabric,1.80,.44, .13, 0,.52,-.34),  // back
        mkBox(M.fab2,  .13,.40, .80,-.845,.40,0),   // left arm
        mkBox(M.fab2,  .13,.40, .80, .845,.40,0),   // right arm
      );
    }
  },
  {
    type:'bed_single', icon:'🛏️', name:'Single Bed', dims:'100 × 200 cm',
    build() {
      return grp(
        mkBox(M.wood,  1.06,.28,2.06, 0,.14, 0),    // frame
        mkBox(M.white,  .96,.18,1.90, 0,.36,.05),   // mattress
        mkBox(M.sheet,  .94,.06,1.38, 0,.54,.25),   // sheet
        mkBox(M.sheet,  .86,.22, .52, 0,.52,-.67),  // pillow
        mkBox(M.wood,  1.06,.62, .10, 0,.60,-1.01), // headboard
        mkBox(M.wood,  1.06,.24, .10, 0,.40, 1.01), // footboard
      );
    }
  },
  {
    type:'bed_double', icon:'🛏️', name:'Double Bed', dims:'160 × 200 cm',
    build() {
      return grp(
        mkBox(M.wood,  1.66,.28,2.06, 0,.14, 0),
        mkBox(M.white, 1.56,.18,1.90, 0,.36,.05),
        mkBox(M.sheet, 1.54,.06,1.38, 0,.54,.25),
        mkBox(M.sheet,  .60,.22, .52,-.40,.52,-.67),
        mkBox(M.sheet,  .60,.22, .52, .40,.52,-.67),
        mkBox(M.wood,  1.66,.66, .10, 0,.64,-1.01),
        mkBox(M.wood,  1.66,.26, .10, 0,.40, 1.01),
      );
    }
  },
  {
    type:'dining_table', icon:'🍽️', name:'Dining Table', dims:'120 × 80 cm',
    build() {
      const legs=[-0.52,0.52].flatMap(x=>[-0.32,0.32].map(z=>mkCyl(M.wood2,.04,.72,x,.36,z)));
      return grp(...legs, mkBox(M.wood,1.20,.05,.80, 0,.74, 0));
    }
  },
  {
    type:'desk', icon:'🖥️', name:'Desk', dims:'120 × 60 cm',
    build() {
      const legs=[-0.54,0.54].flatMap(x=>[-0.24,0.24].map(z=>mkCyl(M.metal,.025,.72,x,.36,z)));
      return grp(...legs,
        mkBox(M.wood,  1.20,.04, .60, 0,.74,  0),  // desktop
        mkBox(M.dark,  .38,.22, .03, .30,1.06,.06), // monitor
        mkBox(M.metal, .38,.04, .12, .30, .90,.01), // monitor neck
        mkBox(M.metal, .28,.02, .28, .30, .78,.01), // monitor base
      );
    }
  },
  {
    type:'bathtub', icon:'🛁', name:'Bathtub', dims:'80 × 170 cm',
    build() {
      return grp(
        mkBox(M.ceram, .80,.56,1.70, 0,.28, 0),     // outer shell
        mkBox(M.water, .62,.20,1.48, 0,.46, 0),     // water / interior
        mkBox(M.ceram, .80,.05,1.70, 0,.58, 0),     // rim
        mkCyl(M.metal,.022,.18,  .26,.65, 0),       // tap spout
        mkCyl(M.metal,.015,.12, -.26,.65, 0),       // drain
      );
    }
  },
  {
    type:'toilet', icon:'🚽', name:'Toilet', dims:'38 × 65 cm',
    build() {
      return grp(
        mkBox(M.ceram, .38,.36, .48, 0,.18,.06),    // bowl
        mkBox(M.ceram, .36,.05, .44, 0,.36,.06),    // seat ring
        mkBox(M.ceram, .38,.14, .20, 0,.46,-.20),   // cistern
        mkBox(M.ceram, .36,.02, .18, 0,.54,-.20),   // cistern lid
      );
    }
  },
  {
    type:'wardrobe', icon:'🗄️', name:'Wardrobe', dims:'120 × 220 cm',
    build() {
      return grp(
        mkBox(M.wood,  1.20,2.20, .60, 0,1.10, 0),  // body
        mkBox(M.wood2, .015,2.00, .58, 0,1.10,.01), // door divider
        mkCyl(M.metal,.016,.07, -.27,1.10, .31),    // handle L
        mkCyl(M.metal,.016,.07,  .27,1.10, .31),    // handle R
      );
    }
  },
];

// ── Raycaster helpers ─────────────────────────────────────────────────────────
const raycaster = new THREE.Raycaster();
function ndc(clientX, clientY) {
  const r = wrap.getBoundingClientRect();
  return new THREE.Vector2(
    ((clientX - r.left) / r.width)  *  2 - 1,
    ((clientY - r.top)  / r.height) * -2 + 1
  );
}
function hitFloor(clientX, clientY) {
  raycaster.setFromCamera(ndc(clientX, clientY), camera);
  const hits = raycaster.intersectObject(floorMesh);
  return hits.length ? hits[0].point : null;
}

// ── Selection ─────────────────────────────────────────────────────────────────
const placed = [];
let selected = null;
const selHint = document.getElementById('sel-hint');

function setSelected(obj) {
  // deselect previous
  if (selected && selected !== obj) {
    selected.traverse(n => {
      if (n.isMesh && n.userData._origHex !== undefined) {
        n.material = n.material.clone();
        n.material.emissiveIntensity = 0;
        n.material.color.setHex(n.userData._origHex);
        delete n.userData._origHex;
      }
    });
  }
  selected = obj;
  if (obj) {
    obj.traverse(n => {
      if (n.isMesh) {
        n.userData._origHex = n.material.color.getHex();
        n.material = n.material.clone();
        n.material.emissive = new THREE.Color(0x224488);
        n.material.emissiveIntensity = 0.4;
      }
    });
    selHint.style.display = 'block';
  } else {
    selHint.style.display = 'none';
  }
}

function deselect() {
  if (!selected) return;
  selected.traverse(n => {
    if (n.isMesh && n.userData._origHex !== undefined) {
      n.material = n.material.clone();
      n.material.emissiveIntensity = 0;
      n.material.color.setHex(n.userData._origHex);
      delete n.userData._origHex;
    }
  });
  selected = null;
  selHint.style.display = 'none';
}

function deleteSelected() {
  if (!selected) return;
  scene.remove(selected);
  placed.splice(placed.indexOf(selected), 1);
  selected = null;
  selHint.style.display = 'none';
}

// ── Drag from sidebar ─────────────────────────────────────────────────────────
const ghost = document.getElementById('ghost');
let draggingType = null;

document.addEventListener('mousemove', e => {
  if (!draggingType) return;
  ghost.style.left = e.clientX + 'px';
  ghost.style.top  = e.clientY + 'px';
});
document.addEventListener('mouseup', e => {
  if (!draggingType) return;
  ghost.style.display = 'none';
  const r = wrap.getBoundingClientRect();
  const overCanvas = e.clientX>=r.left && e.clientX<=r.right &&
                     e.clientY>=r.top  && e.clientY<=r.bottom;
  if (overCanvas) {
    const pt = hitFloor(e.clientX, e.clientY);
    if (pt) addFurniture(draggingType, pt);
  }
  draggingType = null;
});

function addFurniture(type, pos) {
  const def = FURNITURE.find(f => f.type === type);
  if (!def) return;
  const obj = def.build();
  obj.position.set(pos.x, 0, pos.z);
  obj.userData.isFurniture = true;
  scene.add(obj);
  placed.push(obj);
  deselect();
  setSelected(obj);
}

// ── Build sidebar cards ───────────────────────────────────────────────────────
const list = document.getElementById('furniture-list');
FURNITURE.forEach(def => {
  const card = document.createElement('div');
  card.className = 'fcard';
  card.innerHTML = `<span class="ficon">${def.icon}</span>
    <div><div class="fname">${def.name}</div><div class="fsize">${def.dims}</div></div>`;
  card.addEventListener('mousedown', e => {
    e.preventDefault();
    draggingType = def.type;
    ghost.textContent = def.icon + '  ' + def.name;
    ghost.style.display = 'block';
    ghost.style.left = e.clientX + 'px';
    ghost.style.top  = e.clientY + 'px';
  });
  list.appendChild(card);
});

// ── Click-to-select in scene ──────────────────────────────────────────────────
let pointerMoved = false;
renderer.domElement.addEventListener('pointerdown', () => pointerMoved = false);
renderer.domElement.addEventListener('pointermove', () => pointerMoved = true);
renderer.domElement.addEventListener('pointerup', e => {
  if (pointerMoved || draggingType || movingFurniture) return;
  raycaster.setFromCamera(ndc(e.clientX, e.clientY), camera);
  const hits = raycaster.intersectObjects(placed, true);
  if (hits.length) {
    let obj = hits[0].object;
    while (obj.parent && !obj.userData.isFurniture) obj = obj.parent;
    if (obj === selected) deselect(); else setSelected(obj);
  } else {
    deselect();
  }
});

// ── Drag to move / right-drag to rotate selected furniture ───────────────────
let movingFurniture   = false;
let rotatingFurniture = false;
let lastPointerX      = 0;

// Suppress browser context menu on the canvas so right-drag works cleanly
renderer.domElement.addEventListener('contextmenu', e => e.preventDefault());

renderer.domElement.addEventListener('pointerdown', e => {
  if (!selected) return;
  raycaster.setFromCamera(ndc(e.clientX, e.clientY), camera);
  if (!raycaster.intersectObjects([selected], true).length) return;
  controls.enabled = false;
  if (e.button === 2) {                    // right-click → rotate
    rotatingFurniture = true;
    lastPointerX = e.clientX;
  } else {                                 // left-click → move
    movingFurniture = true;
  }
});
renderer.domElement.addEventListener('pointermove', e => {
  if (movingFurniture && selected) {
    const pt = hitFloor(e.clientX, e.clientY);
    if (pt) selected.position.set(pt.x, 0, pt.z);
  }
  if (rotatingFurniture && selected) {
    const dx = e.clientX - lastPointerX;
    selected.rotation.y += dx * (Math.PI / 360);  // ~0.5° per pixel
    lastPointerX = e.clientX;
  }
});
renderer.domElement.addEventListener('pointerup', () => {
  movingFurniture   = false;
  rotatingFurniture = false;
  controls.enabled  = true;
});

// ── Scroll-wheel rotation (when hovering over selected furniture) ─────────────
// capture phase fires before OrbitControls' bubble-phase zoom handler
renderer.domElement.addEventListener('wheel', e => {
  if (!selected) return;
  raycaster.setFromCamera(ndc(e.clientX, e.clientY), camera);
  if (!raycaster.intersectObjects([selected], true).length) return;
  e.stopPropagation();
  e.preventDefault();
  selected.rotation.y -= e.deltaY * (Math.PI / 1200);  // ~15° per scroll notch
}, { capture: true, passive: false });

// ── Keyboard ──────────────────────────────────────────────────────────────────
window.addEventListener('keydown', e => {
  if (e.key === 'Delete' || e.key === 'Backspace') { e.preventDefault(); deleteSelected(); }
  if ((e.key === 'r' || e.key === 'R') && selected) selected.rotation.y += Math.PI / 2;
  if (e.key === 'Escape') deselect();
});

// ── Resize ────────────────────────────────────────────────────────────────────
function resize() {
  const w = wrap.clientWidth, h = wrap.clientHeight;
  renderer.setSize(w, h, false);
  camera.aspect = w / h;
  camera.updateProjectionMatrix();
}
new ResizeObserver(resize).observe(wrap);
resize();

// ── Render loop ───────────────────────────────────────────────────────────────
(function animate() { requestAnimationFrame(animate); controls.update(); renderer.render(scene, camera); })();

// ── Camera helpers ────────────────────────────────────────────────────────────
let isTop = false;
window.resetCamera = () => {
  const sz = modelBox ? modelBox.getSize(new THREE.Vector3()).length() : 20;
  camera.position.set(sz*.7, sz*.6, sz*.7);
  controls.target.set(0, modelBox ? modelBox.max.y*.4 : 1, 0);
  isTop = false; controls.update();
};
window.toggle2D = () => {
  isTop = !isTop;
  if (isTop) { camera.position.set(0, 60, 0.001); controls.target.set(0,0,0); }
  else window.resetCamera();
  controls.update();
};
let wireframe = false;
window.toggleWireframe = () => {
  wireframe = !wireframe;
  scene.traverse(n => {
    if (n.isMesh && n.material)
      (Array.isArray(n.material)?n.material:[n.material]).forEach(m=>m.wireframe=wireframe);
  });
};
</script>
</body>
</html>
"""


def find_free_port(start=8765):
    for port in range(start, start + 100):
        with socket.socket() as s:
            if s.connect_ex(("127.0.0.1", port)) != 0:
                return port
    return start


NEXTJS_URL = "http://localhost:3000"


def main():
    parser = argparse.ArgumentParser(description="Stage 5: Interactive 3D viewer")
    parser.add_argument("input", help="Path to .glb model from Stage 4")
    parser.add_argument("--port", type=int, default=0)
    args = parser.parse_args()

    glb_path = Path(args.input).resolve()
    if not glb_path.exists():
        print(f"[ERROR] GLB not found: {glb_path}")
        sys.exit(1)

    project_root = Path(__file__).parent
    try:
        model_rel = "/" + glb_path.relative_to(project_root).as_posix()
        serve_dir = project_root
    except ValueError:
        model_rel = f"/{glb_path.name}"
        serve_dir = glb_path.parent

    port = args.port or find_free_port()

    class Handler(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *a, **kw):
            super().__init__(*a, directory=str(serve_dir), **kw)
        def log_message(self, *_):
            pass

    server = http.server.HTTPServer(("127.0.0.1", port), Handler)

    # GLB is served by the local file server; Next.js viewer loads it via ?model=
    model_url = f"http://127.0.0.1:{port}{model_rel}"
    viewer_url = f"{NEXTJS_URL}/viewer?model={model_url}"

    print(f"\n[Stage 5] Serving GLB on port {port}")
    print(f"  Model   : {glb_path.name}")
    print(f"  Viewer  : {viewer_url}")
    print(f"\n  Make sure the Next.js app is running (npm run dev in D:/PlanLift/app)")
    print(f"  Press Ctrl+C to stop.\n")

    threading.Timer(1.0, lambda: webbrowser.open(viewer_url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[Stage 5] Stopped.")


if __name__ == "__main__":
    main()
