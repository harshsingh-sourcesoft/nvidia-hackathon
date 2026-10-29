"""
glass_fix.py  —  Standalone glass-material patcher
Run with Isaac Sim's Python:
    D:\ISAACSIM\isaacsim\python.bat D:\PlanLift\PlanLift\glass_fix.py

Finds the most-recently-converted USD in output/isaac_tmp/,
detects every transparent material (opacity < 0.95 from the GLTF
alphaMode=BLEND export), replaces them all with a clean 20%-opacity
glass UsdPreviewSurface, and saves the USD in-place.
"""

import sys
from pathlib import Path

USD_DIR = Path(r"D:\PlanLift\PlanLift\output\isaac_tmp")

# ── Pick the most recent *.usd (skip any that are tiny / corrupt) ─────────────
usds = sorted(
    [f for f in USD_DIR.glob("*.usd") if f.stat().st_size > 10_000],
    key=lambda f: f.stat().st_mtime,
    reverse=True,
)
if not usds:
    print("[glass_fix] ERROR: no USD files found in", USD_DIR)
    sys.exit(1)

USD_PATH = usds[0]
print(f"[glass_fix] Opening: {USD_PATH}  ({USD_PATH.stat().st_size:,} bytes)")

from pxr import Usd, UsdShade, Sdf

stage = Usd.Stage.Open(str(USD_PATH))
if not stage:
    print("[glass_fix] ERROR: could not open USD stage")
    sys.exit(1)

all_prims = list(stage.Traverse())
print(f"[glass_fix] Prims: {len(all_prims)} total")

# ── Dump every Mesh → Material binding ────────────────────────────────────────
print("\n[glass_fix] === Mesh → Material bindings ===")
for p in all_prims:
    if p.GetTypeName() == "Mesh":
        bound = UsdShade.MaterialBindingAPI(p).GetDirectBinding().GetMaterialPath()
        print(f"  MESH  {p.GetPath()}  ->  {bound}")

# ── Dump every UsdPreviewSurface shader with its opacity ─────────────────────
print("\n[glass_fix] === UsdPreviewSurface shaders ===")
glass_mat_paths = set()
for p in all_prims:
    if p.GetTypeName() != "Shader":
        continue
    shader = UsdShade.Shader(p)
    id_attr = shader.GetIdAttr()
    if not (id_attr and id_attr.IsValid() and id_attr.Get() == "UsdPreviewSurface"):
        continue
    op_in = shader.GetInput("opacity")
    op_v  = op_in.Get() if (op_in and op_in.IsValid()) else None
    dc_in = shader.GetInput("diffuseColor")
    dc_v  = dc_in.Get() if (dc_in and dc_in.IsValid()) else None
    mat_path = p.GetPath().GetParentPath()
    print(f"  SHADER {p.GetPath()}  opacity={op_v}  diffuse={dc_v}")
    if op_v is not None and op_v < 0.95:
        glass_mat_paths.add(mat_path)
        print(f"    --> GLASS DETECTED")

print(f"\n[glass_fix] Found {len(glass_mat_paths)} glass material(s): {[str(x) for x in glass_mat_paths]}")

if not glass_mat_paths:
    print("[glass_fix] No transparent materials found — nothing to patch.")
    print("[glass_fix] Check if the GLB was exported with the glass pane (opacity=0.2, transparent=true)")
    sys.exit(0)

# ── Create OmniGlass MDL material (same as right-click → Create → OmniGlass) ──
gm_path = Sdf.Path("/PlanLift_GlassMat")
gm = UsdShade.Material.Define(stage, gm_path)
gs = UsdShade.Shader.Define(stage, str(gm_path) + "/Shader")
gs.SetSourceAsset(Sdf.AssetPath("OmniGlass.mdl"), "mdl")
gs.SetSourceAssetSubIdentifier("OmniGlass", "mdl")
gs.CreateInput("glass_color",        Sdf.ValueTypeNames.Color3f).Set((0.9, 0.95, 1.0))
gs.CreateInput("glass_ior",          Sdf.ValueTypeNames.Float).Set(1.491)
gs.CreateInput("frosting_roughness", Sdf.ValueTypeNames.Float).Set(0.0)
gm.CreateSurfaceOutput("mdl").ConnectToSource(gs.ConnectableAPI(), "out")
gm.CreateDisplacementOutput("mdl").ConnectToSource(gs.ConnectableAPI(), "out")
gm.CreateVolumeOutput("mdl").ConnectToSource(gs.ConnectableAPI(), "out")
print(f"[glass_fix] Created OmniGlass material at {gm_path}")

# ── Rebind every mesh that was using a detected glass material ─────────────────
count = 0
for p in all_prims:
    if p.GetTypeName() != "Mesh":
        continue
    binding = UsdShade.MaterialBindingAPI(p)
    if binding.GetDirectBinding().GetMaterialPath() in glass_mat_paths:
        binding.Bind(gm)
        count += 1
        print(f"[glass_fix] Rebound: {p.GetPath()}")

print(f"\n[glass_fix] Rebound {count} mesh prim(s) to PlanLift_GlassMat")

# ── Save ──────────────────────────────────────────────────────────────────────
stage.GetRootLayer().Save()
print(f"[glass_fix] Saved: {USD_PATH}")
print("[glass_fix] Done. Now open this USD in Isaac Sim.")
