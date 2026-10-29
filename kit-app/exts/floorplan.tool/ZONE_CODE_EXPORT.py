"""
Camera Zone (occlusion-aware floor coverage) code, extracted from
floorplan.tool/floorplan/tool/extension.py for external review.

This is NOT a standalone runnable file — it's copied verbatim from inside
a much larger extension.py, where all of this lives as nested closures
inside one big `_build_ui()` method of the extension class. The pieces
below are everything needed to understand/reproduce the zone system:

  1. Module-level constant (CAM_COLORS)
  2. Outer-scope state these closures close over (paths, dicts, lists)
  3. A few outer-scope helper functions the zone code calls
  4. The full zone system itself, verbatim from extension.py lines 695-1105

Algorithm summary: for each camera, cast a dense grid of rays through its
frustum (density set by _zone_quality), ray-march each one against a
manually cached list of every triangle in the scene (Moeller-Trumbore,
with a bounding-sphere broad-phase for speed — NOT PhysX/omni.physx scene
queries, this build doesn't have PhysX available). A ray only counts as
"visible floor" if its FIRST hit is near-horizontal, facing up, and at
floor height — i.e. occluded by a wall/pillar/furniture piece first,
it's dropped. The final zone polygon is the convex hull of every
surviving floor sample point.
"""

from pxr import Gf, Sdf, Usd, UsdGeom, UsdShade, Vt

# ─────────────────────────────────────────────────────────────────────────
# 1. Module-level constant
# ─────────────────────────────────────────────────────────────────────────

# Unique color per camera (RGBA packed as Gf.Vec3f RGB)
CAM_COLORS = [
    Gf.Vec3f(1.0, 0.3, 0.3),   # red
    Gf.Vec3f(0.3, 1.0, 0.45),  # green
    Gf.Vec3f(0.3, 0.65, 1.0),  # blue
    Gf.Vec3f(1.0, 0.92, 0.2),  # yellow
    Gf.Vec3f(1.0, 0.52, 0.1),  # orange
    Gf.Vec3f(0.8, 0.3, 1.0),   # purple
    Gf.Vec3f(0.2, 1.0, 0.9),   # cyan
    Gf.Vec3f(1.0, 0.42, 0.72), # pink
]


def _walk_prims(stage):
    """Recursively yield all prims. Replaces stage.Traverse() which broke in USD 25.11."""
    def _rec(prim):
        for child in prim.GetChildren():
            yield child
            yield from _rec(child)
    yield from _rec(stage.GetPseudoRoot())


# ─────────────────────────────────────────────────────────────────────────
# 2. Outer-scope state (set once per-extension-instance, inside _build_ui)
# ─────────────────────────────────────────────────────────────────────────
#
#   children      = stage.GetPseudoRoot().GetChildren()
#   STAGE_ROOT     = str(children[0].GetPath()) if children else "/World"
#   RIG_PATH       = f"{STAGE_ROOT}/FloorPlanRig"
#   CAM_ZONE_PATH  = f"{RIG_PATH}/CamZones"
#
#   _cam_list      = []            # list of camera prim paths (populated elsewhere)
#   _cam_colors    = {}            # path -> Gf.Vec3f color (assigned from CAM_COLORS when a camera is created)
#   _cam_zones     = {}            # path -> True if a zone mesh currently exists for that camera
#   _zone_visible  = {}            # path -> bool
#   _scene_tris    = []            # cached world-space triangles, refreshed by _cache_scene_triangles()
#   _zone_quality  = ["medium"]    # "low"/"medium"/"high" -> ray grid density (see _zone_ray_grid)
#   _cam_range     = [10.0]        # zone raycast range in scene units, driven by the Range slider UI


# ─────────────────────────────────────────────────────────────────────────
# 3. Outer-scope helpers the zone code calls
# ─────────────────────────────────────────────────────────────────────────

def _rs():
    """Refresh the stage reference (stage may have been reopened)."""
    import omni.usd
    ctx = omni.usd.get_context()
    return ctx.get_stage()


def get_floor_info():
    """Returns (center_x, center_y, floor_z, width, depth, unit, ten_feet_in_units)
    computed from the world bbox union of every real mesh in the scene
    (skipping our own FloorPlanRig subtree)."""
    stage = _rs()
    bc = UsdGeom.BBoxCache(Usd.TimeCode.Default(),
        [UsdGeom.Tokens.default_, UsdGeom.Tokens.render], useExtentsHint=True)
    total = Gf.Range3d()
    for prim in _walk_prims(stage):
        if "FloorPlanRig" in str(prim.GetPath()): continue
        if prim.IsA(UsdGeom.Boundable):
            try:
                bb = bc.ComputeWorldBound(prim)
                total = Gf.Range3d.GetUnion(total, bb.ComputeAlignedRange())
            except: pass
    if total.IsEmpty(): return 0.0,0.0,0.0,10.0,10.0,"m",3.048
    mn=total.GetMin(); mx=total.GetMax()
    w=abs(mx[0]-mn[0]); d=abs(mx[1]-mn[1])
    cx=(mn[0]+mx[0])/2; cy=(mn[1]+mx[1])/2; fz=mn[2]
    extent=max(w,d,abs(mx[2]-mn[2]))
    unit="cm" if extent>100 else "m"
    ten_feet=304.8 if unit=="cm" else 3.048
    return cx,cy,fz,w,d,unit,ten_feet


def set_status(msg):
    """Writes to the status label + console. Stubbed here (no lbl_status in this export)."""
    print(f"[UI] {msg.replace(chr(10),' | ')}")


def _schedule_rebuild():
    """Marks the camera-list UI dirty so it rebuilds next frame. Stubbed here — the
    real implementation lives in extension.py and touches actual ui.Frame widgets."""
    pass


# ─────────────────────────────────────────────────────────────────────────
# 4. Camera Zones (occlusion-aware floor coverage) — verbatim from
#    extension.py lines 695-1105
# ─────────────────────────────────────────────────────────────────────────

# ── Camera Zones (occlusion-aware floor coverage) ───────────────────
# Faithful port of Piyush's FloorPlanPanel zone system: dense grid
# raycast against a manually cached triangle list, first-hit
# classification (floor vs wall/pillar/furniture), convex hull of
# the surviving floor points. No PhysX, no frustum-viz debug tool.

def _cache_scene_triangles():
    """Cache world-space triangles for every real Mesh in the scene
    (skipping our own FloorPlanRig subtree) -- used by the dense
    occlusion-aware floor-visibility raycast in _generate_zone.
    Includes ALL geometry (floor, walls, furniture, pillars); the
    caller classifies each hit afterwards rather than filtering here,
    since a single ray needs to know not just "did I hit something"
    but "was it the floor or an obstacle". Polygons are
    fan-triangulated from vertex 0, correct for the simple convex
    wall/floor/furniture quads this pipeline generates.

    Each triangle also stores its bounding-sphere center/radius so
    _raycast_nearest can cheaply reject most triangles with a simple
    ray/sphere test before running the full (much more expensive)
    Moeller-Trumbore intersection -- without this, dense sampling
    (hundreds to thousands of rays per zone) against every triangle
    in a furniture-heavy scene was slow enough to stall Kit."""
    global _scene_tris
    s = _rs()
    tris = []
    if s:
        for prim in s.Traverse():
            if "FloorPlanRig" in str(prim.GetPath()):
                continue
            if not prim.IsA(UsdGeom.Mesh):
                continue
            try:
                mesh   = UsdGeom.Mesh(prim)
                pts    = mesh.GetPointsAttr().Get()
                counts = mesh.GetFaceVertexCountsAttr().Get()
                idxs   = mesh.GetFaceVertexIndicesAttr().Get()
                if not pts or not counts or not idxs:
                    continue
                xf  = UsdGeom.Xformable(prim)
                mat = xf.ComputeLocalToWorldTransform(Usd.TimeCode.Default())
                wpts = [mat.Transform(Gf.Vec3d(float(p[0]), float(p[1]), float(p[2]))) for p in pts]
                offset = 0
                for cnt in counts:
                    if cnt >= 3:
                        i0 = idxs[offset]
                        for k in range(1, cnt - 1):
                            i1 = idxs[offset + k]
                            i2 = idxs[offset + k + 1]
                            v0, v1, v2 = wpts[i0], wpts[i1], wpts[i2]
                            ccx = (v0[0]+v1[0]+v2[0]) / 3.0
                            ccy = (v0[1]+v1[1]+v2[1]) / 3.0
                            ccz = (v0[2]+v1[2]+v2[2]) / 3.0
                            radius = max(
                                ((v0[0]-ccx)**2 + (v0[1]-ccy)**2 + (v0[2]-ccz)**2) ** 0.5,
                                ((v1[0]-ccx)**2 + (v1[1]-ccy)**2 + (v1[2]-ccz)**2) ** 0.5,
                                ((v2[0]-ccx)**2 + (v2[1]-ccy)**2 + (v2[2]-ccz)**2) ** 0.5)
                            tris.append((v0, v1, v2, ccx, ccy, ccz, radius))
                    offset += cnt
            except Exception:
                pass
    _scene_tris = tris
    return tris

def _ray_triangle(ox, oy, oz, dx, dy, dz, v0, v1, v2):
    """Moeller-Trumbore ray/triangle intersection. Returns the hit
    distance t (> 0) or None."""
    eps = 1e-9
    e1x, e1y, e1z = v1[0]-v0[0], v1[1]-v0[1], v1[2]-v0[2]
    e2x, e2y, e2z = v2[0]-v0[0], v2[1]-v0[1], v2[2]-v0[2]
    px = dy*e2z - dz*e2y
    py = dz*e2x - dx*e2z
    pz = dx*e2y - dy*e2x
    det = e1x*px + e1y*py + e1z*pz
    if -eps < det < eps:
        return None
    inv_det = 1.0 / det
    tx, ty, tz = ox-v0[0], oy-v0[1], oz-v0[2]
    u = (tx*px + ty*py + tz*pz) * inv_det
    if u < 0.0 or u > 1.0:
        return None
    qx = ty*e1z - tz*e1y
    qy = tz*e1x - tx*e1z
    qz = tx*e1y - ty*e1x
    v = (dx*qx + dy*qy + dz*qz) * inv_det
    if v < 0.0 or u + v > 1.0:
        return None
    t = (e2x*qx + e2y*qy + e2z*qz) * inv_det
    return t if t > 1e-6 else None

def _raycast_nearest(ox, oy, oz, dx, dy, dz, max_t=None):
    """Closest-hit (distance, triangle) against the cached scene
    triangles, or (None, None) if the ray misses everything. Assumes
    (dx,dy,dz) is unit length. Each triangle is first tested against
    its own bounding sphere (a cheap, standard ray/sphere rejection)
    before running the full Moeller-Trumbore test — see
    _cache_scene_triangles for why."""
    best_t, best_tri = None, None
    limit = max_t if max_t is not None else float('inf')
    for v0, v1, v2, ccx, ccy, ccz, radius in _scene_tris:
        ocx, ocy, ocz = ccx-ox, ccy-oy, ccz-oz
        t_closest = ocx*dx + ocy*dy + ocz*dz
        cur_limit = best_t if (best_t is not None and best_t < limit) else limit
        if t_closest - radius > cur_limit or t_closest + radius < 0:
            continue
        dist_sq = ocx*ocx + ocy*ocy + ocz*ocz
        if dist_sq - t_closest*t_closest > radius*radius:
            continue
        t = _ray_triangle(ox, oy, oz, dx, dy, dz, v0, v1, v2)
        if t is not None and t <= limit and (best_t is None or t < best_t):
            best_t, best_tri = t, (v0, v1, v2)
    return best_t, best_tri

def _zone_ray_grid():
    """(cols, rows) sample density for the occlusion-aware floor scan
    in _generate_zone, from _zone_quality[0] ("low"/"medium"/"high")."""
    return {"low": (20, 15), "medium": (40, 30), "high": (80, 60)}.get(
        _zone_quality[0], (40, 30))

def _zone_reach_cap(R):
    """Safety cap for frustum-corner raycasts: large enough to reach
    any wall in the room regardless of the (often small, user-set)
    Range value, so the actual wall distance — not this cap — is what
    determines the zone/FOV shape. Falls back to R itself if the scene
    has no extent yet."""
    _, _, _, sw, sd, _, _ = get_floor_info()
    room_diag = (sw**2 + sd**2) ** 0.5
    return max(float(R), room_diag * 1.5) if room_diag > 0 else float(R)

def _make_zone_material(s, mat_path, color, opacity):
    """Create a semi-transparent UsdPreviewSurface material."""
    if s.GetPrimAtPath(mat_path): s.RemovePrim(mat_path)
    usd_mat = UsdShade.Material.Define(s, mat_path)
    shader  = UsdShade.Shader.Define(s, mat_path + "/Shader")
    shader.CreateIdAttr("UsdPreviewSurface")
    shader.CreateInput("diffuseColor",  Sdf.ValueTypeNames.Color3f).Set(color)
    shader.CreateInput("emissiveColor", Sdf.ValueTypeNames.Color3f).Set(
        Gf.Vec3f(color[0]*0.25, color[1]*0.25, color[2]*0.25))
    shader.CreateInput("opacity",       Sdf.ValueTypeNames.Float).Set(opacity)
    shader.CreateInput("roughness",     Sdf.ValueTypeNames.Float).Set(1.0)
    shader.CreateInput("metallic",      Sdf.ValueTypeNames.Float).Set(0.0)
    # roughness=1.0 alone still leaves a Fresnel-driven specular lobe
    # that's very visible at grazing/oblique viewing angles under RTX
    # path tracing (exactly the angle a floor zone is usually seen
    # from) — zeroing specularColor under the specular workflow kills
    # that reflectance entirely instead of just widening it.
    shader.CreateInput("useSpecularWorkflow", Sdf.ValueTypeNames.Int).Set(1)
    shader.CreateInput("specularColor", Sdf.ValueTypeNames.Color3f).Set(Gf.Vec3f(0, 0, 0))
    usd_mat.CreateSurfaceOutput().ConnectToSource(shader.ConnectableAPI(), "surface")
    return usd_mat

def _detect_floor_z(s, cam_z, cam_x=None, cam_y=None):
    """Find the actual floor surface Z — the lowest large flat mesh in the scene."""
    bc = UsdGeom.BBoxCache(Usd.TimeCode.Default(),
        [UsdGeom.Tokens.default_, UsdGeom.Tokens.render], useExtentsHint=True)
    candidates = []
    for prim in s.Traverse():
        if "FloorPlanRig" in str(prim.GetPath()): continue
        if not prim.IsA(UsdGeom.Mesh): continue
        try:
            r       = bc.ComputeWorldBound(prim).ComputeAlignedRange()
            mn_p, mx_p = r.GetMin(), r.GetMax()
            xy_area = (mx_p[0]-mn_p[0]) * (mx_p[1]-mn_p[1])
            z_thick = mx_p[2] - mn_p[2]
            top_z   = float(mx_p[2])
            if xy_area > 0.5 and z_thick < max(2.0, abs(cam_z) * 0.2) and top_z < cam_z:
                candidates.append((top_z, xy_area))
        except: pass
    if candidates:
        max_area  = max(c[1] for c in candidates)
        sig       = [c for c in candidates if c[1] >= max_area * 0.3]
        sig.sort(key=lambda c: c[0], reverse=True)
        return sig[0][0]          # highest top_z below camera = visible floor surface
    _, _, fz, _, _, _, _ = get_floor_info()
    return fz

def _generate_zone(path, _skip_cache_refresh=False):
    s = _rs()
    if not s: return
    cam_prim = s.GetPrimAtPath(path)
    if not cam_prim or not cam_prim.IsValid(): return

    cam_name  = path.split("/")[-1]
    zone_root = CAM_ZONE_PATH + "/" + cam_name

    for p, t in [(RIG_PATH, "Xform"),
                 (CAM_ZONE_PATH, "Scope"),
                 (zone_root, "Scope")]:
        if not s.GetPrimAtPath(p):
            s.DefinePrim(p, t)

    cam   = UsdGeom.Camera(cam_prim)
    focal = cam.GetFocalLengthAttr().Get()
    h_ap  = cam.GetHorizontalApertureAttr().Get()
    v_ap  = cam.GetVerticalApertureAttr().Get()
    if not focal or focal == 0: return

    th = (h_ap / 2.0) / focal
    tv = (v_ap / 2.0) / focal
    R  = float(_cam_range[0])

    xf     = UsdGeom.Xformable(cam_prim)
    w_mat  = xf.ComputeLocalToWorldTransform(Usd.TimeCode.Default())
    origin = w_mat.Transform(Gf.Vec3d(0, 0, 0))
    color  = _cam_colors.get(path, Gf.Vec3f(1.0, 1.0, 1.0))

    # Remove any leftover cone prim from a previous run
    cone_path = zone_root + "/FrustumCone"
    if s.GetPrimAtPath(cone_path):
        s.RemovePrim(cone_path)

    # ── OCCLUSION-AWARE FLOOR VISIBILITY SCAN ───────────────────────
    # Instead of projecting just the 4 frustum corners (which has no
    # way to know a wall, pillar, or piece of furniture is in the way
    # partway through the room), sample a dense grid of rays across
    # the camera's actual image and raycast each one against the real
    # scene geometry. Only rays whose FIRST hit is the floor itself
    # count as "visible floor" — a ray that hits a wall, a pillar, or
    # furniture first is dropped. The final polygon is the convex
    # hull of every visible-floor sample (see below, after the scan)
    # — informed by hundreds of real occlusion-tested points, so far
    # more accurate than the old 4-corner-only hull, though a convex
    # hull still can't carve out a hole for something like a pillar
    # sitting in the middle of an otherwise-open room.
    fz = _detect_floor_z(s, float(origin[2]), float(origin[0]), float(origin[1]))
    draw_z = fz + 0.01    # 1 cm above floor surface to avoid Z-fighting

    _, _, _, _rw, _rd, _unit, _tf = get_floor_info()
    floor_tol = max(2.0, _tf * 0.05)   # how close a hit must be to fz to count as "the floor"
    cap = _zone_reach_cap(R)

    # Rebuilding this from scratch (walking every mesh in the stage)
    # is the single most expensive part of generating a zone. A lone
    # "Zone" click always refreshes it (the scene could genuinely have
    # changed), but "Generate All Zones" calls this once per camera
    # and passes _skip_cache_refresh=True after the first so the same
    # unchanged scene isn't rescanned N times in a row.
    if not _skip_cache_refresh:
        _cache_scene_triangles()

    cols, rows = _zone_ray_grid()
    ox, oy, oz = float(origin[0]), float(origin[1]), float(origin[2])
    grid = [[None] * (cols + 1) for _ in range(rows + 1)]

    for j in range(rows + 1):
        ndc_y = -tv + (2.0 * tv) * j / rows
        for i in range(cols + 1):
            ndc_x = -th + (2.0 * th) * i / cols
            local_dir = Gf.Vec3d(ndc_x, ndc_y, -1.0)
            llen = local_dir.GetLength()
            if llen < 1e-9:
                continue
            w_dir = Gf.Vec3d(*w_mat.TransformDir(local_dir / llen))
            wlen = w_dir.GetLength()
            if wlen < 1e-9:
                continue
            dxf, dyf, dzf = float(w_dir[0]/wlen), float(w_dir[1]/wlen), float(w_dir[2]/wlen)

            t, tri = _raycast_nearest(ox, oy, oz, dxf, dyf, dzf, max_t=cap)
            if t is None or t > cap or tri is None:
                continue

            v0, v1, v2 = tri
            e1 = v1 - v0
            e2 = v2 - v0
            n  = Gf.Cross(e1, e2)
            n_len = n.GetLength()
            if n_len < 1e-9:
                continue
            nz = n[2] / n_len
            hit_z = oz + dzf * t
            if nz > 0.7 and abs(hit_z - fz) < floor_tol:   # near-horizontal, facing up, at floor height
                grid[j][i] = (ox + dxf*t, oy + dyf*t)

    # A grid-of-quads mesh (one quad per fully-visible sample cell)
    # gives a hard, blocky "staircase" boundary at the grid's
    # resolution — correct, but visually rough, and cranking ray
    # density to smooth it out risks the exact slowdown that caused
    # the app to hang before the broad-phase raycast filter existed.
    # Taking the convex hull of every visible-floor sample point
    # instead gives a clean, smooth polygon informed by (and bounded
    # by) hundreds of real occlusion-tested points — far more
    # accurate than the old 4-corner-only hull, though — being
    # convex — it can't carve out a hole for something like a pillar
    # sitting in the middle of an otherwise-open room.
    sample_pts = [p for row in grid for p in row if p is not None]

    def _hull_ccw(pts):
        pts = sorted(set((round(p[0], 6), round(p[1], 6)) for p in pts))
        if len(pts) <= 2:
            return pts
        def _cross(o, a, b):
            return (a[0]-o[0])*(b[1]-o[1]) - (a[1]-o[1])*(b[0]-o[0])
        lo = []
        for p in pts:
            while len(lo) >= 2 and _cross(lo[-2], lo[-1], p) <= 0:
                lo.pop()
            lo.append(p)
        hi = []
        for p in reversed(pts):
            while len(hi) >= 2 and _cross(hi[-2], hi[-1], p) <= 0:
                hi.pop()
            hi.append(p)
        return lo[:-1] + hi[:-1]

    hull = _hull_ccw(sample_pts)
    verts = [Gf.Vec3f(p[0], p[1], draw_z) for p in hull]
    tri_counts, tri_indices = [], []
    for i in range(1, len(hull) - 1):
        tri_counts.append(3)
        tri_indices.extend([0, i, i + 1])

    if len(verts) >= 3:
        floor_path = zone_root + "/FloorZone"
        if s.GetPrimAtPath(floor_path): s.RemovePrim(floor_path)
        floor_mesh = UsdGeom.Mesh.Define(s, floor_path)
        n = len(verts)
        floor_mesh.GetPointsAttr().Set(Vt.Vec3fArray(verts))
        floor_mesh.GetFaceVertexCountsAttr().Set(Vt.IntArray(tri_counts))
        floor_mesh.GetFaceVertexIndicesAttr().Set(Vt.IntArray(tri_indices))
        floor_mesh.GetSubdivisionSchemeAttr().Set("none")
        floor_mesh.GetDoubleSidedAttr().Set(True)
        floor_mesh.GetNormalsAttr().Set(Vt.Vec3fArray([Gf.Vec3f(0,0,1)] * n))
        floor_mesh.SetNormalsInterpolation(UsdGeom.Tokens.vertex)
        fc = Gf.Vec3f(min(color[0]*1.3, 1.0),
                      min(color[1]*1.3, 1.0),
                      min(color[2]*1.3, 1.0))
        floor_mesh.GetDisplayColorAttr().Set(Vt.Vec3fArray([fc] * n))
        floor_mesh.GetDisplayOpacityAttr().Set(Vt.FloatArray([0.6] * n))
        floor_mat = _make_zone_material(s, zone_root + "/FloorMat", fc, 0.6)
        UsdShade.MaterialBindingAPI(s.GetPrimAtPath(floor_path)).Bind(floor_mat)

    _cam_zones[path]    = True
    _zone_visible[path] = True
    _schedule_rebuild()
    set_status(f"Zone generated: {cam_name}")

def _remove_zone(path):
    s = _rs()
    if not s: return
    cam_name  = path.split("/")[-1]
    zone_root = CAM_ZONE_PATH + "/" + cam_name
    if s.GetPrimAtPath(zone_root):
        s.RemovePrim(zone_root)
    _cam_zones.pop(path, None)
    _zone_visible.pop(path, None)

def _toggle_zone(path):
    s = _rs()
    if not s or not _cam_zones.get(path): return
    cam_name  = path.split("/")[-1]
    zone_root = CAM_ZONE_PATH + "/" + cam_name
    prim = s.GetPrimAtPath(zone_root)
    if not prim or not prim.IsValid(): return
    vis = not _zone_visible.get(path, True)
    _zone_visible[path] = vis
    img = UsdGeom.Imageable(prim)
    if vis: img.MakeVisible()
    else:   img.MakeInvisible()
    _schedule_rebuild()

def _generate_all_zones():
    # Scan the scene's triangles ONCE for the whole batch instead of
    # once per camera — with several cameras this was the difference
    # between one full-stage scan and N of them back to back, which
    # is where most of "Generate All Zones" taking a long time came
    # from (see _generate_zone's _skip_cache_refresh).
    _cache_scene_triangles()
    for path in list(_cam_list):
        _generate_zone(path, _skip_cache_refresh=True)
    set_status("All zones generated.")

def _show_all_zones():
    s = _rs()
    if not s: return
    for path in list(_cam_list):
        if not _cam_zones.get(path): continue
        cam_name  = path.split("/")[-1]
        zone_root = CAM_ZONE_PATH + "/" + cam_name
        prim = s.GetPrimAtPath(zone_root)
        if prim and prim.IsValid():
            UsdGeom.Imageable(prim).MakeVisible()
            _zone_visible[path] = True
    _schedule_rebuild()
    set_status("All zones shown.")

def _hide_all_zones():
    s = _rs()
    if not s: return
    for path in list(_cam_list):
        if not _cam_zones.get(path): continue
        cam_name  = path.split("/")[-1]
        zone_root = CAM_ZONE_PATH + "/" + cam_name
        prim = s.GetPrimAtPath(zone_root)
        if prim and prim.IsValid():
            UsdGeom.Imageable(prim).MakeInvisible()
            _zone_visible[path] = False
    _schedule_rebuild()
    set_status("All zones hidden.")

def _on_range_change(model):
    _cam_range[0] = model.get_value_as_float()
    # Regenerate all existing zones with new range — one triangle-cache
    # scan for the whole batch, same reasoning as _generate_all_zones.
    _cache_scene_triangles()
    for path in list(_cam_list):
        if _cam_zones.get(path):
            was_hidden = not _zone_visible.get(path, True)
            _generate_zone(path, _skip_cache_refresh=True)
            if was_hidden:
                _toggle_zone(path)
