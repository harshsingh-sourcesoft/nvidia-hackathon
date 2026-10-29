"""Floor Plan Tool — Kit Extension (v16 — USD 25.11 compatible)"""
import asyncio
import omni.ext
import omni.kit.app
import omni.ui as ui
from pxr import Gf


def _walk_prims(stage):
    """Recursively yield all prims. Replaces stage.Traverse() which broke in USD 25.11."""
    def _rec(prim):
        for child in prim.GetChildren():
            yield child
            yield from _rec(child)
    yield from _rec(stage.GetPseudoRoot())


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


# Module-level instance reference so Floor Plan View can add cameras to the list.
_INSTANCE = None  # type: FloorPlanToolExtension | None


class FloorPlanToolExtension(omni.ext.IExt):

    def on_startup(self, ext_id: str):
        global _INSTANCE
        _INSTANCE = self
        print("[FPT] on_startup — waiting for stage...")
        self._win             = None
        self._vp_manipulators_disabled = False   # whether we turned off viewport nav/selection
        self._vp_win_for_manipulators  = None    # viewport window whose layers we hid, for restore
        self._drop_sub        = None   # viewport drop-listener subscription
        self._ds              = {}     # shared state accessible from drop callbacks
        self._drag_screen_pos  = (-1.0, -1.0)   # screen coords cached by update poller
        self._update_sub       = None            # app update subscription for cursor poll
        self._drag_preset_idx  = 0              # preset index chosen by the active drag button
        # Joystick state
        self._js_win_origin   = None            # (sx, sy) client-area origin, cached per drag
        self._js_active       = False
        self._js_dx           = 0.0
        self._js_dy           = 0.0
        self._js_placer       = None
        self._js_bg_rect      = None
        self._js_drag_sub     = None
        self._js_cam_getter   = lambda: None
        self._js_speed_getter = lambda: 0.5
        self._js_stage_getter = lambda: None
        self._js_max_r        = 43.0
        self._js_thumb_r      = 18.0
        self._js_cx           = 65.0
        self._js_coord_lbl    = None            # live X/Y readout under the Move X/Y pad
        # Vertical height slider (was a momentary joystick — now an absolute
        # 0..max_ft slider: thumb position IS the camera's actual height,
        # doesn't spring back to center on release).
        self._jsv_active      = False
        self._jsv_placer      = None
        self._jsv_bg_rect     = None
        self._jsv_drag_sub    = None
        self._jsv_center_win  = None            # (sx, sy) cursor pos at press — Y used for drag delta
        self._jsv_max_r       = 43.0
        self._jsv_thumb_r     = 18.0
        self._jsv_cx          = 20.0
        self._jsv_cy          = 65.0
        self._jsv_coord_lbl   = None            # live height readout inside the slider
        self._jsv_height_ft   = 0.0             # current slider value (0..self._jsv_max_ft)
        self._jsv_max_ft      = 10.0            # slider range top — matches this pipeline's
                                                 # existing "10 ft" unit-scale convention (see
                                                 # get_floor_info's tf = 10ft-in-stage-units)
        self._jsv_press_value = 0.0             # height at the moment the slider was pressed
        self._jsv_cached_fz   = None            # floor base-Z, computed once per drag (get_floor_info walks the whole stage)
        self._jsv_cached_upf  = None            # units-per-foot, cached with _jsv_cached_fz
        self._jsv_cached_xy   = (0.0, 0.0)      # camera X/Y frozen for the drag (only Z changes)
        self._jsv_track_h     = 130.0           # pixel height of the slider track (set at build)
        self._jsv_sync_fn     = lambda v: None  # set during _build_ui; repositions thumb+label
        self._jsv_read_height_fn = lambda: 0.0  # set during _build_ui; reads active cam's height
        # Rotation joystick state (Pitch X / Roll Z — Yaw Y is not controlled here)
        self._jsr_active      = False
        self._jsr_dx          = 0.0
        self._jsr_dy          = 0.0
        self._jsr_placer      = None
        self._jsr_bg_rect     = None
        self._jsr_drag_sub    = None
        self._jsr_center_win  = None
        self._jsr_max_r       = 43.0
        self._jsr_thumb_r     = 18.0
        self._jsr_cx          = 65.0
        self._jsr_coord_lbl   = None            # live Pitch/Roll readout under the Rotate pad
        # Ghost cone state
        self._ghost_cone_path  = None           # USD prim path of the drag-preview cone
        self._ghost_light_path = None           # USD prim path of the drag-preview light
        self._ghost_last_pos   = None           # last (wx, wy, wz) computed during drag
        self._drag_aabbs       = []             # [(xmin,ymin,zmin,xmax,ymax,zmax), …]
        self._drag_floor_z     = 0.0
        self._drag_scene_cx    = 0.0
        self._drag_scene_cy    = 0.0
        self._drag_scene_size  = 10.0
        self._drag_ghost_h     = 0.3            # ghost cone height in scene units
        self._task = asyncio.ensure_future(self._deferred_startup())

    def on_shutdown(self):
        print("[FPT] on_shutdown")
        self._destroy_ghost_cone()
        # NOT calling _restore_viewport_manipulators() here: on_shutdown fires
        # on every hot-reload during development, and restoring here just to
        # immediately re-disable in the new instance's _build_ui() was
        # toggling 5 UI-owning extensions off-on-off on every code edit,
        # which was destabilizing Kit's window docking layout. The lock is
        # now session-scoped (see _VP_LOCK_SETTING) — it only applies once,
        # and getting mouse/keyboard back requires restarting Kit or calling
        # _restore_viewport_manipulators() manually.
        try:
            self._drop_sub    = None   # Strategy A subscription
            self._mouse_sub   = None   # Strategy C subscription
            self._update_sub  = None   # cursor-poll update subscription
            self._js_drag_sub  = None   # joystick drag subscription
            self._jsv_drag_sub = None   # vertical joystick drag subscription
            self._jsr_drag_sub = None   # rotation joystick drag subscription
            if hasattr(self, "_ci"): self._ci = None
        except Exception as e:
            print(f"[FPT] drop-sub cleanup err: {e}")
        try:
            for w in ui.Workspace.get_windows():
                # Exact match only — "Floor Plan" as a substring also matches
                # planlift.floorview's separate "Floor Plan View" window,
                # destroying it on every reload of this extension.
                if w.title == "Floor Plan Tool":
                    w.destroy()
        except Exception as e:
            print(f"[FPT] shutdown err: {e}")
        self._win = None
        self._ds  = {}
        global _INSTANCE
        if _INSTANCE is self:
            _INSTANCE = None

    async def _deferred_startup(self):
        """Wait for planlift.ui_setup to open the stage before building UI."""
        import omni.usd
        app = omni.kit.app.get_app()
        for i in range(600):
            await app.next_update_async()
            ctx = omni.usd.get_context()
            s = ctx.get_stage()
            if s and s.GetPseudoRoot().GetChildren():
                print(f"[FPT] Stage ready after {i} frames")
                break
        else:
            print("[FPT] WARNING: stage wait timed out")
        try:
            self._build_ui()
        except Exception as e:
            import traceback
            print(f"[FPT] ERROR: {e}")
            traceback.print_exc()

    def _build_ui(self):
        # ── The user's original script, only stage.Traverse() replaced ──
        import omni.usd
        import omni.kit.commands
        import omni.ui as ui
        from pxr import Usd, UsdGeom, UsdLux, UsdShade, Gf, Sdf, Vt
        import random
        import math
        import asyncio
        import time

        try:
            import omni.timeline
            HAS_TIMELINE = True
        except:
            HAS_TIMELINE = False

        try:
            import omni.kit.viewport.utility as vp_util
            HAS_VP = True
        except:
            HAS_VP = False

        try:
            import numpy as np
            HAS_NUMPY = True
        except:
            HAS_NUMPY = False

        ctx   = omni.usd.get_context()
        stage = ctx.get_stage()
        if not stage:
            print("[FPT] no stage")
            return

        # STAGE_ROOT must resolve to the SAME top-level prim every time this
        # extension (re)starts, or every path derived from it (RIG_PATH,
        # CAMS_PATH, ...) silently drifts. children[0] alone isn't reliable
        # once other top-level prims exist alongside it (e.g.
        # /PlanLift_Furniture, /PlanLift_FloorPlanImage) — USD doesn't
        # guarantee "the root we created FloorPlanRig under" stays first in
        # GetChildren() forever. Confirmed live: after a hot-reload with a
        # scene already open, children[0] resolved to a different top-level
        # prim than before, CAMS_PATH quietly became wrong, and
        # _cam_optical_path() stopped recognizing existing camera rig paths
        # as cameras at all (no "/Camera" child appended) — so switch_to_cam
        # pointed the viewport at a plain Xform instead of the real Camera
        # prim, which it silently refuses to switch to. Prefer whichever
        # top-level prim ALREADY has a FloorPlanRig child (the root
        # established before this reload) and only fall back to the old
        # first-child guess on a genuinely brand-new stage.
        children   = stage.GetPseudoRoot().GetChildren()
        _existing_root = next(
            (str(c.GetPath()) for c in children if stage.GetPrimAtPath(f"{c.GetPath()}/FloorPlanRig")),
            None)
        STAGE_ROOT = _existing_root or (str(children[0].GetPath()) if children else "/World")
        RIG_PATH   = f"{STAGE_ROOT}/FloorPlanRig"
        LIGHTS_PATH= f"{RIG_PATH}/Lights"
        CAMS_PATH  = f"{RIG_PATH}/Cameras"
        CAM_ZONE_PATH = f"{RIG_PATH}/CamZones"
        IND_LIGHTS_PATH = f"{RIG_PATH}/IndLights"   # individually-dropped lights (separate from the bulk grid)
        NUM_LIGHTS = 20

        CAM_PRESETS = {
            "2MP  (1920x1080)": dict(label="2MP",res_w=1920,res_h=1080,
                                     sensor='1/2.8"',focal_mm=2.8,h_ap=5.37,v_ap=3.02),
            "4MP  (2560x1440)": dict(label="4MP",res_w=2560,res_h=1440,
                                     sensor='1/2.7"',focal_mm=2.8,h_ap=5.60,v_ap=3.15),
            "8MP  (3840x2160)": dict(label="8MP",res_w=3840,res_h=2160,
                                     sensor='1/1.8"',focal_mm=2.8,h_ap=7.20,v_ap=5.40),
        }
        PRESET_KEYS = list(CAM_PRESETS.keys())

        _cam_list      = []
        _cam_count     = [0]
        btn_perspective = [None]   # set once the button widget is created
        lbl_total_area = [None]   # set once the Total Floor Area label widget is created
        _active_cam    = [None]
        _cam_speed     = [0.5]
        _cam_scan_done = [False]   # stage scan runs once on first rebuild only
        _cam_filter    = [""]     # lowercased name-substring filter for the compact camera list

        # Rig path -> floor idx (matches Floor Plan View's floor registry,
        # see planlift_floorview.extension._floors_registry). Populated at
        # every camera-creation site (add_camera, _spawn_camera_at_pixel,
        # and cross-extension via _notify_fpt_camera_added) and consumed by
        # _rebuild_cam_list (grouped list) and _generate_zone (floor-scoped
        # level selection). A camera with no entry here is legacy/unknown —
        # both consumers fall back to today's whole-scene behavior for it.
        _cam_floor     = {}          # path -> floor idx

        # ── Camera Zones (occlusion-aware floor coverage) state ─────────────
        _cam_colors    = {}          # path -> Gf.Vec3f color
        _cam_zones     = {}          # path -> True if zone mesh exists
        _zone_visible  = {}          # path -> bool
        _cam_zone_area = {}          # path -> zone floor area in m^2 (stage units assumed = metres, as everywhere else in this file)
        _scene_tris    = []          # cached world-space triangles for the occlusion-aware zone scan
                                      # (pure-Python fallback path only — see _cache_scene_triangles)
        _scene_tri_np  = (None, None, None)  # (v0,v1,v2) each (N,3) float64 arrays — numpy fast path
        _scene_tri_bp  = None         # (centers, radius, radius_sq) numpy broad-phase arrays, or None
        _rc_cache      = {"origin": None, "oc": None, "dist_sq": None}  # per-origin raycast cache —
                                      # see _raycast_nearest; invalidated whenever triangles are rebuilt
        _sct_cache     = {"stage": None, "t": 0.0, "mesh_count": -1}   # _cache_scene_triangles reuse signature
        _dfl_cache     = {"stage": None, "t": 0.0, "cands": None}      # _detect_floor_level_candidates memo
        _zone_quality  = ["medium"]  # "low"/"medium"/"high" -> ray grid density (see _zone_ray_grid)
        _cam_range     = [10.0]      # zone raycast range, driven by the Range slider
        _wall_collision = [False]   # Wall Collision toggle — see _clamp_move_by_walls

        _ind_light_list   = []      # individually-dropped lights (separate from the bulk grid)
        _ind_light_count  = [0]
        _active_ind_light = [None]
        _updating_ind_light_height = [False]
        _updating_ind_light_props  = [False]   # guards the per-light Intensity/Temp/W/D fields
        field_ind_light_intensity = None
        field_ind_light_temp      = None
        field_ind_light_width     = None
        field_ind_light_depth     = None

        def set_attr(prim, name, value, sdf_type=None):
            for n in [f"inputs:{name}", name]:
                a = prim.GetAttribute(n)
                if a and a.IsValid():
                    a.Set(value); return
            if sdf_type:
                prim.CreateAttribute(f"inputs:{name}", sdf_type).Set(value)

        def _rs():
            """Refresh the stage reference (stage may have been reopened)."""
            nonlocal stage
            stage = ctx.get_stage()
            return stage

        def _cam_optical_path(path):
            """Path of the actual UsdGeom.Camera prim for a camera identity
            path. Placed cameras are a small rig+camera pair: an outer Xform
            "rig" (translate + a yaw-only rotateZ) with the real Camera prim
            as its fixed, (0,0,0)-translate child holding tilt/roll exactly
            as before. Splitting them this way means panning (which only
            ever touches the rig's yaw) can never bleed into tilt/roll,
            which a single combined Euler triple could not guarantee once
            the camera had any existing tilt (see _on_rotate_drag_update in
            planlift_floorview for the full story). The built-in Perspective
            camera has no rig — it's used directly."""
            if not path:
                return path
            # Checks the stage directly for a "/Camera" child FIRST, rather
            # than only a CAMS_PATH-prefix string match: CAMS_PATH is
            # captured once, early, from this extension's own root
            # detection, but planlift_floorview's _spawn_camera (the
            # AI-agent/drag-drop path) recomputes its own root
            # independently at camera-creation time -- if the stage's set
            # of top-level prims shifted between those two moments, the
            # two can land on the same REAL root by logic but disagree as
            # STRINGS, and the old prefix check would silently miss it,
            # leaving switch_to_cam targeting a bare rig Xform the
            # viewport then just as silently refuses to switch to. A
            # direct stage check works regardless of which root either
            # side landed on.
            try:
                if stage:
                    child = stage.GetPrimAtPath(path + "/Camera")
                    if child and child.IsValid() and child.GetTypeName() == "Camera":
                        return path + "/Camera"
            except Exception:
                pass
            if path.startswith(CAMS_PATH + "/"):
                return path + "/Camera"
            return path

        def ensure(path):
            _rs()
            if not stage.GetPrimAtPath(path):
                stage.DefinePrim(path, "Xform")

        # Walking every prim and world-bounding it (below) costs ~1 s on a
        # full multi-floor building. The building geometry is static in this
        # viewer, so memoise: recompute only when the stage object changes or
        # the cached value is older than 20 s. Without this, the first frame
        # of a joystick drag (and the resync on release) stalled ~1 s.
        _gfi_cache = {"stage": None, "t": 0.0, "val": None}

        def _get_floor_info_uncached():
            _rs()
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

        def get_floor_info(force=False):
            import time as _t
            _rs()
            now = _t.time()
            if (not force and _gfi_cache["val"] is not None
                    and _gfi_cache["stage"] is stage
                    and (now - _gfi_cache["t"]) < 20.0):
                return _gfi_cache["val"]
            val = _get_floor_info_uncached()
            _gfi_cache.update(stage=stage, t=now, val=val)
            return val

        def _mpu():
            """Real metres per raw stage unit — NOT from
            UsdGeom.GetStageMetersPerUnit(). Confirmed via live testing
            that this pipeline's GLB->USD import leaves that metadata at
            1.0 (claiming stage units = metres) even when the actual
            geometry is centimetre-scaled — e.g. a scene logged as both
            "14860.6x8790.3m" (FloorPlanImage) and "14860.6x8790.3cm"
            (LIGHTS) for the SAME numbers, with the "10ft=304.800cm" light
            calculation confirming cm is the one that's actually self-
            consistent. get_floor_info()'s own extent>100 cm/m heuristic
            already gets this right (it's what the rest of this file's
            real-world-distance features — light sizing, camera height —
            already rely on), so derive the conversion from its `ten_feet`
            return value instead of trusting stage metadata: ten_feet is
            "how many raw units = 10 real feet" (304.8 for cm, 3.048 for
            m), and 10 feet is always 3.048 metres regardless of which."""
            _, _, _, _, _, _, ten_feet = get_floor_info()
            return 3.048 / ten_feet if ten_feet else 1.0

        def _get_wall_height_ft():
            """Building WALL height specifically (not "tallest thing in the
            scene" — tried that first, confirmed via live testing it picks
            up a gable roof's ridge, which sits well above the walls) in
            real feet — used as the vertical joystick's max, so it matches
            whatever wall height was set in the webapp at export time.

            Every roof shape this pipeline builds (flat/shed/gable/hip/
            gambrel/butterfly/sawtooth) starts AT wall height and builds
            upward/outward from there — including a gable's own vertical
            triangular gable-end infill, whose base sits at wall height,
            not at the floor. So instead of one scene-wide bounding box,
            this scans each prim's OWN bounding box individually and keeps
            only the ones that actually touch the floor (within
            floor_tol) — a roof panel or gable-end triangle never does,
            regardless of shape. Among floor-touching prims, the floor
            slab itself is excluded using the same "thin" test
            _detect_floor_z() uses to identify it (large-area is skipped
            here — confirmed via live testing that this pipeline's actual
            scale makes get_floor_info()/that check's original area
            threshold pass for virtually everything, not a useful
            discriminator here; z_thick vs. floor_tol, both already in
            the same raw-unit scale, does the real work). The tallest of
            what's left is the wall top.

            Caveat: floor-standing furniture taller than the walls would
            also be picked up and read as "wall height" — the same
            fundamental ambiguity _detect_floor_z() already has on the
            floor side. Not handled specially — a real but much rarer
            case than the roof one this was built to fix."""
            _rs()
            _cx, _cy, fz, _w, _d, _unit, tf = get_floor_info()
            floor_tol = max(2.0, tf * 0.05)
            bc = UsdGeom.BBoxCache(Usd.TimeCode.Default(),
                [UsdGeom.Tokens.default_, UsdGeom.Tokens.render], useExtentsHint=True)
            mpu = _mpu()
            best_top_ft, best_path = None, None
            for prim in _walk_prims(stage):
                if "FloorPlanRig" in str(prim.GetPath()): continue
                if not prim.IsA(UsdGeom.Boundable): continue
                try:
                    r = bc.ComputeWorldBound(prim).ComputeAlignedRange()
                    mn_p, mx_p = r.GetMin(), r.GetMax()
                except Exception:
                    continue
                if abs(float(mn_p[2]) - fz) > floor_tol:
                    continue  # doesn't touch the floor -> roof/ceiling-mounted, not a wall
                z_thick = mx_p[2] - mn_p[2]
                if z_thick < floor_tol:
                    continue  # thin slab sitting at floor level -> this IS the floor, not a wall
                top_ft = (float(mx_p[2]) * mpu) / 0.3048
                if best_top_ft is None or top_ft > best_top_ft:
                    best_top_ft, best_path = top_ft, str(prim.GetPath())
            if best_top_ft is None:
                print("[JSV] wall-height scan: no floor-touching non-floor geometry found — "
                      "falling back to 10ft default")
                return 10.0
            print(f"[JSV] wall-height (floor-touching, non-floor) scan: tallest={best_path} "
                  f"top={best_top_ft:.2f}ft fz={fz:.3f}raw floor_tol={floor_tol:.3f}raw")
            return best_top_ft if best_top_ft > 0.1 else 10.0

        def lpath(i): return f"{LIGHTS_PATH}/RectLight_{i+1:02d}"
        def any_lights(): return any(stage.GetPrimAtPath(lpath(i)) for i in range(NUM_LIGHTS))

        def set_status(msg):
            try: lbl_status.text = msg
            except: pass
            print(f"[UI] {msg.replace(chr(10),' | ')}")

        def _localize_all_textures(st, out_dir):
            """Copy EVERY external texture asset referenced anywhere in the
            stage — wall/pillar/mezzanine/furniture materials from the
            GLB->USD conversion, the floor-plan background image, anything
            else — into a textures/ folder next to the saved .usd, and
            repoint each shader input at its own local copy.

            Only the zone meshes' own materials (plain UsdPreviewSurface
            colour values, no texture files at all) ever survived a plain
            save-as to a different folder — everything else was authored
            with texture paths relative to the ORIGINAL converted-USD's own
            folder (output/isaac_tmp), which breaks the moment the saved
            file lives anywhere else. Confirmed live: a save that only
            localized the floor-plan image (the original narrower version
            of this function) still came out with every building material
            blank/grey — this walks every Shader prim in the stage instead
            of just that one hardcoded path, so nothing is special-cased.

            Best-effort per-texture: a single missing/unreadable file is
            printed and skipped rather than aborting the whole save."""
            try:
                import shutil
                from pathlib import Path
                from pxr import UsdShade, Sdf
                tex_dir = out_dir / "textures"
                copied  = {}   # resolved source path (str) -> destination Path
                n_ok = n_fail = 0
                for prim in _walk_prims(st):
                    if not prim.IsA(UsdShade.Shader):
                        continue
                    shader = UsdShade.Shader(prim)
                    for tex_in in shader.GetInputs():
                        try:
                            if tex_in.GetTypeName() != Sdf.ValueTypeNames.Asset:
                                continue
                            asset_val = tex_in.Get()
                            if not asset_val:
                                continue
                            raw = asset_val.resolvedPath or asset_val.path
                            if not raw:
                                continue
                            local = raw[len("file:///"):] if raw.startswith("file:///") else raw
                            src = Path(local)
                            key = str(src)
                            if key in copied:
                                dst = copied[key]
                            else:
                                if not src.is_file():
                                    n_fail += 1
                                    print(f"[FPT] save_scene: texture not found, leaving as-is: {src} "
                                          f"({prim.GetPath()}.{tex_in.GetBaseName()})")
                                    continue
                                tex_dir.mkdir(parents=True, exist_ok=True)
                                dst = tex_dir / src.name
                                if not dst.exists():
                                    shutil.copy2(src, dst)
                                copied[key] = dst
                            tex_in.Set(Sdf.AssetPath("file:///" + str(dst).replace("\\", "/")))
                            n_ok += 1
                        except Exception as _e:
                            print(f"[FPT] save_scene: texture input skipped ({prim.GetPath()}): {_e}")
                print(f"[FPT] save_scene: localized {n_ok} texture reference(s) "
                      f"({len(copied)} unique file(s) copied), {n_fail} not found")
            except Exception as e:
                print(f"[FPT] save_scene: texture localize error: {e}")

        def _save_scene():
            """Save the LIVE stage (placed cameras, generated zones, everything
            currently in the scene) to a permanent .usd file — this app has no
            File menu (the viewer .kit config omits omni.kit.window.file), and
            the stage's own current path is a temp file under output/isaac_tmp
            that gets auto-deleted ~1hr after launch, so Save As to a new
            permanent path here instead of relying on either of those."""
            try:
                import omni.usd, datetime
                from pathlib import Path
                ctx = omni.usd.get_context()
                st  = ctx.get_stage()
                if not st:
                    set_status("Save failed: no stage")
                    return
                out_dir = Path("D:/PlanLift-Backend/output/isaac_saves")
                out_dir.mkdir(parents=True, exist_ok=True)
                ts       = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
                out_path = out_dir / f"planlift_scene_{ts}.usd"
                _localize_all_textures(st, out_dir)
                # Flatten every composition arc into ONE self-contained layer
                # instead of a bare save_as_stage. /PlanLift_Furniture brings
                # in the actual building/mezzanine/furniture geometry via a
                # USD *reference* to the converted file in output/isaac_tmp —
                # save_as_stage only writes the thin root layer (cameras,
                # zones, and that reference arc itself), leaving the real
                # 16k+ triangles of geometry pointing at a temp file the
                # 1-hour cleanup deletes. Confirmed live: a save_as_stage-only
                # save came out at 47KB, far too small to hold the real scene
                # — that was this exact bug. Flatten() resolves every
                # reference/payload into real prims in one layer so the
                # saved file has no outside dependency on isaac_tmp at all.
                flat_layer = st.Flatten()
                ok = flat_layer.Export(str(out_path))
                if ok:
                    set_status(f"Saved: {out_path.name}")
                    print(f"[FPT] Saved scene to {out_path}")
                else:
                    set_status("Save failed — see console")
                    print("[FPT] flat_layer.Export returned False")
            except Exception as e:
                print(f"[FPT] save_scene error: {e}")
                set_status(f"Save failed: {e}")

        def _open_scene():
            """Load the most recently saved scene (output/isaac_saves/*.usd)
            into the CURRENT stage, replacing whatever's in it now — this
            app has no File menu, so this is the only way to reopen a
            previously-saved scene (e.g. in a freshly-launched instance of
            this .kit app). Floor Plan Tool's existing "reconnect to
            existing cameras" scan (_cam_scan_done, see _rebuild_cam_list)
            picks the reopened cameras/zones back up on its own once the
            new stage finishes loading — no extra wiring needed for that.
            Picks the newest file in the folder rather than prompting for
            one, since there's no file-picker extension loaded either;
            rename/move older saves out of that folder if the wrong one
            would otherwise be picked up."""
            try:
                import omni.usd
                from pathlib import Path
                save_dir = Path("D:/PlanLift-Backend/output/isaac_saves")
                candidates = sorted(save_dir.glob("*.usd"), key=lambda p: p.stat().st_mtime, reverse=True)
                if not candidates:
                    set_status("Open failed: no saved scenes found")
                    return
                latest = candidates[0]
                ctx = omni.usd.get_context()
                ok = ctx.open_stage(str(latest))
                if ok:
                    set_status(f"Opening: {latest.name}")
                    print(f"[FPT] Opening saved scene {latest}")
                else:
                    set_status("Open failed — see console")
                    print("[FPT] open_stage returned False")
            except Exception as e:
                print(f"[FPT] open_scene error: {e}")
                set_status(f"Open failed: {e}")


        def switch_to_cam(path):
            _active_cam[0] = path
            try:
                if HAS_VP:
                    vp = vp_util.get_active_viewport()
                    target = _cam_optical_path(path)
                    print(f"[VIEW DEBUG] switch_to_cam: vp={vp!r} target={target} "
                          f"before={getattr(vp,'camera_path',None) if vp else None}")
                    if vp:
                        vp.camera_path = target
                        print(f"[VIEW DEBUG] switch_to_cam: after={getattr(vp,'camera_path',None)}")
                    else:
                        print("[VIEW DEBUG] switch_to_cam: get_active_viewport() returned None/falsy")
            except Exception as e:
                print(f"[VIEW] {e}")
            # Resync the height slider to whichever camera just became active,
            # so it doesn't keep showing the previous camera's height.
            try:
                self._jsv_sync_fn(self._jsv_read_height_fn())
            except Exception as e:
                print(f"[JSV] resync on switch: {e}")
            _schedule_rebuild()
            set_status(f"Active: {path.split('/')[-1]}")
            try:
                from planlift_floorview.extension import _INSTANCE as _fpv
                if _fpv is not None:
                    _fpv._update_height_field_from_cam(path)
            except Exception as _e:
                print(f"[FPT] fpv height sync: {_e}")


        _rflag = [False]
        def _schedule_rebuild():
            _rflag[0] = True
            try:
                import omni.kit.app, asyncio
                async def _do():
                    await omni.kit.app.get_app().next_update_async()
                    if _rflag[0]:
                        _rflag[0] = False
                        _rebuild_cam_list()
                asyncio.ensure_future(_do())
            except:
                _rebuild_cam_list()

        # _refresh_jsv_max() (the joystick's height ceiling) only ever runs
        # inside _rebuild_cam_list(), which otherwise only fires on user
        # actions like adding a camera — so a user who switches to
        # Perspective before ever adding a camera gets stuck at the 10ft
        # startup default even after the model finishes loading, since
        # nothing else ever re-triggers a rebuild. Poll for the model
        # actually being present (same /PlanLift_Furniture signal
        # planlift.ui_setup polls for) and force one rebuild as soon as it
        # appears, so the real wall height is picked up without needing an
        # incidental camera-add first.
        async def _poll_model_then_refresh():
            app = omni.kit.app.get_app()
            for _ in range(3600):
                await app.next_update_async()
                try:
                    s = _rs()
                    if s and s.GetPrimAtPath("/PlanLift_Furniture").IsValid():
                        break
                except Exception:
                    pass
            else:
                print("[JSV] model poll timed out — wall-height max may still be the 10ft default")
                return
            # A few extra ticks so the reference fully resolves before scanning.
            for _ in range(30):
                await app.next_update_async()
            _schedule_rebuild()
        asyncio.ensure_future(_poll_model_then_refresh())

        def _perspective_btn_style(active):
            return {"background_color":0xFF1A3A1A if active else 0xFF222222,
                    "color":0xFF88FF88 if active else 0xFFAAAAAA,
                    "font_size":10,"border_radius":4}

        def _refresh_perspective_btn():
            if btn_perspective[0] is None:
                return
            in_persp = _active_cam[0] in (None, "/OmniverseKit_Persp")
            btn_perspective[0].style = _perspective_btn_style(in_persp)

        def _refresh_total_area():
            """Whole-model footprint, for sanity-checking against the area
            entered in the PlanLift webapp. This is the scene's bounding
            box (get_floor_info's sw*sd), not a flood-filled outline like
            the webapp's footprint measurement — close for a roughly
            rectangular building, but will read larger than the webapp's
            figure for an L-shaped/irregular one, since a bounding box
            always includes the empty corners outside the actual walls."""
            if lbl_total_area[0] is None:
                return
            _rs()
            _, _, _, sw, sd, _unit, _ = get_floor_info()
            mpu = _mpu()
            area_m2 = sw * sd * (mpu ** 2)
            area_ft2 = area_m2 * 10.7639
            lbl_total_area[0].text = (
                f"Total Floor Area: {area_m2:,.1f} m²  /  {area_ft2:,.0f} ft²  (bounding box)"
            )
            print(f"[TOTAL AREA] sw={sw:.3f} sd={sd:.3f} stage_units, metersPerUnit={mpu}, "
                  f"unit_guess={_unit} -> {area_m2:.1f} m^2 ({area_ft2:.1f} ft^2)")

        def _refresh_jsv_max():
            """Keep the vertical (height) joystick's max in sync with the
            actual building height (see _get_wall_height_ft), instead of
            the old fixed 10ft default — so the joystick's ceiling always
            matches whatever wall height was set in the webapp at export
            time. Recomputed on every rebuild (cheap — one bounding-box
            scan, same cost as get_floor_info(), already called this often)
            so it stays correct if a different model gets loaded without
            restarting the extension. Re-syncs the slider display
            afterward with the same idiom used elsewhere (e.g. on camera
            switch) so a value now above a newly-detected ceiling gets
            re-clamped immediately instead of only on the next drag.

            Prefers the ACTIVE camera's own floor's wallHeightM from the
            floor registry (per-floor, set in the webapp) over the whole-
            stage geometry scan below — once floors can have different
            heights, that scan only ever measures whichever floor sits at
            the stage's lowest Z (see _get_wall_height_ft's own docstring),
            which is the wrong ceiling for every other floor's joystick.
            Falls back to the geometry scan for legacy scenes exported
            before per-floor heights existed (no wallHeightM in the
            registry). Same _cam_floor / unstamped-camera-infers-from-its-
            own-Z idiom as _jsv_read_active_height, so this and that stay
            in agreement about which floor "the active camera" means."""
            new_max = None
            try:
                cam_path = _active_cam[0] or "/OmniverseKit_Persp"
                from planlift_floorview.extension import _INSTANCE as _fpv_max
                if _fpv_max is not None:
                    _fidx = _cam_floor.get(cam_path)
                    if _fidx is None:
                        s = _rs()
                        prim = s.GetPrimAtPath(cam_path) if s else None
                        if prim and prim.IsValid():
                            vecs = UsdGeom.XformCommonAPI(prim).GetXformVectors(Usd.TimeCode.Default())
                            _fidx = _fpv_max._floor_idx_for_z(vecs[0][2])
                    if _fidx is not None:
                        _entry = _fpv_max._floor_registry_entry(_fidx)
                        _wh_m = _entry.get("wallHeightM") if _entry else None
                        if _wh_m is not None:
                            new_max = float(_wh_m) / 0.3048
                            print(f"[JSV] max height: using floor {_fidx}'s own registry "
                                  f"wallHeightM={_wh_m:.2f}m -> {new_max:.2f}ft")
            except Exception as e:
                print(f"[JSV] max height: registry lookup failed ({e}), falling back to geometry scan")
            if new_max is None:
                new_max = _get_wall_height_ft()
            if abs(new_max - self._jsv_max_ft) > 0.01:
                print(f"[JSV] max height: {self._jsv_max_ft:.2f}ft -> {new_max:.2f}ft "
                      f"(building height detected from scene)")
                self._jsv_max_ft = new_max
            self._jsv_sync_fn(self._jsv_read_height_fn())

        def _rebuild_cam_list():
            _refresh_perspective_btn()
            _refresh_total_area()
            _refresh_jsv_max()
            # One-time stage scan: discovers cameras that existed before the Tool started.
            # After the first rebuild, _cam_list is the sole source of truth — running
            # the scan every rebuild caused deleted cameras to reappear because the prim
            # is still alive when async DeletePrims hasn't fired yet.
            if not _cam_scan_done[0]:
                _cam_scan_done[0] = True
                try:
                    _rs()
                    if stage:
                        for prim in _walk_prims(stage):
                            if prim.GetTypeName() != "Camera":
                                continue
                            p_str = str(prim.GetPath())
                            if "/OmniverseKit_" in p_str:
                                continue
                            if "/_GhostCamera" in p_str:
                                continue
                            # prim here is the CHILD "Camera" prim (e.g.
                            # .../Cam_00_2MP/Camera) — _cam_list stores the
                            # RIG path (its parent, e.g. .../Cam_00_2MP;
                            # see add_camera()'s _cam_list.append(path)).
                            # Comparing/appending the child path directly
                            # meant this scan never recognized a
                            # just-created camera as already present, and
                            # duplicated it — only on the first rebuild
                            # after startup, since _cam_scan_done makes
                            # this a one-shot.
                            rig_str = str(prim.GetPath().GetParentPath())
                            if rig_str not in _cam_list:
                                _cam_list.append(rig_str)
                            # _cam_colors is pure in-memory state — a reload
                            # empties it, and this reconnect scan restoring
                            # _cam_list alone left every rediscovered camera
                            # falling back to _cam_colors.get(path, WHITE),
                            # which is exactly why every zone came out the
                            # same (white-ish) colour after a reload+
                            # regenerate instead of each camera's own colour.
                            # Both naming schemes (add_camera's "Cam_00_4MP"
                            # and the drag-drop path's "Cam_Drop_00_4MP")
                            # encode the original creation index as the first
                            # purely-numeric "_"-separated segment, so re-
                            # deriving it from the name reproduces the exact
                            # same CAM_COLORS entry it originally got.
                            if rig_str not in _cam_colors:
                                _idx = 0
                                for _part in rig_str.split("/")[-1].split("_"):
                                    if _part.isdigit():
                                        _idx = int(_part)
                                        break
                                _cam_colors[rig_str] = CAM_COLORS[_idx % len(CAM_COLORS)]
                                _cam_count[0] = max(_cam_count[0], _idx + 1)
                except Exception as _e:
                    print(f"[FPT] cam scan: {_e}")
            cam_list_frame.clear()
            with cam_list_frame:
                if not _cam_list:
                    ui.Label("No cameras added yet.",
                             style={"font_size":10,"color":0xFF555555})
                else:
                    _filt = _cam_filter[0]
                    shown = [p for p in _cam_list
                             if not _filt or _filt in p.split("/")[-1].lower()]
                    if not shown:
                        ui.Label(f'No cameras match "{_cam_filter[0]}".',
                                 style={"font_size":10,"color":0xFF555555})
                    # Group into per-floor sections (see _cam_floor, stamped
                    # at every camera-creation site) using Floor Plan View's
                    # own floor registry for ordering/labels, so this list
                    # matches its floor tabs exactly. A camera with no floor
                    # stamp yet (legacy) falls into a trailing "Unassigned"
                    # section instead of silently disappearing. On a
                    # single-floor scene (empty registry) this collapses
                    # back to one unlabeled section — today's behavior,
                    # unchanged.
                    _floor_reg = self._fpv_floor_registry()
                    _groups = []  # [(label_or_None, [paths])]
                    if _floor_reg:
                        for _fl in _floor_reg:
                            _fi = _fl.get("idx", 0)
                            _cams_here = [p for p in shown if _cam_floor.get(p) == _fi]
                            if _cams_here:
                                _groups.append((str(_fl.get("label") or f"Floor {_fi + 1}"), _cams_here))
                        _unassigned = [p for p in shown if _cam_floor.get(p) is None]
                        if _unassigned:
                            _groups.append(("Unassigned", _unassigned))
                    elif shown:
                        _groups = [(None, shown)]
                    # Squarish chip grid (5/row) instead of one full-width row
                    # per camera — with 70-80 cameras a single column, even a
                    # compact one, was still a very long scroll to reach Zone
                    # Controls/joysticks below. Each chip: Z (generate zone,
                    # tinted by this camera's own zone colour) + X (delete) in
                    # the corners, index number in the middle, a bottom
                    # stripe as an at-a-glance zone-state indicator. Hide/Show
                    # and the area readout live only in cam_detail_frame for
                    # whichever camera is currently active.
                    _CHIP_W, _CHIP_H, _CHIP_COLS = 62, 52, 5
                    with ui.VStack(spacing=6):
                        for _section_label, _section_cams in _groups:
                            if _section_label is not None:
                                ui.Label(f"{_section_label}  ({len(_section_cams)})",
                                         height=16,
                                         style={"font_size": 11, "color": 0xFF7690AC})
                            for _row_start in range(0, len(_section_cams), _CHIP_COLS):
                                _row = _section_cams[_row_start:_row_start+_CHIP_COLS]
                                with ui.HStack(height=_CHIP_H, spacing=4):
                                    for p in _row:
                                        name   = p.split("/")[-1]
                                        active = (p == _active_cam[0])
                                        color    = _cam_colors.get(p, Gf.Vec3f(1,1,1))
                                        cr = int(color[0]*255); cg = int(color[1]*255); cb = int(color[2]*255)
                                        ui_color = (0xFF000000 | (cb << 16) | (cg << 8) | cr)
                                        has_zone = _cam_zones.get(p, False)
                                        zone_vis = _zone_visible.get(p, True)
                                        # Green (this camera's own zone colour) when visible,
                                        # orange when hidden, dim grey when no zone yet —
                                        # matches the Hide/Show colour convention already
                                        # used in cam_detail_frame below.
                                        state_color = (ui_color if (has_zone and zone_vis)
                                                       else 0xFFFFAA44 if has_zone
                                                       else 0xFF555555)
                                        _parts = name.split("_")
                                        chip_label = _parts[1] if len(_parts) > 1 else name[:4]
                                        with ui.ZStack(width=_CHIP_W, height=_CHIP_H):
                                            ui.Rectangle(style={
                                                "background_color": 0xFF173A1B if active else 0xFF191C21,
                                                "border_color": 0xFF3A8A46 if active else 0xFF24272E,
                                                "border_width": 1, "border_radius": 6,
                                            })
                                            with ui.VStack():
                                                with ui.HStack(height=16, spacing=2, style={"margin": 2}):
                                                    ui.Button(
                                                        "Z", height=16,
                                                        tooltip=("Zone generated — click to regenerate" if has_zone else "Generate zone"),
                                                        style={"background_color": 0xFF101216, "color": state_color,
                                                               "font_size": 9, "border_radius": 3},
                                                    ).set_clicked_fn(lambda _p=p: _generate_zone(_p))
                                                    ui.Button(
                                                        "X", width=16, height=16,
                                                        tooltip="Delete camera",
                                                        style={"background_color": 0xFF2A1417, "color": 0xFFFF8A8A,
                                                               "font_size": 9, "border_radius": 3},
                                                    ).set_clicked_fn(lambda _p=p: _remove_cam(_p))
                                                ui.Button(
                                                    chip_label, height=_CHIP_H-16-3,
                                                    tooltip=name,
                                                    style={"background_color": 0x00000000,
                                                           "color": 0xFF8FF0A8 if active else 0xFF9BA2AC,
                                                           "font_size": 12, "border_radius": 0},
                                                ).set_clicked_fn(lambda _p=p: switch_to_cam(_p))
                                                ui.Rectangle(height=3, style={
                                                    "background_color": state_color if has_zone else 0xFF2A2E35,
                                                })
            _refresh_cam_detail()

        def _on_cam_filter_changed(m):
            _cam_filter[0] = m.get_value_as_string().strip().lower()
            _rebuild_cam_list()

        def _refresh_cam_detail():
            """Full zone details + full-text action buttons for whichever
            camera is currently active — keeps the compact list rows tiny
            while still surfacing area/regenerate/hide without a click-through."""
            if cam_detail_frame is None:
                return
            cam_detail_frame.clear()
            p = _active_cam[0]
            with cam_detail_frame:
                if not p or p not in _cam_list:
                    ui.Label("Select a camera above to see its zone details.",
                             height=18, style={"font_size":9,"color":0xFF555555})
                    return
                name     = p.split("/")[-1]
                has_zone = _cam_zones.get(p, False)
                zone_vis = _zone_visible.get(p, True)
                with ui.VStack(spacing=3):
                    ui.Label(f"Selected: {name}", height=16,
                             style={"font_size":10,"color":0xFF88BBFF})
                    if has_zone:
                        area_m2  = _cam_zone_area.get(p, 0.0)
                        area_ft2 = area_m2 * 10.7639
                        ui.Label(f"Zone: generated   Area: {area_m2:.1f} m²  /  {area_ft2:.0f} ft²",
                                 height=16, style={"font_size":9,"color":0xFF888888})
                    else:
                        ui.Label("Zone: not generated", height=16,
                                 style={"font_size":9,"color":0xFF666666})
                    with ui.HStack(height=26, spacing=6):
                        ui.Button(
                            "Regenerate Zone" if has_zone else "Generate Zone",
                            height=26, clicked_fn=lambda _p=p: _generate_zone(_p),
                            style={"background_color":0xFF0E2A40,"color":0xFF88CCFF,
                                   "font_size":10,"border_radius":4})
                        if has_zone:
                            ui.Button(
                                "Hide Zone" if zone_vis else "Show Zone",
                                height=26, clicked_fn=lambda _p=p: _toggle_zone(_p),
                                style={"background_color": 0xFF2A1A08 if zone_vis else 0xFF083A08,
                                       "color": 0xFFFFAA44 if zone_vis else 0xFF44FF88,
                                       "font_size":10,"border_radius":4})

        # ── Individual lights (drag-to-place) list ─────────────────────────
        _irflag = [False]
        def _schedule_ind_light_list_rebuild():
            _irflag[0] = True
            try:
                import omni.kit.app, asyncio
                async def _do():
                    await omni.kit.app.get_app().next_update_async()
                    if _irflag[0]:
                        _irflag[0] = False
                        _rebuild_ind_light_list()
                asyncio.ensure_future(_do())
            except:
                _rebuild_ind_light_list()

        def _select_ind_light(path):
            _active_ind_light[0] = path
            _sync_ind_light_height_field()
            _schedule_ind_light_list_rebuild()
            set_status(f"Light selected: {path.split('/')[-1]}")

        def _sync_ind_light_height_field():
            """Reflect the SELECTED individual light's own attributes in the
            per-light fields — height (ft), intensity, colour temp, width,
            depth. Called on every selection change so the fields never show a
            stale value from a previous selection. Each individual light keeps
            its OWN values; the global "Light Properties" sliders only seed a
            light at creation and drive the Quick-Fill grid, never these."""
            p = _active_ind_light[0]
            prim = stage.GetPrimAtPath(p) if p else None
            if not prim or not prim.IsValid():
                return
            _updating_ind_light_height[0] = True
            _updating_ind_light_props[0]  = True
            try:
                if field_ind_light_height:
                    vecs = UsdGeom.XformCommonAPI(prim).GetXformVectors(Usd.TimeCode.Default())
                    t = list(vecs[0])
                    _,_2,fz,_3,_4,_5,tf = get_floor_info()
                    upf = tf / 10.0
                    height_ft = (t[2] - fz) / upf if upf else 0.0
                    field_ind_light_height.model.set_value(round(height_ft, 2))

                def _rd(attr, dflt):
                    a = prim.GetAttribute(attr)
                    v = a.Get() if a and a.HasAuthoredValue() else None
                    return float(v) if v is not None else float(dflt)

                if field_ind_light_intensity:
                    field_ind_light_intensity.model.set_value(_rd("intensity", 50000.0))
                if field_ind_light_temp:
                    field_ind_light_temp.model.set_value(_rd("colorTemperature", 4500.0))
                if field_ind_light_width:
                    field_ind_light_width.model.set_value(round(_rd("width", 1.0), 3))
                if field_ind_light_depth:
                    field_ind_light_depth.model.set_value(round(_rd("height", 1.0), 3))
            finally:
                _updating_ind_light_height[0] = False
                _updating_ind_light_props[0]  = False

        def _on_ind_light_height_changed(model):
            if _updating_ind_light_height[0]:
                return
            p = _active_ind_light[0]
            if not p or not stage.GetPrimAtPath(p):
                return
            prim = stage.GetPrimAtPath(p)
            vecs = UsdGeom.XformCommonAPI(prim).GetXformVectors(Usd.TimeCode.Default())
            t = list(vecs[0]); r = list(vecs[1])
            _,_2,fz,_3,_4,_5,tf = get_floor_info()
            upf = tf / 10.0
            t[2] = fz + model.get_value_as_float() * upf
            xf = UsdGeom.XformCommonAPI(prim)
            xf.SetTranslate(Gf.Vec3d(*t))
            xf.SetRotate(Gf.Vec3f(*r), UsdGeom.XformCommonAPI.RotationOrderXYZ)

        def _on_ind_light_prop_changed(attr_name, model):
            """Write one property to the SELECTED individual light only."""
            if _updating_ind_light_props[0]:
                return
            p = _active_ind_light[0]
            prim = stage.GetPrimAtPath(p) if p else None
            if not prim or not prim.IsValid():
                return
            try:
                val = float(model.get_value_as_float())
            except Exception:
                return
            set_attr(prim, attr_name, val, Sdf.ValueTypeNames.Float)
            if attr_name == "colorTemperature":
                set_attr(prim, "enableColorTemperature", True, Sdf.ValueTypeNames.Bool)

        def _remove_ind_light(path):
            name = path.split("/")[-1]
            if path in _ind_light_list:
                _ind_light_list.remove(path)
            if _active_ind_light[0] == path:
                _active_ind_light[0] = _ind_light_list[-1] if _ind_light_list else None
                _sync_ind_light_height_field()
            try:
                if stage.GetPrimAtPath(path):
                    omni.kit.commands.execute("DeletePrims", paths=[path])
            except Exception as e:
                print(f"[FPT] remove light: {e}")
            _schedule_ind_light_list_rebuild()
            # Floor Plan View only auto-refreshes on camera-state polling —
            # lights aren't polled, so removal needs an explicit nudge or the
            # deleted light's marker would linger until something else redraws.
            try:
                from planlift_floorview.extension import _INSTANCE as _fpv
                if _fpv is not None:
                    _fpv._schedule_rebuild()
            except Exception:
                pass
            set_status(f"Light removed: {name}")

        def _rebuild_ind_light_list():
            ind_light_list_frame.clear()
            with ind_light_list_frame:
                if not _ind_light_list:
                    ui.Label("No individual lights yet.",
                             style={"font_size":10,"color":0xFF555555})
                    return
                with ui.VStack(spacing=2):
                    for p in list(_ind_light_list):
                        name   = p.split("/")[-1]
                        active = (p == _active_ind_light[0])
                        with ui.HStack(height=26,spacing=2):
                            ui.Button(
                                (">> " if active else "   ") + name, height=26,
                                style={"background_color":0xFF3A2E08 if active else 0xFF1E1E1E,
                                       "color":0xFFFFEE88 if active else 0xFF999999,
                                       "font_size":10,"border_radius":3},
                            ).set_clicked_fn(lambda _p=p: _select_ind_light(_p))
                            ui.Button("X",width=26,height=26,
                                style={"background_color":0xFF4A1010,
                                       "color":0xFFFF6666,"font_size":10},
                            ).set_clicked_fn(lambda _p=p: _remove_ind_light(_p))

        def add_individual_light():
            """Click-to-place counterpart to dragging the Light card — places
            one individual light at the floor center, same kind/size/height
            as drag-and-drop (mirrors add_camera's floor-center placement)."""
            _rs()
            cx,cy,fz,_w,_d,_unit,tf = get_floor_info()
            ensure(RIG_PATH); ensure(IND_LIGHTS_PATH)
            idx = _ind_light_count[0]; _ind_light_count[0] += 1
            path = f"{IND_LIGHTS_PATH}/Light_{idx:02d}"
            UsdLux.RectLight.Define(stage, path)
            prim = stage.GetPrimAtPath(path)
            units_per_foot = tf / 10.0
            # An individual light is a NORMAL local fixture, not the scene-
            # sized panel the Quick-Fill grid uses — the sl_lwidth/sl_ldepth
            # sliders default to ~1.2x the whole floor (fine for the grid,
            # "fills the whole floor" for a single light). Fixed small size,
            # editable per-light afterwards in "Individual Lights".
            _ilsz = self._IND_LIGHT_DEFAULT_FT * units_per_foot
            set_attr(prim,"width",_ilsz,Sdf.ValueTypeNames.Float)
            set_attr(prim,"height",_ilsz,Sdf.ValueTypeNames.Float)
            set_attr(prim,"intensity",float(sl_intensity.model.get_value_as_float()),Sdf.ValueTypeNames.Float)
            set_attr(prim,"exposure",0.0,Sdf.ValueTypeNames.Float)
            set_attr(prim,"colorTemperature",float(sl_temp.model.get_value_as_float()),Sdf.ValueTypeNames.Float)
            set_attr(prim,"enableColorTemperature",True,Sdf.ValueTypeNames.Bool)
            set_attr(prim,"color",Gf.Vec3f(1,1,1),Sdf.ValueTypeNames.Color3f)
            z = fz + self._LIGHT_SPAWN_HEIGHT_FT * units_per_foot
            xf=UsdGeom.XformCommonAPI(prim)
            xf.SetTranslate(Gf.Vec3d(cx,cy,z))
            xf.SetRotate(Gf.Vec3f(0,0,0),UsdGeom.XformCommonAPI.RotationOrderXYZ)
            _ind_light_list.append(path)
            _schedule_ind_light_list_rebuild()
            try:
                from planlift_floorview.extension import _INSTANCE as _fpv
                if _fpv is not None:
                    _fpv._schedule_rebuild()
            except Exception:
                pass
            set_status(f"Light placed  ({cx:.2f}, {cy:.2f}, {z:.2f})")
            print(f"[FPT] {path}")

        def place_lights():
            _rs()
            cx,cy,fz,w,d,unit,tf = get_floor_info()
            intensity=float(sl_intensity.model.get_value_as_float())
            lw=float(sl_lwidth.model.get_value_as_float())
            ld=float(sl_ldepth.model.get_value_as_float())
            tk=float(sl_temp.model.get_value_as_float())
            ensure(RIG_PATH); ensure(LIGHTS_PATH)
            units_per_foot = tf / 10.0
            height_units   = _selected_height[0] * units_per_foot
            # Use scene-sized lights: cover the whole floor area
            actual_lw = max(lw, w * 1.2)
            actual_ld = max(ld, d * 1.2)
            print(f"[LIGHTS] scene={w:.1f}x{d:.1f}{unit}  center=({cx:.1f},{cy:.1f})  fz={fz:.3f}")
            print(f"[LIGHTS] light size={actual_lw:.1f}x{actual_ld:.1f}  height_per_floor={height_units:.3f}")
            for i in range(NUM_LIGHTS):
                n=i+1; z=fz+height_units*n; p=lpath(i)
                UsdLux.RectLight.Define(stage,p)
                prim=stage.GetPrimAtPath(p)
                if not prim: continue
                set_attr(prim,"width",actual_lw,Sdf.ValueTypeNames.Float)
                set_attr(prim,"height",actual_ld,Sdf.ValueTypeNames.Float)
                set_attr(prim,"intensity",intensity,Sdf.ValueTypeNames.Float)
                set_attr(prim,"exposure",0.0,Sdf.ValueTypeNames.Float)
                set_attr(prim,"colorTemperature",tk,Sdf.ValueTypeNames.Float)
                set_attr(prim,"enableColorTemperature",True,Sdf.ValueTypeNames.Bool)
                set_attr(prim,"color",Gf.Vec3f(1,1,1),Sdf.ValueTypeNames.Color3f)
                xf=UsdGeom.XformCommonAPI(prim)
                xf.SetTranslate(Gf.Vec3d(cx,cy,z))
                xf.SetRotate(Gf.Vec3f(0,0,0),UsdGeom.XformCommonAPI.RotationOrderXYZ)
            btn_add_light.visible=False; btn_rem_light.visible=True
            set_status(f"20 lights  {actual_lw:.0f}x{actual_ld:.0f}{unit}  10ft={tf:.3f}{unit}")

        def remove_lights():
            _rs()
            to_del=[lpath(i) for i in range(NUM_LIGHTS) if stage.GetPrimAtPath(lpath(i))]
            if stage.GetPrimAtPath(LIGHTS_PATH): to_del.append(LIGHTS_PATH)
            if to_del: omni.kit.commands.execute("DeletePrims",paths=to_del)
            btn_add_light.visible=True; btn_rem_light.visible=False
            set_status("Lights removed from stage.")

        def live_light(_=None):
            # The global "Light Properties" sliders drive the Quick-Fill grid
            # ONLY. Individually-placed lights are seeded from these sliders at
            # creation time and are independent afterwards — edit them per
            # light in the "Individual Lights" section (see
            # _on_ind_light_prop_changed).
            _rs()
            intensity = float(sl_intensity.model.get_value_as_float())
            temp      = float(sl_temp.model.get_value_as_float())
            if any_lights():
                _,_2,_3,w,d,_4,_5 = get_floor_info()
                lw = max(float(sl_lwidth.model.get_value_as_float()), w * 1.2)
                ld = max(float(sl_ldepth.model.get_value_as_float()), d * 1.2)
                for i in range(NUM_LIGHTS):
                    prim=stage.GetPrimAtPath(lpath(i))
                    if prim and prim.IsValid():
                        set_attr(prim,"intensity",intensity)
                        set_attr(prim,"width",lw)
                        set_attr(prim,"height",ld)
                        set_attr(prim,"colorTemperature",temp)

        def add_camera():
            _rs()
            cx,cy,fz,w,d,unit,tf = get_floor_info()
            ensure(RIG_PATH); ensure(CAMS_PATH)
            idx=_cam_count[0]; _cam_count[0]+=1
            pidx=cam_combo.model.get_item_value_model().get_value_as_int()
            ps=CAM_PRESETS[PRESET_KEYS[pidx]]
            path=f"{CAMS_PATH}/Cam_{idx:02d}_{ps['label']}"
            cam_path = path + "/Camera"

            # Rig: an Xform holding the camera's world position and yaw
            # (rotateZ) only. See _cam_optical_path — keeping yaw on a
            # separate outer prim from tilt/roll means panning this camera
            # later can never bleed into them.
            stage.DefinePrim(path, "Xform")
            rig_prim = stage.GetPrimAtPath(path)
            UsdGeom.Xformable(rig_prim).ClearXformOpOrder()
            rig_xf = UsdGeom.XformCommonAPI(rig_prim)
            _fpv_fz, _fpv_h = self._fpv_cam_height()
            cam_z = (_fpv_fz + _fpv_h) if _fpv_fz is not None else (fz + tf)
            rig_xf.SetTranslate(Gf.Vec3d(cx, cy, cam_z))
            rig_xf.SetRotate(Gf.Vec3f(0, 0, 0), UsdGeom.XformCommonAPI.RotationOrderXYZ)

            # Use Kit's standard creation so the built-in camera visual body is included
            import omni.kit.commands
            omni.kit.commands.execute("CreatePrimWithDefaultXformCommand",
                                      prim_type="Camera",
                                      prim_path=cam_path,
                                      select_new_prim=False)
            prim=stage.GetPrimAtPath(cam_path)
            if not prim.IsValid():
                # Fallback: define bare camera prim
                UsdGeom.Camera.Define(stage, cam_path)
                prim=stage.GetPrimAtPath(cam_path)
            cam=UsdGeom.Camera(prim)
            cam.GetFocalLengthAttr().Set(ps["focal_mm"])
            cam.GetHorizontalApertureAttr().Set(ps["h_ap"])
            cam.GetVerticalApertureAttr().Set(ps["v_ap"])
            cam.GetClippingRangeAttr().Set(Gf.Vec2f(0.001,1000000.0))
            prim.CreateAttribute("info:res_w",Sdf.ValueTypeNames.Int).Set(ps["res_w"])
            prim.CreateAttribute("info:res_h",Sdf.ValueTypeNames.Int).Set(ps["res_h"])
            # Clear any default xform ops from the command before applying our own.
            # Translate stays at the rig's origin (0,0,0) — position lives on
            # the rig, not here. Rotate keeps the existing pitch/roll
            # convention exactly (r[1]/yaw was already never touched here).
            UsdGeom.Xformable(prim).ClearXformOpOrder()
            xf=UsdGeom.XformCommonAPI(prim)
            xf.SetTranslate(Gf.Vec3d(0, 0, 0))
            xf.SetRotate(Gf.Vec3f(90,0,0),UsdGeom.XformCommonAPI.RotationOrderXYZ)
            self._attach_camera_body(stage, cam_path)
            _cam_list.append(path)
            _cam_colors[path] = CAM_COLORS[idx % len(CAM_COLORS)]
            _cam_floor[path] = self._fpv_active_floor_idx()
            switch_to_cam(path)
            set_status(f"{ps['label']}  {ps['res_w']}x{ps['res_h']}  "
                       f"focal={ps['focal_mm']}mm  H={ps['h_ap']}  V={ps['v_ap']}")
            print(f"[CAM] {path}")

        def _remove_cam(path):
            name = path.split("/")[-1]
            is_active = (_active_cam[0] == path)
            if path in _cam_list:
                _cam_list.remove(path)
            _cam_colors.pop(path, None)
            _cam_floor.pop(path, None)
            _remove_zone(path)
            others   = _cam_list[:]
            next_cam = others[-1] if others else None
            if is_active:
                _active_cam[0] = next_cam
                # Bypasses switch_to_cam (that would try to re-select the
                # camera we're mid-deleting), so resync the height slider
                # here explicitly — otherwise it keeps showing the deleted
                # camera's last-known height instead of the fallback one's.
                try:
                    self._jsv_sync_fn(self._jsv_read_height_fn())
                except Exception as e:
                    print(f"[JSV] resync on remove: {e}")
            _schedule_rebuild()
            set_status(f"Removing: {name} ...")
            try:
                import omni.kit.app, asyncio
                async def _deferred_delete():
                    app = omni.kit.app.get_app()
                    if is_active:
                        try:
                            if HAS_VP:
                                vp = vp_util.get_active_viewport()
                                if vp:
                                    target = next_cam if next_cam else "/OmniverseKit_Persp"
                                    vp.camera_path = _cam_optical_path(target)
                        except Exception as e:
                            print(f"[DELETE CAM] switch err: {e}")
                    await app.next_update_async()
                    await app.next_update_async()
                    try:
                        if stage.GetPrimAtPath(path):
                            omni.kit.commands.execute("DeletePrims", paths=[path])
                    except Exception as e:
                        print(f"[DELETE CAM] Delete err: {e}")
                    set_status(f"Camera removed: {name}")
                asyncio.ensure_future(_deferred_delete())
            except Exception as e:
                try:
                    if stage.GetPrimAtPath(path):
                        omni.kit.commands.execute("DeletePrims", paths=[path])
                except: pass
                set_status(f"Camera removed: {name}")

        # ── Camera Zones (occlusion-aware floor coverage) ───────────────────
        # Faithful port of Piyush's FloorPlanPanel zone system: dense grid
        # raycast against a manually cached triangle list, first-hit
        # classification (floor vs wall/pillar/furniture), convex hull of
        # the surviving floor points. No PhysX, no frustum-viz debug tool.

        def _cache_scene_triangles(force=False):
            """Cache world-space triangles for every real Mesh in the scene
            (skipping our own FloorPlanRig subtree) -- used by the dense
            occlusion-aware floor-visibility raycast in _generate_zone.
            Includes ALL geometry (floor, walls, furniture, pillars); the
            caller classifies each hit afterwards rather than filtering here,
            since a single ray needs to know not just "did I hit something"
            but "was it the floor or an obstacle". Polygons are
            fan-triangulated from vertex 0, correct for the simple convex
            wall/floor/furniture quads this pipeline generates.

            When numpy is available, triangles are stored as flat (N,3)
            arrays instead of a list of per-triangle tuples, plus a
            per-triangle bounding-sphere (center/radius) array, so
            _raycast_nearest can vectorize the same broad-phase-then-full-
            test strategy the pure-Python path already used, cutting a
            per-ray candidate set out of all N triangles before running the
            (much more expensive) full Moeller-Trumbore test on only that
            subset -- see that function for why this two-step shape matters
            (testing all N triangles per ray with no broad-phase measured
            ~7.3s per zone here; broad-phase-filtered brought it to ~0.5s —
            vs. ~14.8s for the original pure-Python per-triangle loop on
            this PC's ~61.5k-triangle scene, the actual cause of the 20-30s
            generation latency, not weaker hardware). Falls back to the
            original per-triangle list (each entry carrying its own
            center/radius) when numpy isn't available."""
            nonlocal _scene_tris, _scene_tri_np, _scene_tri_bp, _rc_cache
            _ct0 = time.perf_counter()
            s = _rs()

            # ── Reuse guard ────────────────────────────────────────────────
            # Walking every mesh and world-transforming its vertices is the
            # single most expensive part of generating a zone (~7s on this
            # ~250k-triangle building). The geometry is static in this
            # viewer, so reuse the existing cache when the stage is the same
            # object, it was built in the last 30s (same "static geometry"
            # assumption get_floor_info makes), and no Mesh prim has been
            # added or removed since (cheap count walk — no bbox/transform).
            # This makes the 2nd..Nth zone in a burst — every per-camera
            # "Zone" click, and every camera in "Generate All Zones" / a
            # Range-slider change — effectively free. force=True skips the
            # guard for callers that know geometry just changed.
            _have_cache = ((_scene_tri_np[0] is not None and _scene_tri_np[0].shape[0] > 0)
                           if HAS_NUMPY else bool(_scene_tris))
            if (not force and s is not None and s is _sct_cache["stage"] and _have_cache
                    and (_ct0 - _sct_cache["t"]) < 30.0):
                _mc = 0
                for _p in _walk_prims(s):
                    if _p.IsA(UsdGeom.Mesh) and "FloorPlanRig" not in str(_p.GetPath()):
                        _mc += 1
                if _mc == _sct_cache["mesh_count"]:
                    return (_scene_tri_np[0].shape[0] if HAS_NUMPY else len(_scene_tris))

            tris = []
            v0_list, v1_list, v2_list = [], [], []
            _mesh_count = 0
            # Per-mesh decimation for the RAYCAST CACHE only (never touches
            # the actual rendered/USD geometry) — occlusion/floor-visibility
            # testing doesn't need every triangle of a detailed catalog
            # model, just a reasonable approximation of its silhouette. A
            # single mesh contributing far more than this many triangles is
            # stride-sampled down to the cap; simple meshes (walls, floor,
            # low-poly furniture) are already well under it and untouched.
            # Caps every mesh individually (not a single global random
            # sample) so a handful of high-poly models can't crowd out
            # smaller ones and leave gaps in occlusion coverage.
            _RAYCAST_MAX_TRIS_PER_MESH = 300
            if s:
                for prim in _walk_prims(s):
                    if "FloorPlanRig" in str(prim.GetPath()):
                        continue
                    if not prim.IsA(UsdGeom.Mesh):
                        continue
                    _mesh_count += 1
                    try:
                        mesh   = UsdGeom.Mesh(prim)
                        pts    = mesh.GetPointsAttr().Get()
                        counts = mesh.GetFaceVertexCountsAttr().Get()
                        idxs   = mesh.GetFaceVertexIndicesAttr().Get()
                        if not pts or not counts or not idxs:
                            continue
                        xf  = UsdGeom.Xformable(prim)
                        mat = xf.ComputeLocalToWorldTransform(Usd.TimeCode.Default())

                        if HAS_NUMPY:
                            # Vectorised world-transform: one (V,4)@(4,4) matmul
                            # instead of a Python-level Gf.Matrix4d.Transform()
                            # call per vertex (the dominant cost of this scan on
                            # a multi-thousand-mesh building). USD Gf uses the
                            # row-vector convention (p' = p * M), so
                            # [x,y,z,1] @ M matches mat.Transform(p). The 4x4 is
                            # read element-by-element (16 reads/mesh, cheap) —
                            # np.asarray(Gf.Matrix4d) is NOT reliable across USD
                            # builds and a failure there was silently dropping
                            # whole meshes from the collision cache.
                            P  = np.asarray(pts, dtype=np.float64).reshape(-1, 3)
                            M  = np.array([[mat[i][j] for j in range(4)] for i in range(4)],
                                          dtype=np.float64)
                            Ph = np.empty((P.shape[0], 4), dtype=np.float64)
                            Ph[:, :3] = P
                            Ph[:, 3]  = 1.0
                            Wp = Ph @ M
                            w  = Wp[:, 3:4].copy()
                            w[w == 0.0] = 1.0
                            Wp = Wp[:, :3] / w
                            idx_np = np.asarray(idxs, dtype=np.int64)
                            cnt_np = np.asarray(counts, dtype=np.int64)
                            mv0 = None
                            if cnt_np.size and int(cnt_np.min()) == int(cnt_np.max()) >= 3:
                                # Uniform tri/quad/ngon mesh (the common case
                                # for this pipeline's output) — fan-triangulate
                                # every face at once with strided column slices.
                                c = int(cnt_np[0])
                                faces = idx_np.reshape(-1, c)
                                a0 = faces[:, 0]
                                p0, p1, p2 = [], [], []
                                for k in range(1, c - 1):
                                    p0.append(Wp[a0])
                                    p1.append(Wp[faces[:, k]])
                                    p2.append(Wp[faces[:, k + 1]])
                                mv0 = np.concatenate(p0, axis=0)
                                mv1 = np.concatenate(p1, axis=0)
                                mv2 = np.concatenate(p2, axis=0)
                            else:
                                # Mixed polygon sizes — Python fan, but index
                                # straight into the already-transformed Wp.
                                p0, p1, p2 = [], [], []
                                off = 0
                                for cnt in cnt_np.tolist():
                                    if cnt >= 3:
                                        i0 = int(idx_np[off])
                                        for k in range(1, cnt - 1):
                                            p0.append(i0)
                                            p1.append(int(idx_np[off + k]))
                                            p2.append(int(idx_np[off + k + 1]))
                                    off += cnt
                                if p0:
                                    mv0 = Wp[np.asarray(p0, dtype=np.int64)]
                                    mv1 = Wp[np.asarray(p1, dtype=np.int64)]
                                    mv2 = Wp[np.asarray(p2, dtype=np.int64)]
                            if mv0 is None or mv0.shape[0] == 0:
                                continue
                            _n = mv0.shape[0]
                            if _n > _RAYCAST_MAX_TRIS_PER_MESH:
                                sel = (np.arange(_RAYCAST_MAX_TRIS_PER_MESH, dtype=np.float64)
                                       * (_n / _RAYCAST_MAX_TRIS_PER_MESH)).astype(np.int64)
                                mv0, mv1, mv2 = mv0[sel], mv1[sel], mv2[sel]
                            v0_list.append(mv0)
                            v1_list.append(mv1)
                            v2_list.append(mv2)
                        else:
                            wpts = [mat.Transform(Gf.Vec3d(float(p[0]), float(p[1]), float(p[2]))) for p in pts]
                            _mesh_v0, _mesh_v1, _mesh_v2 = [], [], []
                            offset = 0
                            for cnt in counts:
                                if cnt >= 3:
                                    i0 = idxs[offset]
                                    for k in range(1, cnt - 1):
                                        _mesh_v0.append(wpts[i0])
                                        _mesh_v1.append(wpts[idxs[offset + k]])
                                        _mesh_v2.append(wpts[idxs[offset + k + 1]])
                                offset += cnt
                            _n = len(_mesh_v0)
                            if _n > _RAYCAST_MAX_TRIS_PER_MESH:
                                _stride = _n / _RAYCAST_MAX_TRIS_PER_MESH
                                _keep = [int(i * _stride) for i in range(_RAYCAST_MAX_TRIS_PER_MESH)]
                            else:
                                _keep = range(_n)
                            for _i in _keep:
                                v0, v1, v2 = _mesh_v0[_i], _mesh_v1[_i], _mesh_v2[_i]
                                ccx = (v0[0]+v1[0]+v2[0]) / 3.0
                                ccy = (v0[1]+v1[1]+v2[1]) / 3.0
                                ccz = (v0[2]+v1[2]+v2[2]) / 3.0
                                radius = max(
                                    ((v0[0]-ccx)**2 + (v0[1]-ccy)**2 + (v0[2]-ccz)**2) ** 0.5,
                                    ((v1[0]-ccx)**2 + (v1[1]-ccy)**2 + (v1[2]-ccz)**2) ** 0.5,
                                    ((v2[0]-ccx)**2 + (v2[1]-ccy)**2 + (v2[2]-ccz)**2) ** 0.5)
                                tris.append((v0, v1, v2, ccx, ccy, ccz, radius))
                    except Exception:
                        pass
            if HAS_NUMPY:
                v0a = np.concatenate(v0_list, axis=0) if v0_list else np.zeros((0, 3))
                v1a = np.concatenate(v1_list, axis=0) if v1_list else np.zeros((0, 3))
                v2a = np.concatenate(v2_list, axis=0) if v2_list else np.zeros((0, 3))
                _scene_tri_np = (v0a, v1a, v2a)
                centers = (v0a + v1a + v2a) / 3.0
                radius = np.maximum(
                    np.linalg.norm(v0a - centers, axis=1),
                    np.maximum(np.linalg.norm(v1a - centers, axis=1),
                               np.linalg.norm(v2a - centers, axis=1)),
                ) if v0_list else np.zeros(0)
                _scene_tri_bp = (centers, radius, radius * radius)
                # Triangles changed, so any cached per-origin broad-phase
                # arrays in _rc_cache (computed against the OLD centers) are
                # now stale — drop them so the next raycast recomputes.
                _rc_cache["origin"] = None
                tri_count = int(v0a.shape[0])
            else:
                _scene_tris = tris
                tri_count = len(tris)
            # This scan just rebuilt the triangle set from the live stage —
            # record the reuse signature, and drop the floor-level candidate
            # memo (its cached bboxes came from the pre-change geometry).
            _sct_cache.update(stage=s, t=time.perf_counter(), mesh_count=_mesh_count)
            _dfl_cache["cands"] = None
            print(f"[ZONE TIMING]   _cache_scene_triangles: {time.perf_counter()-_ct0:.2f}s "
                  f"({_mesh_count} meshes -> {tri_count} triangles, "
                  f"{'numpy' if HAS_NUMPY else 'pure-python'})")
            return tri_count

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

        def _raycast_nearest(ox, oy, oz, dx, dy, dz, max_t=None, _ov=None):
            """Closest-hit (distance, triangle) against the cached scene
            triangles, or (None, None) if the ray misses everything. Assumes
            (dx,dy,dz) is unit length.

            _ov, when given, is (np_tris, bp, rc_cache) — a camera-local
            prefiltered subset (see _generate_zone) used instead of the full
            scene cache, so far-away triangles never enter the per-ray
            broad-phase. Callers that don't pass it (e.g. wall collision)
            keep using the complete cache.

            NumPy path: two-step broad-phase + full test, both vectorized.
            First a cheap ray/bounding-sphere rejection against every cached
            triangle at once (O(N) array ops, no cross products) narrows N
            triangles down to a small per-ray candidate set; only that
            subset then runs the full Moeller-Trumbore test. Running the
            full test unconditionally on all N triangles (no broad-phase)
            measured ~7.3s per zone here; adding the vectorized broad-phase
            brought that to ~0.5s — vs. ~14.8s for the original
            pure-Python per-triangle loop with the same broad-phase idea
            (1271 rays x ~61.5k triangles on this PC's scene, the actual
            cause of 20-30s zone-generation latency, not weaker hardware;
            see [ZONE TIMING] in the log). The ray origin is the same for
            every ray in one zone's whole sampling grid, so the
            origin-dependent (but ray-independent) part of the broad-phase
            is cached in _rc_cache and only recomputed when the origin
            actually changes, instead of on every single ray.

            Pure-Python fallback (no numpy): original per-triangle loop,
            each triangle first tested against its own bounding sphere
            before the full Moeller-Trumbore test, to keep dense sampling
            tractable without numpy."""
            limit = max_t if max_t is not None else float('inf')

            if HAS_NUMPY:
                if _ov is not None:
                    v0a, v1a, v2a              = _ov[0]
                    centers, radius, radius_sq = _ov[1]
                    _rcc                       = _ov[2]
                else:
                    v0a, v1a, v2a              = _scene_tri_np
                    centers, radius, radius_sq = _scene_tri_bp
                    _rcc                       = _rc_cache
                if v0a.shape[0] == 0:
                    return None, None
                origin_key = (ox, oy, oz)
                if _rcc["origin"] != origin_key:
                    oc = centers - np.array(origin_key)
                    _rcc["origin"]  = origin_key
                    _rcc["oc"]      = oc
                    _rcc["dist_sq"] = np.sum(oc * oc, axis=1)
                oc      = _rcc["oc"]
                dist_sq = _rcc["dist_sq"]

                d = np.array((dx, dy, dz))
                t_closest = oc @ d
                reject = (t_closest - radius > limit) | (t_closest + radius < 0.0)
                reject |= (dist_sq - t_closest * t_closest) > radius_sq
                cand = np.nonzero(~reject)[0]
                if cand.size == 0:
                    return None, None

                v0 = v0a[cand]
                e1 = v1a[cand] - v0
                e2 = v2a[cand] - v0

                pvec = np.cross(d, e2)
                det = np.sum(e1 * pvec, axis=1)
                eps = 1e-9
                valid = np.abs(det) > eps
                safe_det = np.where(valid, det, 1.0)  # avoid div-by-zero; masked out below regardless
                inv_det = 1.0 / safe_det
                o = np.array(origin_key)
                tvec = o - v0
                u = np.sum(tvec * pvec, axis=1) * inv_det
                valid &= (u >= 0.0) & (u <= 1.0)
                qvec = np.cross(tvec, e1)
                v = np.sum(d * qvec, axis=1) * inv_det
                valid &= (v >= 0.0) & (u + v <= 1.0)
                t = np.sum(e2 * qvec, axis=1) * inv_det
                valid &= (t > 1e-6) & (t <= limit)
                if not np.any(valid):
                    return None, None
                t_masked = np.where(valid, t, np.inf)
                local_idx = int(np.argmin(t_masked))
                best_t = float(t_masked[local_idx])
                if not math.isfinite(best_t):
                    return None, None
                global_idx = int(cand[local_idx])
                best_tri = (Gf.Vec3d(*v0a[global_idx]), Gf.Vec3d(*v1a[global_idx]), Gf.Vec3d(*v2a[global_idx]))
                return best_t, best_tri

            best_t, best_tri = None, None
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

        def _clamp_move_by_walls(ox, oy, oz, nx, ny, margin=None):
            """If Wall Collision is on, clamp a proposed horizontal move from
            (ox,oy) to (nx,ny) at height oz so it stops just short of any
            wall-like surface in the way instead of passing through it.
            Returns (nx, ny) unchanged when collision is off, the scene has
            no cached geometry, or nothing wall-like blocks the straight
            line to the target.

            There's no explicit "this prim is a wall" tag anywhere in this
            pipeline's USD output (confirmed — walls, floor, and furniture
            are all just generic meshes), so this classifies a hit as
            "wall" purely by its surface normal being mostly vertical
            (abs(nz) < 0.5), the inverse of the floor classification
            _probe_ndc already uses (nz > 0.7 = floor). That also means a
            tall piece of furniture or a pillar reads as a wall too — not
            just literal walls — which in practice is the sensible
            behavior for "don't let the camera clip through things", even
            though it's broader than the literal ask."""
            if not _wall_collision[0]:
                return nx, ny
            dx, dy = nx - ox, ny - oy
            dist = math.sqrt(dx*dx + dy*dy)
            if dist < 1e-6:
                return nx, ny
            dxu, dyu = dx / dist, dy / dist
            have_tris = (bool(_scene_tris) if not HAS_NUMPY
                         else _scene_tri_np[0] is not None and _scene_tri_np[0].shape[0] > 0)
            if not have_tris:
                # Lazily populate on first use (e.g. toggled on but never
                # refreshed) rather than silently allowing movement through
                # walls — the toggle's own clicked_fn also refreshes this
                # up front so this path is normally only a cold-start fallback.
                _cache_scene_triangles(force=True)
                _ok = (bool(_scene_tris) if not HAS_NUMPY
                       else _scene_tri_np[0] is not None and _scene_tri_np[0].shape[0] > 0)
                if not _ok:
                    print("[WC] no scene triangles even after a forced rebuild — collision inert")
                    return nx, ny
            _, _, _, _rw, _rd, _, _tf = get_floor_info()
            if margin is None:
                margin = max((_rw**2 + _rd**2) ** 0.5 * 0.004, _tf * 0.05)
            t, tri = _raycast_nearest(ox, oy, oz, dxu, dyu, 0.0, max_t=dist)
            if t is None or tri is None:
                return nx, ny
            v0, v1, v2 = tri
            n = Gf.Cross(v1 - v0, v2 - v0)
            n_len = n.GetLength()
            if n_len < 1e-9:
                return nx, ny
            nz = abs(n[2] / n_len)
            if nz >= 0.5:   # horizontal-ish (floor/ceiling/tabletop) — not a wall
                return nx, ny
            stop_t = max(0.0, t - margin)
            print(f"[WC] stop: wall at t={t:.2f} (nz={nz:.2f}), move {dist:.2f} -> {stop_t:.2f}")
            return ox + dxu * stop_t, oy + dyu * stop_t

        def _zone_ray_grid():
            """(cols, rows) sample density for the occlusion-aware floor scan
            in _generate_zone, from _zone_quality[0] ("low"/"medium"/"high").

            Ray sampling is uniform in angle, not in world-space distance —
            toward a grazing/oblique viewing angle (exactly what a room's
            far corners usually are from an elevated camera) the world-
            space gap between adjacent rays grows sharply, so the hull can
            end up visibly short of the true corner even though the floor
            keeps going there (confirmed: at the old "medium" density
            (1271 rays) the resulting hull had only 8-11 vertices total for
            the whole room). These numbers were sized back when the
            per-triangle-loop raycast made anything denser too slow to be
            usable (~14.8s for 1271 rays); the numpy broad-phase rewrite
            measured 0.31s for that same 1271 rays on this PC's ~61.5k-
            triangle scene, so there's now real headroom to sample much
            more densely instead."""
            return {"low": (40, 30), "medium": (80, 60), "high": (140, 105)}.get(
                _zone_quality[0], (80, 60))

        def _polygon_area(pts):
            """Shoelace formula over a planar (x,y) point list — used on the
            zone's hull (the same points the FloorZone mesh's fan
            triangulation is built from) to get its floor area in stage
            units^2. Works for any simple polygon, convex or not; the hull
            here is always convex, but the formula doesn't require it."""
            n = len(pts)
            if n < 3: return 0.0
            a = 0.0
            for i in range(n):
                x1, y1 = pts[i][0], pts[i][1]
                x2, y2 = pts[(i + 1) % n][0], pts[(i + 1) % n][1]
                a += x1 * y2 - x2 * y1
            return abs(a) / 2.0

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
            for prim in _walk_prims(s):
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

        def _detect_floor_level_candidates(s):
            """Raw (top_z, xy_area) for every large, thin, flat mesh in the
            scene — the camera-independent half of _detect_floor_levels.
            Walking every mesh's world bound costs about as much as half of
            _cache_scene_triangles, and a batch of zone generations (or the
            Range slider re-running every camera) would otherwise pay it once
            per camera, so memoise on stage identity + a 30s TTL — the same
            "geometry is static in this viewer" assumption get_floor_info
            makes. _cache_scene_triangles clears this whenever it does a real
            triangle rebuild."""
            if (_dfl_cache["cands"] is not None and _dfl_cache["stage"] is s
                    and (time.perf_counter() - _dfl_cache["t"]) < 30.0):
                return _dfl_cache["cands"]
            _mpu_val   = _mpu()
            min_area   = 0.5 / (_mpu_val ** 2)          # >= 0.5 real m^2 of XY footprint
            thick_cap  = max(2.0, 1.5 / _mpu_val)       # <= 1.5 real metres thick — fixed, not camera-height-scaled
            bc = UsdGeom.BBoxCache(Usd.TimeCode.Default(),
                [UsdGeom.Tokens.default_, UsdGeom.Tokens.render], useExtentsHint=True)
            candidates = []
            for prim in _walk_prims(s):
                if "FloorPlanRig" in str(prim.GetPath()): continue
                if not prim.IsA(UsdGeom.Mesh): continue
                try:
                    r       = bc.ComputeWorldBound(prim).ComputeAlignedRange()
                    mn_p, mx_p = r.GetMin(), r.GetMax()
                    xy_area = (mx_p[0]-mn_p[0]) * (mx_p[1]-mn_p[1])
                    z_thick = mx_p[2] - mn_p[2]
                    top_z   = float(mx_p[2])
                    if xy_area > min_area and z_thick < thick_cap:
                        candidates.append((top_z, xy_area))
                except: pass
            _dfl_cache.update(stage=s, t=time.perf_counter(), cands=candidates)
            return candidates

        def _detect_floor_levels(s, cam_z):
            """Every distinct large flat 'walkable' surface height at/below
            the camera — the ground floor AND any mezzanine / upper storey /
            void-cut balcony corridor — instead of a single highest one.
            (See _detect_floor_level_candidates for the cached raw scan; this
            just applies the per-camera 'below the camera' cut, merges mesh
            pieces at the same height, and drops slivers.)

            A "balcony" in this pipeline is a void cut in a storey's slab
            with the loop walls turned into a railing, so the balcony deck is
            that storey's OWN slab at its yBase — a real distinct level here,
            not a thin add-on."""
            _, _, _, _rw, _rd, _unit, _tf = get_floor_info()
            candidates = [c for c in _detect_floor_level_candidates(s) if c[0] < cam_z]
            if not candidates:
                _, _, fz, _, _, _, _ = get_floor_info()
                return [{"z": fz, "area": 0.0}]
            merge_tol = max(2.0, _tf * 0.05)
            # Merge candidates whose heights are within merge_tol (one physical
            # level split across several mesh pieces — e.g. a mezzanine deck
            # made of more than one slab) into a single level entry.
            candidates.sort(key=lambda c: c[0])
            levels = []
            for z, area in candidates:
                if levels and (z - levels[-1]["z"]) <= merge_tol:
                    levels[-1]["z"]     = max(levels[-1]["z"], z)
                    levels[-1]["area"] += area
                else:
                    levels.append({"z": z, "area": area})
            # Drop negligible slivers that technically passed the per-mesh
            # filter but aren't a real distinct level, then cap the count —
            # a real building has a handful of storeys/platforms at most.
            max_area = max(l["area"] for l in levels)
            levels = [l for l in levels if l["area"] >= max_area * 0.03]
            levels.sort(key=lambda l: l["area"], reverse=True)
            return levels[:4]

        def _generate_zone(path, _skip_cache_refresh=False):
            # Timing breakdown — printed at the end of this function. Added
            # because this same code runs in 1-2s on a much weaker PC but
            # 20-30s here, which isn't explained by "the algorithm is slow"
            # (a weaker CPU being faster rules that out) — something
            # specific to this scene/setup is the bottleneck, and guessing
            # which phase without data wastes time. Read the numbers this
            # prints next time a zone is generated instead of assuming.
            _tt0 = time.perf_counter()
            s = _rs()
            if not s: return
            # The actual Camera prim (focal/aperture, and the transform to
            # raycast from) lives at _cam_optical_path(path), not path itself
            # — path is the rig, which only carries position + yaw. Computing
            # the world transform on the optical prim naturally folds in the
            # rig's position/yaw as an ancestor, so this is still the camera's
            # full, correct pose.
            cam_prim = s.GetPrimAtPath(_cam_optical_path(path))
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
            # scene geometry. Only rays whose FIRST hit is a real walkable
            # LEVEL — the ground floor, or a mezzanine/platform deck, or any
            # other distinct flat surface below the camera (see
            # _detect_floor_levels) — count as "visible floor"; a ray that
            # hits a wall, a pillar, or furniture first is dropped. A camera
            # framed across a mezzanine gets its own hull on the mezzanine
            # deck AND a separate hull on whatever ground is visible past its
            # edge, instead of only one or the other (see per-level bucketing
            # below) — each level's polygon is the convex hull of that
            # level's own visible samples, informed by hundreds of real
            # occlusion-tested points; still convex per level, so it can't
            # carve out a hole for something like a pillar mid-level.
            # If this camera is stamped to a known floor (see _cam_floor,
            # populated at every camera-creation site), trust that floor's
            # own registry Z directly instead of re-deriving candidate
            # levels heuristically from the whole stage — on a multi-floor
            # stack the heuristic has no notion of "this camera's own
            # floor" and can miss it entirely or attribute the zone to the
            # wrong level. Cameras with no stamp yet (legacy, or a plain
            # single-floor scene) keep today's exact behavior.
            _fp_idx = _cam_floor.get(path)
            _fp_reg = None
            _fpv_zone = None
            if _fp_idx is not None:
                try:
                    from planlift_floorview.extension import _INSTANCE as _fpv_zone
                    if _fpv_zone is not None:
                        _fp_reg = _fpv_zone._floor_registry_entry(_fp_idx)
                except Exception:
                    _fp_reg = None
            _, _, _, _rw, _rd, _unit, _tf = get_floor_info()
            floor_tol = max(2.0, _tf * 0.05)   # how close a hit must be to a level's z to count as "on it"

            # Detect every walkable deck from geometry (ground slab + any
            # upper storey / mezzanine / void-cut balcony corridor below the
            # camera), regardless of the per-camera floor stamp. The stamp
            # alone is not enough: a "balcony" here is a void cut in a
            # storey's slab with the loop walls turned into a railing, so its
            # deck sits at that storey's own yBase — and if the stamp's
            # registry entry has no/renamed yBase key it silently falls back
            # to 0.0 (seen live as levels=[0.0] with the camera physically
            # ~4m up on the balcony), which then rejected every real corridor
            # hit and drew no zone on the balcony at all. Keep the stamped
            # level too as a guaranteed entry, but only add it when no
            # detected level already covers that height.
            levels = _detect_floor_levels(s, float(origin[2]))
            if _fp_reg is not None:
                # yBase is stored in real metres (editor.html's own units) —
                # convert to the stage's raw units before using it as a
                # raycast target Z (see planlift_floorview._raw_units_per_metre).
                _fp_z_raw = _fpv_zone._metres_to_raw(_fp_reg.get("yBase", 0.0))
                if not any(abs(l["z"] - _fp_z_raw) <= floor_tol for l in levels):
                    levels.append({"z": _fp_z_raw, "area": 1.0})
            # Range (R, from the Range slider — 1 to 200) is meant as a
            # hard cap on realistic detection distance IN METRES, not just
            # a raycast performance/safety limit — a camera can't reliably
            # detect a person arbitrarily far away just because the room
            # is big enough for the wall to be visible that far. This used
            # to go through _zone_reach_cap(R), which explicitly extended
            # past R to "reach any wall in the room regardless of Range"
            # — that's why a camera at one end of a long room produced a
            # zone stretching all the way to the far wall instead of
            # stopping at Range. Occlusion (walls/furniture closer than R)
            # still correctly limits the zone as before; only the "extend
            # past Range to find a wall" override is gone.
            #
            # R itself is in metres (the slider's whole design — min 1,
            # max 200 — only makes sense as metres; 200 raw units would be
            # a useless ~2m cap on this centimetre-scaled stage), but every
            # raycast distance here (cap, the frustum/floor math, _tf,
            # floor_tol) is in raw stage units — so R has to be converted
            # via the same real-metres-per-raw-unit factor as everywhere
            # else (_mpu()), or a "10m" Range would raycast only 10 raw
            # units = 10cm and every probe would miss (confirmed live).
            cap = R / _mpu()

            # Rebuilding this from scratch (walking every mesh in the stage)
            # is the single most expensive part of generating a zone. A lone
            # "Zone" click lets _cache_scene_triangles' own reuse signature
            # decide (stage identity + 30s TTL + mesh count) so the 2nd..Nth
            # zone in a burst skips the ~7s rebuild; "Generate All Zones" /
            # the Range slider pass _skip_cache_refresh=True after refreshing
            # once up front, so the same unchanged scene isn't rescanned N
            # times in a row.
            if not _skip_cache_refresh:
                _cache_scene_triangles()
            _tt1 = time.perf_counter()

            cols, rows = _zone_ray_grid()
            ox, oy, oz = float(origin[0]), float(origin[1]), float(origin[2])
            level_zs = [lvl["z"] for lvl in levels]
            sample_pts_by_level = {z: [] for z in level_zs}

            # ── Camera-local triangle prefilter ───────────────────────────
            # Every probe ray caps its hit distance at `cap` (Range), so a
            # cached triangle can only be a hit if its center is within
            # cap + 2*radius of the camera: broad-phase keeps it only when
            # t_closest <= cap + radius, and for it to lie near a ray its
            # center is within `radius` of that, so dist_center <= cap +
            # 2*radius is a safe superset (never drops a possible hit).
            # Slicing the global cache down to that near set before the dense
            # grid turns a ~250k-triangle per-ray broad-phase into a few-
            # thousand one on a large building, with no change to the result.
            # Passed through _probe_ndc -> _raycast_nearest as `_ov`; the
            # global cache is untouched so wall collision still sees it all.
            _pf_ov = None
            _pf_kept = _active_tris = (int(_scene_tri_np[0].shape[0])
                                       if (HAS_NUMPY and _scene_tri_np[0] is not None) else len(_scene_tris))
            if HAS_NUMPY and _scene_tri_np[0] is not None and _scene_tri_np[0].shape[0] > 0:
                _pv0, _pv1, _pv2 = _scene_tri_np
                _pctr, _prad, _ = _scene_tri_bp
                _pd2 = np.sum((_pctr - np.array([ox, oy, oz])) ** 2, axis=1)
                _pkeep = _pd2 <= (cap + 2.0 * _prad) ** 2
                _pn = int(np.count_nonzero(_pkeep))
                if 0 < _pn < _pv0.shape[0]:
                    _rk = _prad[_pkeep]
                    _pf_ov = ((_pv0[_pkeep], _pv1[_pkeep], _pv2[_pkeep]),
                              (_pctr[_pkeep], _rk, _rk * _rk),
                              {"origin": None, "oc": None, "dist_sq": None})
                    _pf_kept = _pn

            def _nearest_level(hit_z):
                """The level whose z is within floor_tol of hit_z, or None —
                the ray's hit height decides which level's hull it feeds,
                so a mezzanine-deck hit and a ground hit from the same scan
                land in separate buckets instead of one colliding fz check."""
                best, best_d = None, floor_tol
                for z in level_zs:
                    d = abs(hit_z - z)
                    if d < best_d:
                        best, best_d = z, d
                return best

            def _probe_ndc(ndc_x, ndc_y, _debug_tag=None):
                """Cast one occlusion-aware floor-visibility ray through the
                given camera-local NDC coordinate. Returns (world_x, world_y,
                level_z) for whichever detected level the ray actually hit,
                or None if the ray misses, exceeds Range, hits a non-floor
                obstacle first, or the hit doesn't look like flat floor at
                any known level. Shared by the main sampling grid below and
                the edge-reinforcement pass after it — a real raycast against
                actual scene geometry either way, so a point from either one
                can never claim coverage through a wall, past the Range cap,
                or off the edge of the real floor mesh (unlike a blind
                camera-ray/infinite-floor-plane intersection).

                _debug_tag: when set, prints why this specific probe hit or
                missed — pass the 4 frustum corners through with tags to
                diagnose why the zone hull isn't reaching them, since a miss
                could be "no geometry hit", "hit something past the cap",
                or "hit something, but it didn't look like floor" and those
                point at completely different fixes."""
                local_dir = Gf.Vec3d(ndc_x, ndc_y, -1.0)
                llen = local_dir.GetLength()
                if llen < 1e-9:
                    return None
                w_dir = Gf.Vec3d(*w_mat.TransformDir(local_dir / llen))
                wlen = w_dir.GetLength()
                if wlen < 1e-9:
                    return None
                dxf, dyf, dzf = float(w_dir[0]/wlen), float(w_dir[1]/wlen), float(w_dir[2]/wlen)

                t, tri = _raycast_nearest(ox, oy, oz, dxf, dyf, dzf, max_t=cap, _ov=_pf_ov)
                if t is None or t > cap or tri is None:
                    if _debug_tag:
                        print(f"[ZONE DEBUG] {_debug_tag}: MISS — no geometry hit within cap={cap:.3f} "
                              f"(t={t})")
                    return None

                v0, v1, v2 = tri
                e1 = v1 - v0
                e2 = v2 - v0
                n  = Gf.Cross(e1, e2)
                n_len = n.GetLength()
                if n_len < 1e-9:
                    if _debug_tag:
                        print(f"[ZONE DEBUG] {_debug_tag}: MISS — degenerate hit triangle normal")
                    return None
                nz = n[2] / n_len
                hit_z = oz + dzf * t
                lvl_z = _nearest_level(hit_z) if nz > 0.7 else None
                if lvl_z is not None:   # near-horizontal, facing up, at a known level's height
                    if _debug_tag:
                        print(f"[ZONE DEBUG] {_debug_tag}: HIT at t={t:.3f}, "
                              f"world=({ox+dxf*t:.3f},{oy+dyf*t:.3f}), nz={nz:.3f}, "
                              f"hit_z={hit_z:.3f} (level={lvl_z:.3f}, tol={floor_tol:.3f})")
                    return (ox + dxf*t, oy + dyf*t, lvl_z)
                if _debug_tag:
                    print(f"[ZONE DEBUG] {_debug_tag}: MISS — hit at t={t:.3f} but not floor-like "
                          f"(nz={nz:.3f} [need >0.7], hit_z={hit_z:.3f}, levels={[round(z,3) for z in level_zs]} tol={floor_tol:.3f})")
                return None

            for j in range(rows + 1):
                ndc_y = -tv + (2.0 * tv) * j / rows
                for i in range(cols + 1):
                    ndc_x = -th + (2.0 * th) * i / cols
                    hit = _probe_ndc(ndc_x, ndc_y)
                    if hit is not None:
                        sample_pts_by_level[hit[2]].append((hit[0], hit[1]))
            _tt2 = time.perf_counter()

            print(f"[ZONE DEBUG] cam={cam_name} th={th:.4f} tv={tv:.4f} "
                  f"R={R:.3f}m cap={cap:.3f}raw (mpu={_mpu():.4f}) "
                  f"levels={[round(z,3) for z in level_zs]} floor_tol={floor_tol:.3f} origin=({ox:.3f},{oy:.3f},{oz:.3f})")
            _probe_ndc(-th, -tv, _debug_tag="corner BL (-th,-tv)")
            _probe_ndc( th, -tv, _debug_tag="corner BR (+th,-tv)")
            _probe_ndc(-th,  tv, _debug_tag="corner TL (-th,+tv)")
            _probe_ndc( th,  tv, _debug_tag="corner TR (+th,+tv)")

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
            # sitting in the middle of an otherwise-open room. Each level's
            # hull is built only from that level's own bucket in
            # sample_pts_by_level (already populated by the base grid above).

            # NOTE: this used to also force-add the camera's own ground
            # footprint (ox, oy) as a hull anchor, to close a "gap" between
            # the hull's near edge and directly beneath the camera. That was
            # wrong: it turns the near edge into a 3-point kink (LB corner ->
            # camera's own footprint -> RB corner) instead of the straight
            # LB-to-RB line the frustum's bottom clip plane actually produces
            # when intersected with a flat floor (two planes always meet in
            # a straight line) — and the camera's FOV often doesn't reach the
            # floor directly below it at all, so anchoring there stretched
            # the zone into floor area the camera can't actually see. The
            # hull is now built only from real occlusion-tested visible-
            # floor hits.

            # Extra samples concentrated at the 4 frustum corners (and along
            # the edges leading into them), on top of the base grid above.
            # Ray sampling is uniform in *angle*, not in world-space distance
            # — toward a grazing/oblique viewing angle (exactly what a
            # room's far corners usually are from an elevated camera) the
            # world-space gap between adjacent rays grows sharply, so the
            # base grid's hull can end up visibly short of the true corner
            # even though the floor keeps going there. This pass probes
            # extra points along each of the 4 frustum edges, eased so they
            # cluster near the corners specifically (where the gap is
            # worst) rather than spread evenly, through the exact same
            # _probe_ndc used above — so, unlike projecting the frustum
            # corners onto an infinite floor plane, a point from this pass
            # is still a real occlusion-tested raycast hit and can't leak
            # through a wall or past the camera's configured Range.
            _EDGE_SUBDIV = 24
            def _eased(u):
                # Slow near u=0/1 (dense sampling at the corners), fast near
                # u=0.5 (sparse in the middle of the edge, already well
                # covered by the base grid).
                return 0.5 - 0.5 * math.cos(math.pi * u)
            for k in range(_EDGE_SUBDIV + 1):
                u = _eased(k / _EDGE_SUBDIV)
                ex = -th + 2.0 * th * u
                for ey in (-tv, tv):
                    hit = _probe_ndc(ex, ey)
                    if hit is not None:
                        sample_pts_by_level[hit[2]].append((hit[0], hit[1]))
                ey2 = -tv + 2.0 * tv * u
                for ex2 in (-th, th):
                    hit = _probe_ndc(ex2, ey2)
                    if hit is not None:
                        sample_pts_by_level[hit[2]].append((hit[0], hit[1]))

            def _hull_ccw(pts):
                pts = sorted(set((round(p[0], 6), round(p[1], 6)) for p in pts))
                if len(pts) <= 2:
                    return pts
                def _cross(o, a, b):
                    return (a[0]-o[0])*(b[1]-o[1]) - (a[1]-o[1])*(b[0]-o[0])
                # Popping only on an exact "<= 0" cross product removes just
                # perfectly-collinear points. Raycast sampling noise means
                # points along a straight wall are almost never *exactly*
                # collinear, so dozens of them used to survive as their own
                # tiny hull vertices, and fan-triangulating past them produced
                # long sliver triangles — visible as a hairline seam/gap in
                # the render. Treat points within ~2cm of the line as
                # collinear too, so hull edges stay clean straight lines.
                hull_tol = 0.02 if _unit == "m" else 2.0
                def _pop(o, a, b):
                    seg = ((b[0]-o[0])**2 + (b[1]-o[1])**2) ** 0.5
                    if seg < 1e-9:
                        return _cross(o, a, b) <= 0
                    return (_cross(o, a, b) / seg) <= hull_tol
                lo = []
                for p in pts:
                    while len(lo) >= 2 and _pop(lo[-2], lo[-1], p):
                        lo.pop()
                    lo.append(p)
                hi = []
                for p in reversed(pts):
                    while len(hi) >= 2 and _pop(hi[-2], hi[-1], p):
                        hi.pop()
                    hi.append(p)
                return lo[:-1] + hi[:-1]

            # One polygon per level that actually got any samples — a camera
            # framed across a mezzanine typically produces two here (the
            # mezzanine deck's own polygon, and a separate one for whatever
            # ground is visible past its edge), instead of one mixing points
            # from both heights (which the old single-fz version would have
            # rejected down to whichever ONE height matched).
            #
            # VOID-AWARE: a "balcony" deck here is a storey slab with a void
            # cut in it and the loop walls turned into a railing, so the real
            # walkable area is a RING. Rays aimed at the void fall through and
            # register on the ground level, not this one, so this level's hit
            # cloud already has an empty middle — but its convex hull fills
            # that middle back in and the zone then covers the open void.
            # So: raster the level's own hits, morphologically close the
            # grazing-angle sampling gaps, trace the covered blob's boundary,
            # and read back any fully-enclosed uncovered pocket as a HOLE
            # loop; keep a smooth outer boundary (the hull, or a hard-
            # simplified trace when the deck is genuinely L/notch shaped) and
            # ear-clip outer-minus-holes so the void is actually cut out.
            _mpu_val = _mpu()

            def _signed_area(p):
                a = 0.0
                for i in range(len(p)):
                    x1, y1 = p[i]; x2, y2 = p[(i + 1) % len(p)]
                    a += x1 * y2 - x2 * y1
                return a * 0.5

            def _pt_in_poly(pt, poly):
                x, y = pt; inside = False; n = len(poly); j = n - 1
                for i in range(n):
                    xi, yi = poly[i]; xj, yj = poly[j]
                    denom = (yj - yi) or 1e-12
                    if ((yi > y) != (yj > y)) and (x < (xj - xi) * (y - yi) / denom + xi):
                        inside = not inside
                    j = i
                return inside

            def _covered_boundary_loops(cov):
                # Directed cell-edge boundary with the covered region always on
                # the LEFT — so the outer ring comes back CCW (area > 0) and
                # every enclosed hole CW (area < 0).
                E = {}
                for (c, r) in cov:
                    if (c, r - 1) not in cov: E.setdefault((c, r),         []).append((c + 1, r))
                    if (c + 1, r) not in cov: E.setdefault((c + 1, r),     []).append((c + 1, r + 1))
                    if (c, r + 1) not in cov: E.setdefault((c + 1, r + 1), []).append((c, r + 1))
                    if (c - 1, r) not in cov: E.setdefault((c, r + 1),     []).append((c, r))
                loops = []
                remaining = sum(len(v) for v in E.values())
                guard = 0
                while remaining > 0 and guard < remaining + 16:
                    guard += 1
                    start = None
                    for k, v in E.items():
                        if v:
                            start = k; break
                    if start is None:
                        break
                    loop = [start]; cur = start
                    while True:
                        nxts = E.get(cur)
                        if not nxts:
                            break
                        nxt = nxts.pop(); remaining -= 1
                        if nxt == start:
                            break
                        loop.append(nxt); cur = nxt
                    if len(loop) >= 4:
                        loops.append(loop)
                return loops

            def _rdp_closed(loop, eps):
                """Ramer-Douglas-Peucker on a closed loop: split at vertex 0
                and the vertex farthest from it, simplify each half open.
                Collapses the stair-steps of a raster-traced boundary into
                clean straight runs."""
                n = len(loop)
                if n < 5:
                    return list(loop)
                def _pd(p, a, b):
                    ax, ay = a; bx, by = b; px, py = p
                    dx, dy = bx - ax, by - ay
                    L = (dx * dx + dy * dy) ** 0.5
                    if L < 1e-9:
                        return ((px - ax) ** 2 + (py - ay) ** 2) ** 0.5
                    return abs(dx * (ay - py) - dy * (ax - px)) / L
                def _open(seg):
                    if len(seg) < 3:
                        return list(seg)
                    dm, idx = -1.0, 0
                    for i in range(1, len(seg) - 1):
                        d = _pd(seg[i], seg[0], seg[-1])
                        if d > dm:
                            dm, idx = d, i
                    if dm > eps:
                        return _open(seg[:idx + 1])[:-1] + _open(seg[idx:])
                    return [seg[0], seg[-1]]
                far = max(range(n), key=lambda i: (loop[i][0] - loop[0][0]) ** 2
                          + (loop[i][1] - loop[0][1]) ** 2)
                if far == 0:
                    return list(loop)
                a = _open(loop[0:far + 1])
                b = _open(loop[far:] + [loop[0]])
                return a[:-1] + b[:-1]

            def _largest_cc(cells):
                """Largest 4-connected component of a cell set — drops the
                small ragged blobs a few stray leak-through hits produce."""
                cells = set(cells)
                best = set(); seen = set()
                for c0 in cells:
                    if c0 in seen:
                        continue
                    stack = [c0]; comp = set()
                    while stack:
                        c = stack.pop()
                        if c in comp:
                            continue
                        comp.add(c); seen.add(c)
                        for d in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                            nc = (c[0] + d[0], c[1] + d[1])
                            if nc in cells and nc not in comp:
                                stack.append(nc)
                    if len(comp) > len(best):
                        best = comp
                return best

            def _earcut(poly):
                """Ear-clip a simple polygon (possibly with zero-width bridge
                slits from _tri_with_holes). Robust to the slit: treats a
                candidate vertex coincident with an ear corner as outside, and
                if no clean ear is found in a full pass, cuts the most convex
                corner anyway so it always fully triangulates."""
                n = len(poly)
                if n < 3:
                    return []
                def _a2(a, b, c):
                    return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])
                def _near(p, q):
                    return abs(p[0] - q[0]) + abs(p[1] - q[1]) < 1e-6
                def _in_tri(p, a, b, c):
                    if _near(p, a) or _near(p, b) or _near(p, c):
                        return False
                    d1 = _a2(p, a, b); d2 = _a2(p, b, c); d3 = _a2(p, c, a)
                    return not (((d1 < 0) or (d2 < 0) or (d3 < 0)) and ((d1 > 0) or (d2 > 0) or (d3 > 0)))
                V = list(range(n))
                if _signed_area(poly) < 0:
                    V.reverse()
                tris = []
                guard = 0
                while len(V) > 3 and guard < 5 * n + 100:
                    guard += 1
                    m = len(V); found = False
                    for k in range(m):
                        ia, ib, ic = V[(k - 1) % m], V[k], V[(k + 1) % m]
                        a, b, c = poly[ia], poly[ib], poly[ic]
                        if _a2(a, b, c) <= 1e-9:
                            continue
                        blocked = False
                        for j in range(m):
                            vj = V[j]
                            if vj in (ia, ib, ic):
                                continue
                            if _in_tri(poly[vj], a, b, c):
                                blocked = True; break
                        if not blocked:
                            tris.append((ia, ib, ic)); V.pop(k); found = True; break
                    if not found:
                        best = None
                        for k in range(m):
                            ia, ib, ic = V[(k - 1) % m], V[k], V[(k + 1) % m]
                            ar = _a2(poly[ia], poly[ib], poly[ic])
                            if ar > 1e-9 and (best is None or ar > best[0]):
                                best = (ar, k, ia, ib, ic)
                        if best is None:
                            break
                        _, k, ia, ib, ic = best
                        tris.append((ia, ib, ic)); V.pop(k)
                if len(V) == 3:
                    tris.append((V[0], V[1], V[2]))
                return tris

            def _tri_with_holes(outer, holes):
                """(verts2d, [(i,j,k), ...]) for a CCW outer ring minus CW
                hole rings — each hole bridged into the ring by a zero-width
                cut from its right-most vertex to the nearest ring vertex,
                then ear-clipped. USD Mesh has no hole primitive."""
                ring = list(outer)
                if _signed_area(ring) < 0:
                    ring.reverse()
                hs = []
                for hh in holes:
                    if len(hh) < 3:
                        continue
                    h2 = list(hh)
                    if _signed_area(h2) > 0:
                        h2.reverse()
                    hs.append(h2)
                hs.sort(key=lambda hh: -max(p[0] for p in hh))
                for hh in hs:
                    hi = max(range(len(hh)), key=lambda i: hh[i][0])
                    M = hh[hi]
                    cand = [i for i in range(len(ring)) if ring[i][0] >= M[0] - 1e-9] or list(range(len(ring)))
                    oi = min(cand, key=lambda i: (ring[i][0] - M[0]) ** 2 + (ring[i][1] - M[1]) ** 2)
                    rot = hh[hi:] + hh[:hi]
                    ring = ring[:oi + 1] + rot + [rot[0], ring[oi]] + ring[oi + 1:]
                dd = []
                for p in ring:
                    if not dd or (abs(dd[-1][0] - p[0]) + abs(dd[-1][1] - p[1])) > 1e-7:
                        dd.append(p)
                if len(dd) > 1 and (abs(dd[0][0] - dd[-1][0]) + abs(dd[0][1] - dd[-1][1])) < 1e-7:
                    dd.pop()
                return dd, _earcut(dd)

            def _level_polygon(pts):
                """(outer_ccw, [hole_cw, ...]) in world XY for one level's hit
                cloud: a SMOOTH outer boundary (the convex hull when the deck
                is ~convex, else the raster-traced outline simplified hard so
                it's clean straight runs) plus any enclosed void as a
                simplified hole loop. Only the level's largest connected blob
                of hits is used, so a few stray leak-through hits on another
                surface can't add ragged fragments.

                A "balcony" deck here is a storey slab with a void cut in it
                and the loop walls turned into a railing, so its real walkable
                area is a RING; the convex hull alone would bridge over the
                void and cover it."""
                hull = _hull_ccw(pts)
                if len(hull) < 3:
                    return None, []
                if len(pts) < 24:
                    return hull, []
                xs = [p[0] for p in pts]; ys = [p[1] for p in pts]
                minx, maxx = min(xs), max(xs); miny, maxy = min(ys), max(ys)
                w = maxx - minx; h = maxy - miny
                span = max(w, h)
                if span < 1e-6:
                    return hull, []
                cell = max((0.30 / _mpu_val) if _mpu_val > 0 else 0.30, span / 60.0)
                while ((w / cell) + 6) * ((h / cell) + 6) > 120000:
                    cell *= 1.4
                gw = int(w / cell) + 6; gh = int(h / cell) + 6
                ox0 = minx - 3 * cell; oy0 = miny - 3 * cell
                cov = set()
                for (px, py) in pts:
                    c = int((px - ox0) / cell); r = int((py - oy0) / cell)
                    if 0 <= c < gw and 0 <= r < gh:
                        cov.add((c, r))
                if len(cov) < 8:
                    return hull, []
                # Morphological close, radius 2 (dilate x2 then erode x2,
                # 8-connected): fills the wider gaps left between rays at
                # grazing viewing angles that a radius-1 close leaves as a
                # comb, so the covered region is one solid blob.
                nb8 = ((-1, -1), (0, -1), (1, -1), (-1, 0), (1, 0), (-1, 1), (0, 1), (1, 1))
                def _dil(cs):
                    out = set(cs)
                    for (c, r) in cs:
                        for dc, dr in nb8:
                            out.add((c + dc, r + dr))
                    return out
                def _ero(cs):
                    return set(p for p in cs
                               if all((p[0] + dc, p[1] + dr) in cs for dc, dr in nb8))
                cov = _ero(_ero(_dil(_dil(cov))))
                cov = _largest_cc(cov)
                if len(cov) < 8:
                    return hull, []
                loops = _covered_boundary_loops(cov)
                if not loops:
                    return hull, []
                world = [[(ox0 + c * cell, oy0 + r * cell) for (c, r) in lp] for lp in loops]
                scored = sorted(((_signed_area(wl), wl) for wl in world), key=lambda t: -abs(t[0]))
                pos = [wl for a, wl in scored if a > 0]
                if not pos:
                    return hull, []
                traced_outer = pos[0]
                outer_a = abs(_signed_area(traced_outer))
                eps = cell * 1.6
                holes = []
                for a, wl in scored:
                    if a < 0 and abs(a) > outer_a * 0.02 and abs(a) > (cell * cell) * 6:
                        hh = _rdp_closed(wl, eps)
                        if len(hh) >= 3 and all(_pt_in_poly(v, hull) for v in hh):
                            holes.append(hh)
                hull_area = _polygon_area(hull)
                # Perfectly smooth hull when the blob basically fills it
                # (convex-ish deck); otherwise the deck is genuinely L/notch
                # shaped, so follow the simplified traced outline.
                if hull_area > 0 and outer_a >= hull_area * 0.86:
                    outer = hull
                else:
                    outer = _rdp_closed(traced_outer, eps)
                    if len(outer) < 3:
                        outer = hull
                return outer, holes

            level_polys = []   # (level_z, outer_ccw, [hole_cw, ...], area_m2)
            total_samples = 0
            for lvl in levels:
                z   = lvl["z"]
                pts = sample_pts_by_level.get(z, [])
                total_samples += len(pts)
                if len(pts) < 30:
                    # too few hits to be a real visible deck — e.g. a few rays
                    # that leaked through a void edge onto the storey below
                    continue
                outer, holes = _level_polygon(pts)
                if not outer or len(outer) < 3:
                    continue
                net = _polygon_area(outer) - sum(_polygon_area(hh) for hh in holes)
                area_m2 = max(0.0, net) * (_mpu_val ** 2)
                level_polys.append((z, outer, holes, area_m2))

            # Drop negligible fragments relative to the camera's main deck.
            if level_polys:
                _maxA = max(t[3] for t in level_polys)
                level_polys = [t for t in level_polys if t[3] >= max(2.0, _maxA * 0.08)]

            _cam_zone_area[path] = sum(t[3] for t in level_polys)
            print(f"[ZONE AREA] {cam_name}: metersPerUnit={_mpu_val} "
                  f"levels_with_coverage={len(level_polys)}/{len(levels)} "
                  f"(voids cut: {sum(len(t[2]) for t in level_polys)}) -> "
                  f"corrected={_cam_zone_area[path]:.3f} m^2 "
                  f"({_cam_zone_area[path]*10.7639:.1f} ft^2) | "
                  f"get_floor_info_unit_guess={get_floor_info()[5]}")

            # A ~0 total area means this camera currently sees no usable
            # floor/deck at all (pointed at a wall/ceiling, Range too short,
            # fully occluded, ...) — delete whatever zone mesh(es) it had
            # from a previous generation instead of leaving them next to a
            # "0.0 m² / 0 ft²" readout, which would look like a live zone
            # with no coverage rather than no zone at all.
            if _cam_zone_area[path] <= 1e-6:
                print(f"[ZONE AREA] {cam_name}: area ~0 -> removing existing zone")
                _remove_zone(path)
                _schedule_rebuild()
                set_status(f"Zone removed (no visible floor): {cam_name}")
                return

            _tt3 = time.perf_counter()
            # A boundary-refinement pass was tried here (extending each hull
            # vertex outward to close the corner gap left by angle-uniform
            # sampling) and then reverted — it measured "outward" from the
            # hull's own centroid rather than constraining the extension to
            # the camera's actual forward view cone, so it could occasionally
            # grow the zone into floor space behind the camera. Back to the
            # plain hull from the collinearity fix above until a version of
            # that refinement gets the direction constraint right.

            # Clear every previous zone-level mesh up front — level count/
            # naming can differ between regenerations (mezzanine geometry
            # added or removed since last time), so start from a clean slate
            # rather than only ever touching a single fixed "FloorZone" child.
            zr_prim = s.GetPrimAtPath(zone_root)
            if zr_prim and zr_prim.IsValid():
                for child in list(zr_prim.GetChildren()):
                    if child.GetName().startswith("FloorZone"):
                        s.RemovePrim(child.GetPath())

            fc = Gf.Vec3f(min(color[0]*1.3, 1.0),
                          min(color[1]*1.3, 1.0),
                          min(color[2]*1.3, 1.0))
            floor_mat = _make_zone_material(s, zone_root + "/FloorMat", fc, 0.6)

            # Build each level's mesh: ear-clip the (smooth) outer ring minus
            # any void hole loop(s). No void -> a plain ear-clip of the hull.
            total_hull_pts = 0
            for idx, (z, outer, holes, _area) in enumerate(level_polys):
                draw_z = z + 0.01   # 1 cm above this level's surface to avoid Z-fighting
                if holes:
                    verts2d, tris = _tri_with_holes(outer, holes)
                else:
                    verts2d, tris = list(outer), _earcut(list(outer))
                if not tris:
                    n_o = len(outer)
                    cx = sum(p[0] for p in outer) / n_o
                    cy = sum(p[1] for p in outer) / n_o
                    verts2d = [(cx, cy)] + list(outer)
                    tris = [(0, 1 + i, 1 + (i + 1) % n_o) for i in range(n_o)]
                verts = [Gf.Vec3f(p[0], p[1], draw_z) for p in verts2d]
                tri_counts = [3] * len(tris)
                tri_indices = []
                for (ta, tb, tc) in tris:
                    tri_indices.extend([ta, tb, tc])
                total_hull_pts += len(verts)

                floor_path = zone_root + "/FloorZone" if idx == 0 else f"{zone_root}/FloorZone_{idx+1}"
                floor_mesh = UsdGeom.Mesh.Define(s, floor_path)
                n = len(verts)
                floor_mesh.GetPointsAttr().Set(Vt.Vec3fArray(verts))
                floor_mesh.GetFaceVertexCountsAttr().Set(Vt.IntArray(tri_counts))
                floor_mesh.GetFaceVertexIndicesAttr().Set(Vt.IntArray(tri_indices))
                floor_mesh.GetSubdivisionSchemeAttr().Set("none")
                floor_mesh.GetDoubleSidedAttr().Set(True)
                floor_mesh.GetNormalsAttr().Set(Vt.Vec3fArray([Gf.Vec3f(0,0,1)] * n))
                floor_mesh.SetNormalsInterpolation(UsdGeom.Tokens.vertex)
                floor_mesh.GetDisplayColorAttr().Set(Vt.Vec3fArray([fc] * n))
                floor_mesh.GetDisplayOpacityAttr().Set(Vt.FloatArray([0.6] * n))
                UsdShade.MaterialBindingAPI(s.GetPrimAtPath(floor_path)).Bind(floor_mat)
            _tt4 = time.perf_counter()

            print(f"[ZONE TIMING] {cam_name}: "
                  f"tri_cache={_tt1-_tt0:.2f}s ({_active_tris} tris"
                  f"{f' -> {_pf_kept} near' if _pf_ov is not None else ''}) | "
                  f"raycast_grid={_tt2-_tt1:.2f}s ({(cols+1)*(rows+1)} rays) | "
                  f"hull={_tt3-_tt2:.2f}s ({total_samples} pts -> {total_hull_pts} poly pts across "
                  f"{len(level_polys)} level(s)) | "
                  f"mesh/material={_tt4-_tt3:.2f}s | TOTAL={_tt4-_tt0:.2f}s | "
                  f"area={_cam_zone_area.get(path, 0.0):.1f}m² ({_cam_zone_area.get(path, 0.0)*10.7639:.0f}ft²)")

            _cam_zones[path]    = True
            _zone_visible[path] = True
            # Hide/Show (_toggle_zone) authors visibility directly on the
            # zone_root Scope prim, which regeneration above never touches
            # (only its FloorZone child mesh gets removed/rebuilt) — so a
            # zone hidden before a regenerate stayed invisible in the
            # viewport even though _zone_visible flipped back to True here,
            # until the Hide/Show button was clicked again to touch the
            # real prim. Force the prim visible too so "Generate"/"Generate
            # All Zones" always shows the result regardless of prior state.
            try:
                zr_prim = s.GetPrimAtPath(zone_root)
                if zr_prim and zr_prim.IsValid():
                    UsdGeom.Imageable(zr_prim).MakeVisible()
            except Exception as _e:
                print(f"[ZONE] force-visible on generate: {_e}")
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
            _cam_zone_area.pop(path, None)

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
            # Run off the UI thread, yielding a frame between cameras, so
            # Kit keeps pumping and Windows doesn't paint "Not Responding"
            # while a multi-camera batch generates.
            asyncio.ensure_future(_generate_all_zones_async())

        async def _generate_all_zones_async():
            # Scan the scene's triangles ONCE for the whole batch instead of
            # once per camera — with several cameras this was the difference
            # between one full-stage scan and N of them back to back, which
            # is where most of "Generate All Zones" taking a long time came
            # from (see _generate_zone's _skip_cache_refresh).
            _cache_scene_triangles(force=True)
            _app = omni.kit.app.get_app()
            for path in list(_cam_list):
                _generate_zone(path, _skip_cache_refresh=True)
                await _app.next_update_async()
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

        _range_gen_token = [0]

        def _on_range_change(model):
            _cam_range[0] = model.get_value_as_float()
            # Regenerate all existing zones with the new range, off the UI
            # thread and yielding between cameras. The slider fires a burst
            # of callbacks while dragging, so bump a token and let only the
            # latest scheduled run proceed (and coalesce one frame first).
            _range_gen_token[0] += 1
            asyncio.ensure_future(_on_range_change_async(_range_gen_token[0]))

        async def _on_range_change_async(_tok):
            _app = omni.kit.app.get_app()
            await _app.next_update_async()
            if _tok != _range_gen_token[0]:
                return
            # one triangle-cache scan for the whole batch, same reasoning as
            # _generate_all_zones.
            _cache_scene_triangles(force=True)
            for path in list(_cam_list):
                if _tok != _range_gen_token[0]:
                    return
                if _cam_zones.get(path):
                    was_hidden = not _zone_visible.get(path, True)
                    _generate_zone(path, _skip_cache_refresh=True)
                    if was_hidden:
                        _toggle_zone(path)
                    await _app.next_update_async()

        def on_combo_change(model, item):
            try:
                idx=cam_combo.model.get_item_value_model().get_value_as_int()
                ps=CAM_PRESETS[PRESET_KEYS[idx]]
                lbl_specs.text=(f"Res {ps['res_w']}x{ps['res_h']}  "
                                f"Sensor {ps['sensor']}  "
                                f"Focal {ps['focal_mm']}mm  "
                                f"H {ps['h_ap']}  V {ps['v_ap']}")
            except Exception as e:
                print(f"[COMBO] {e}")

        # ── RAIN SYSTEM ──
        RAIN_ROOT    = f"{STAGE_ROOT}/RainSystem"
        _rain_baked  = [False]
        _rain_active = [False]
        FPS_RAIN     = 24.0
        TOTAL_SEC    = 120.0
        TOTAL_FRAMES = int(TOTAL_SEC * FPS_RAIN)
        MAX_DROPS = 3000
        FALL_SLOW = 8
        FALL_FAST = 3
        F_D = int(20  * FPS_RAIN)
        F_B = int(50  * FPS_RAIN)
        F_H = int(80  * FPS_RAIN)
        F_S = int(100 * FPS_RAIN)
        F_E = int(120 * FPS_RAIN)

        def _rain_intensity(frame):
            if frame <= F_D:
                t = frame / F_D;             return 0.05 + t * 0.10
            elif frame <= F_B:
                t = (frame-F_D)/(F_B-F_D);  return 0.15 + t * 0.85
            elif frame <= F_H:               return 1.0
            elif frame <= F_S:
                t = (frame-F_H)/(F_S-F_H);  return 1.0 - t * 0.90
            else:
                t = (frame-F_S)/(F_E-F_S);  return max(0.0, 0.10 - t * 0.10)

        def _rain_make_mat(path, r, g, b, opacity, roughness):
            m  = UsdShade.Material.Define(stage, path)
            sh = UsdShade.Shader.Define(stage, path + "/S")
            try:
                sh.CreateIdAttr("UsdPreviewSurface")
                sh.CreateInput("diffuseColor",  Sdf.ValueTypeNames.Color3f).Set(Gf.Vec3f(r,g,b))
                sh.CreateInput("emissiveColor", Sdf.ValueTypeNames.Color3f).Set(Gf.Vec3f(r*0.3,g*0.3,b*0.4))
                sh.CreateInput("opacity",       Sdf.ValueTypeNames.Float).Set(float(opacity))
                sh.CreateInput("roughness",     Sdf.ValueTypeNames.Float).Set(float(roughness))
                sh.CreateInput("metallic",      Sdf.ValueTypeNames.Float).Set(0.0)
                sh.CreateInput("ior",           Sdf.ValueTypeNames.Float).Set(1.33)
                m.CreateSurfaceOutput().ConnectToSource(sh.ConnectableAPI(), "surface")
                return m
            except:
                return None

        def _get_scene_bounds():
            _rs()
            try:
                bc   = UsdGeom.BBoxCache(0, [UsdGeom.Tokens.default_], True)
                bbox = bc.ComputeWorldBound(stage.GetPseudoRoot())
                rng  = bbox.GetRange()
                mn   = rng.GetMin(); mx = rng.GetMax()
                return (float((mn[0]+mx[0])/2), float((mn[1]+mx[1])/2),
                        float(mn[2]),
                        float(mx[0]-mn[0]), float(mx[1]-mn[1]), float(mx[2]-mn[2]))
            except:
                return 0.0, 0.0, 0.0, 50.0, 50.0, 10.0

        def bake_rain():
            _rs()
            set_status("Baking rain... please wait (~30s)")
            print("[RAIN] Starting bake...")
            if HAS_TIMELINE:
                try:
                    tl = omni.timeline.get_timeline_interface()
                    if tl.is_playing(): tl.stop()
                except: pass
            if stage.GetPrimAtPath(RAIN_ROOT).IsValid():
                stage.RemovePrim(RAIN_ROOT)
            UsdGeom.Xform.Define(stage, RAIN_ROOT)
            CX,CY,GZ,SW,SD,SH = _get_scene_bounds()
            SX = max(SW, SD) * 1.2 / 2.0
            SY = max(SW, SD) * 1.2 / 2.0
            RAIN_TOP   = GZ + SH * 1.2
            HIDE_Z     = GZ - SH * 3.0
            fall_range = RAIN_TOP - GZ
            ref        = max(SW, SD)
            auto_drop_h = max(0.05, ref * 0.005)
            auto_drop_r = max(0.002, ref * 0.0004)
            dm = _rain_make_mat(RAIN_ROOT+"/DM", 0.80, 0.88, 1.00, 0.35, 0.02)
            ps         = RAIN_ROOT + "/Protos"
            proto_path = ps + "/Drop"
            UsdGeom.Scope.Define(stage, ps)
            cap = UsdGeom.Capsule.Define(stage, proto_path)
            cap.CreateHeightAttr().Set(auto_drop_h)
            cap.CreateRadiusAttr().Set(auto_drop_r)
            cap.CreateAxisAttr().Set("Z")
            cap.CreateDisplayColorAttr().Set(Vt.Vec3fArray([Gf.Vec3f(0.80,0.88,1.0)]))
            if dm:
                try: UsdShade.MaterialBindingAPI(stage.GetPrimAtPath(proto_path)).Bind(dm)
                except: pass
            tilt_rad = math.radians(4.0)
            half     = tilt_rad / 2.0
            tilt_q   = Gf.Quath(math.cos(half), math.sin(half), 0.0, 0.0)
            BATCH_SIZE  = 250
            NUM_BATCHES = MAX_DROPS // BATCH_SIZE
            tilt_arr    = Vt.QuathArray([tilt_q] * BATCH_SIZE)
            instancers = []
            for b in range(NUM_BATCHES):
                br   = RAIN_ROOT + "/B" + str(b)
                UsdGeom.Xform.Define(stage, br)
                inst = UsdGeom.PointInstancer.Define(stage, br+"/D")
                inst.CreatePrototypesRel().SetTargets([Sdf.Path(proto_path)])
                inst.GetProtoIndicesAttr().Set(Vt.IntArray([0]*BATCH_SIZE))
                inst.GetOrientationsAttr().Set(tilt_arr)
                instancers.append(inst)
            random.seed(42)
            all_xs  = [CX + random.uniform(-SX,SX) for _ in range(MAX_DROPS)]
            all_ys  = [CY + random.uniform(-SY,SY) for _ in range(MAX_DROPS)]
            all_off = [random.randint(0, FALL_SLOW-1)  for _ in range(MAX_DROPS)]
            for frame in range(0, TOTAL_FRAMES+1, 2):
                intensity  = _rain_intensity(frame)
                active     = max(0, int(MAX_DROPS * intensity))
                fall_speed = max(FALL_FAST, int(FALL_SLOW - intensity*(FALL_SLOW-FALL_FAST)))
                for b in range(NUM_BATCHES):
                    start = b * BATCH_SIZE
                    drops = Vt.Vec3fArray(BATCH_SIZE)
                    for li in range(BATCH_SIZE):
                        gi = start + li
                        if gi >= active or intensity == 0.0:
                            drops[li] = Gf.Vec3f(float(all_xs[gi]), float(all_ys[gi]), HIDE_Z)
                            continue
                        cf = (frame + all_off[gi]) % fall_speed
                        t  = cf / fall_speed
                        z  = RAIN_TOP - t * fall_range
                        if z < GZ + auto_drop_h * 2:
                            drops[li] = Gf.Vec3f(float(all_xs[gi]), float(all_ys[gi]), HIDE_Z)
                        else:
                            drops[li] = Gf.Vec3f(float(all_xs[gi]), float(all_ys[gi]), float(z))
                    instancers[b].GetPositionsAttr().Set(drops, frame)
                if frame % 480 == 0:
                    pct = round(frame/TOTAL_FRAMES*100)
                    set_status(f"Baking rain... {pct}%")
            try:
                if HAS_TIMELINE:
                    tl = omni.timeline.get_timeline_interface()
                    tl.set_start_time(0)
                    tl.set_end_time(TOTAL_FRAMES / FPS_RAIN)
                    tl.set_time_codes_per_second(FPS_RAIN)
                stage.SetStartTimeCode(0)
                stage.SetEndTimeCode(float(TOTAL_FRAMES))
                stage.SetTimeCodesPerSecond(FPS_RAIN)
            except: pass
            _rain_baked[0] = True
            btn_rain_bake.visible  = False
            btn_rain_start.visible = True
            btn_rain_stop.visible  = False
            set_status("Rain ready! Click Start Rain to play.")

        def start_rain():
            if not _rain_baked[0]:
                set_status("Bake rain first!"); return
            played = False
            try:
                import omni.timeline
                tl = omni.timeline.get_timeline_interface()
                tl.set_current_time(0.0)
                tl.play()
                played = True
            except: pass
            _rain_active[0] = True
            btn_rain_start.visible = False
            btn_rain_stop.visible  = True
            set_status("Rain playing!" if played else "Press SPACE to start rain")

        def stop_rain():
            try:
                import omni.timeline
                tl = omni.timeline.get_timeline_interface()
                tl.stop(); tl.set_current_time(0.0)
            except: pass
            _rain_active[0] = False
            btn_rain_start.visible = True
            btn_rain_stop.visible  = False
            set_status("Rain stopped.")

        def clear_rain():
            _rs()
            stop_rain()
            if stage.GetPrimAtPath(RAIN_ROOT).IsValid():
                stage.RemovePrim(RAIN_ROOT)
            _rain_baked[0]  = False
            _rain_active[0] = False
            btn_rain_bake.visible  = True
            btn_rain_start.visible = False
            btn_rain_stop.visible  = False
            set_status("Rain cleared.")

        # ── Floor height state ──
        _selected_height = [10]
        _ht_btns         = {}

        def _refresh_height_buttons():
            for ft, btn in _ht_btns.items():
                active = (ft == _selected_height[0])
                btn.style = {
                    "background_color": 0xFF2A5A2A if active else 0xFF222222,
                    "color":            0xFF88FF88 if active else 0xFFAAAAAA,
                    "font_size": 12, "border_radius": 4,
                }

        def _apply_custom_height():
            try:
                h = float(custom_height_field.model.get_value_as_float())
                if h <= 0: set_status("Height must be > 0"); return
                _selected_height[0] = h
                for ft, btn in _ht_btns.items():
                    btn.style = {"background_color":0xFF222222,"color":0xFFAAAAAA,
                                 "font_size":12,"border_radius":4}
                _reposition_lights()
                set_status(f"Custom height {h:.1f} ft")
            except Exception as e:
                set_status(f"Invalid: {e}")

        def _reposition_lights():
            _rs()
            cx,cy,fz,w,d,unit,tf = get_floor_info()
            units_per_foot  = tf / 10.0
            new_z_per_floor = _selected_height[0] * units_per_foot
            for i in range(NUM_LIGHTS):
                prim = stage.GetPrimAtPath(lpath(i))
                if prim and prim.IsValid():
                    n    = i + 1
                    new_z = fz + new_z_per_floor * n
                    xf   = UsdGeom.XformCommonAPI(prim)
                    t    = xf.GetXformVectors(Usd.TimeCode.Default())[0]
                    xf.SetTranslate(Gf.Vec3d(t[0], t[1], new_z))

        # ── UI ──
        try:
            for w in ui.Workspace.get_windows():
                # Exact match only — see on_shutdown for why "in" was wrong here.
                if w.title == "Floor Plan Tool": w.destroy()
        except: pass

        cx,cy,fz,fw,fd,unit,ten_feet=get_floor_info()
        dw=round(fw*1.2,4); dd=round(fd*1.2,4)
        lights_now=any_lights()

        try:
            import omni.appwindow
            _aw   = omni.appwindow.get_default_app_window()
            scr_w = _aw.get_width()
            scr_h = _aw.get_height()
        except Exception:
            scr_w, scr_h = 1920, 1080

        tool_h = int(scr_h * 0.80)
        tool_w = 500
        tool_x = scr_w - tool_w - 4
        tool_y = 30

        win = ui.Window("Floor Plan Tool", width=tool_w, height=tool_h,
                        position_x=tool_x, position_y=tool_y)
        win.set_visibility_changed_fn(self._win_visibility_guard)

        # Dock as tab next to Env Day Cycle Control (deferred so it works after layout)
        win.deferred_dock_in("Env Day Cycle Control", ui.DockPolicy.CURRENT_WINDOW_IS_ACTIVE)

        with win.frame:
            with ui.VStack(spacing=0):
                with ui.ZStack(height=26):
                    ui.Rectangle(style={"background_color":0xFF080808})
                    with ui.HStack(style={"margin_left":8,"margin_right":6}):
                        ui.Label("Floor Plan Tool", style={"font_size":13,"color":0xFFDDDDDD})
                        ui.Spacer()
                        ui.Label(f"{fw:.2f}x{fd:.2f}{unit}", style={"font_size":9,"color":0xFF444444})

                with ui.HStack(height=30,spacing=0):
                    btn_tab_light=ui.Button("  Lights  ",
                        style={"background_color":0xFF2D5A2D,"color":0xFFFFFFFF,"font_size":12,"border_radius":0})
                    btn_tab_cam=ui.Button("  Camera  ",
                        style={"background_color":0xFF111111,"color":0xFF666666,"font_size":12,"border_radius":0})
                    btn_tab_rain=ui.Button("  Rain  ",
                        style={"background_color":0xFF111111,"color":0xFF666666,"font_size":12,"border_radius":0})

                # This app has no File menu (viewer .kit config), so these
                # are the only way to persist/reload the live stage —
                # visible regardless of which tab is active since they're
                # outside the tab content. Open Scene replaces whatever's in
                # the CURRENT stage with the most recently saved file — the
                # normal use is a fresh, empty-stage instance of this app.
                with ui.HStack(height=24, spacing=0):
                    ui.Button("💾 Save Scene", clicked_fn=_save_scene,
                              style={"background_color":0xFF1E3A6A,"color":0xFFFFFFFF,
                                     "font_size":11,"border_radius":0})
                    ui.Button("📂 Open Scene", clicked_fn=_open_scene,
                              style={"background_color":0xFF2A2A2A,"color":0xFFDDDDDD,
                                     "font_size":11,"border_radius":0})

                ui.Line(style={"color":0xFF1E1E1E,"border_width":1})
                _fill_h = tool_h - 26 - 30 - 24 - 4 - 28

                # ── LIGHTS TAB ──
                sf_lights = ui.ScrollingFrame(height=_fill_h, visible=True,
                    horizontal_scrollbar_policy=ui.ScrollBarPolicy.SCROLLBAR_ALWAYS_OFF,
                    vertical_scrollbar_policy=ui.ScrollBarPolicy.SCROLLBAR_AS_NEEDED)
                with sf_lights:
                    with ui.VStack(spacing=8, style={"margin":10}):
                        # ── Individual light: drag-to-place ─────────────────────
                        def _make_light_drag_fn():
                            def _drag():
                                self._drag_active = True
                                self._drag_kind    = "light"
                                FloorPlanToolExtension._drag_kind = "light"
                                print("[FPT] Drag started — Light")
                                try:
                                    cx_, cy_, fz_, w_, d_, _, tf_ = get_floor_info()
                                    self._drag_floor_z    = fz_
                                    self._drag_scene_cx   = cx_
                                    self._drag_scene_cy   = cy_
                                    self._drag_scene_size = max(w_, d_, 1.0)
                                    self._drag_ghost_h    = tf_ / 5.0
                                    self._drag_aabbs      = self._cache_scene_aabbs()
                                except Exception as _e:
                                    print(f"[FPT] light drag init: {_e}")
                                    self._drag_aabbs = []
                                return FloorPlanToolExtension._DRAG_TOKEN
                            return _drag
                        with ui.ZStack(height=54, style={"margin":0}):
                            ui.Rectangle(style={"background_color":0xFF2E2A0D,
                                                 "border_radius":7,"border_width":1,
                                                 "border_color":0xFF80702A,"margin":0})
                            with ui.VStack(spacing=2, style={"margin":8}):
                                ui.Label("[ light ]", height=14,
                                         style={"font_size":9,"color":0xFFAA9A4A,
                                                "alignment":ui.Alignment.CENTER,"margin":0})
                                ui.Label("Light", height=22,
                                         style={"font_size":15,"color":0xFFFFEEBB,
                                                "alignment":ui.Alignment.CENTER,"margin":0})
                            _light_overlay = ui.Rectangle(style={"background_color":0x00000000,"margin":0})
                            _light_overlay.set_drag_fn(_make_light_drag_fn())
                        ui.Label("↳  Drag to place exactly, or click Add Light for the floor center",
                                 height=16, style={"font_size":9,"color":0xFF7A6A3A,"margin":0})
                        ui.Button("Add Light", height=36, clicked_fn=add_individual_light,
                            style={"background_color":0xFF2D6A2D,"color":0xFFFFFFFF,"font_size":13,"border_radius":6,"margin":0})

                        ui.Line(height=8, style={"color":0xFF2A2A2A,"margin":0})
                        ui.Label("Individual Lights", height=18,
                                 style={"font_size":11,"color":0xFF888888,"margin":0})
                        ind_light_list_frame = ui.Frame(height=100, style={"margin":0})
                        with ind_light_list_frame:
                            ui.Label("No individual lights yet.",
                                     style={"font_size":10,"color":0xFF444444,"margin":0})
                        ui.Label("Selected light — its own settings (independent of the sliders below)",
                                 height=14, word_wrap=True,
                                 style={"font_size":9,"color":0xFF6A6A6A,"margin":0})
                        _ild_row = lambda: ui.HStack(height=24, spacing=6, style={"margin":0})
                        _ild_lbl = lambda s: ui.Label(s, width=88,
                                     style={"font_size":10,"color":0xFFAAAAAA,"margin":0})
                        with _ild_row():
                            _ild_lbl("Height (ft)")
                            field_ind_light_height = ui.FloatDrag(min=0.0, max=100.0, step=0.1)
                            field_ind_light_height.model.add_value_changed_fn(_on_ind_light_height_changed)
                        with _ild_row():
                            _ild_lbl("Intensity (lx)")
                            field_ind_light_intensity = ui.FloatDrag(min=0.0, max=500000.0, step=1000.0)
                            field_ind_light_intensity.model.add_value_changed_fn(
                                lambda m: _on_ind_light_prop_changed("intensity", m))
                        with _ild_row():
                            _ild_lbl("Temp (K)")
                            field_ind_light_temp = ui.FloatDrag(min=1000.0, max=12000.0, step=50.0)
                            field_ind_light_temp.model.add_value_changed_fn(
                                lambda m: _on_ind_light_prop_changed("colorTemperature", m))
                        with _ild_row():
                            _ild_lbl(f"Width ({unit})")
                            field_ind_light_width = ui.FloatDrag(min=0.01, max=max(fw*3, 20), step=0.1)
                            field_ind_light_width.model.add_value_changed_fn(
                                lambda m: _on_ind_light_prop_changed("width", m))
                        with _ild_row():
                            _ild_lbl(f"Depth ({unit})")
                            field_ind_light_depth = ui.FloatDrag(min=0.01, max=max(fd*3, 20), step=0.1)
                            field_ind_light_depth.model.add_value_changed_fn(
                                lambda m: _on_ind_light_prop_changed("height", m))

                        ui.Line(height=8, style={"color":0xFF2A2A2A,"margin":0})
                        ui.Label("Quick Fill", style={"font_size":11,"color":0xFF888888,"margin":0})
                        ui.Label("Places 20 lights in a grid at once,\n"
                                 "separate from individually-dragged lights above.",
                                 word_wrap=True, height=26,
                                 style={"font_size":9,"color":0xFF555555,"margin":0})
                        btn_add_light=ui.Button("Quick Fill 20 Lights", height=42, clicked_fn=place_lights,
                            style={"background_color":0xFF2D6A2D,"color":0xFFFFFFFF,"font_size":13,"border_radius":6,"margin":0})
                        btn_add_light.visible = not lights_now
                        btn_rem_light=ui.Button("Remove Quick-Fill Lights", height=42, clicked_fn=remove_lights,
                            style={"background_color":0xFF6A2D2D,"color":0xFFFFFFFF,"font_size":13,"border_radius":6,"margin":0})
                        btn_rem_light.visible = lights_now
                        ui.Line(height=8, style={"color":0xFF2A2A2A,"margin":0})
                        ui.Label("Floor Height", style={"font_size":12,"color":0xFF888888,"margin":0})
                        with ui.HStack(spacing=4, height=34, style={"margin":0}):
                            for ft in [9, 10, 11, 12]:
                                def _make_ht_fn(h=ft):
                                    def _fn():
                                        _selected_height[0] = h
                                        try: custom_height_field.model.set_value(float(h))
                                        except: pass
                                        _refresh_height_buttons(); _reposition_lights()
                                        set_status(f"Height set to {h} ft")
                                    return _fn
                                _ht_btns[ft] = ui.Button(f"{ft} ft", height=34, clicked_fn=_make_ht_fn(ft),
                                    style={"background_color":0xFF2A5A2A if ft==_selected_height[0] else 0xFF222222,
                                           "color":0xFF88FF88 if ft==_selected_height[0] else 0xFFAAAAAA,
                                           "font_size":12,"border_radius":4,"margin":0})
                        with ui.HStack(height=28, spacing=6, style={"margin":0}):
                            ui.Label("Custom:", width=58, style={"font_size":11,"color":0xFFAAAAAA,"margin":0})
                            custom_height_field = ui.FloatField(height=26, style={"margin":0})
                            custom_height_field.model.set_value(float(_selected_height[0]))
                            ui.Button("Apply", width=56, height=26, clicked_fn=lambda: _apply_custom_height(),
                                      style={"background_color":0xFF1A3A5A,"color":0xFFFFFFFF,"font_size":11,"border_radius":4,"margin":0})
                        ui.Line(height=8, style={"color":0xFF2A2A2A,"margin":0})
                        ui.Label("Light Properties", style={"font_size":12,"color":0xFF888888,"margin":0})
                        for lbl_,attr_,mn_,mx__,def_,fl_ in [
                            ("Intensity (lx)","intensity",1000,500000,50000,False),
                            (f"Width ({unit})","lwidth",0.1,max(fw*3,20),dw,True),
                            (f"Depth ({unit})","ldepth",0.1,max(fd*3,20),dd,True),
                            ("Temp (K)","temp",1500,9000,4500,False),
                        ]:
                            with ui.HStack(height=26,spacing=6, style={"margin":0}):
                                ui.Label(lbl_,width=114, style={"font_size":11,"color":0xFFCCCCCC,"margin":0})
                                sl=(ui.FloatSlider(min=mn_,max=mx__) if fl_ else ui.IntSlider(min=int(mn_),max=int(mx__)))
                                sl.style = {"margin":0}
                                sl.model.set_value(float(def_) if fl_ else int(def_))
                                sl.model.add_value_changed_fn(live_light)
                                if attr_=="intensity": sl_intensity=sl
                                elif attr_=="lwidth":  sl_lwidth=sl
                                elif attr_=="ldepth":  sl_ldepth=sl
                                elif attr_=="temp":    sl_temp=sl
                        ui.Line(height=8, style={"color":0xFF2A2A2A,"margin":0})
                        ui.Label("Presets",style={"font_size":12,"color":0xFF888888,"margin":0})
                        with ui.HStack(spacing=6,height=36, style={"margin":0}):
                            def warm(_=None): sl_temp.model.set_value(3000); sl_intensity.model.set_value(30000)
                            def cool(_=None): sl_temp.model.set_value(6500); sl_intensity.model.set_value(60000)
                            def bright(_=None): sl_temp.model.set_value(5500); sl_intensity.model.set_value(200000)
                            ui.Button("Warm",clicked_fn=warm,height=36,style={"background_color":0xFF3A2A10,"color":0xFFFFCC88,"border_radius":5,"margin":0})
                            ui.Button("Cool",clicked_fn=cool,height=36,style={"background_color":0xFF102030,"color":0xFF88CCFF,"border_radius":5,"margin":0})
                            ui.Button("Bright",clicked_fn=bright,height=36,style={"background_color":0xFF2A2A10,"color":0xFFFFFFAA,"border_radius":5,"margin":0})
                        ui.Spacer(height=10)

                # ── CAMERA TAB ──
                sf_camera = ui.ScrollingFrame(height=_fill_h, visible=False,
                    horizontal_scrollbar_policy=ui.ScrollBarPolicy.SCROLLBAR_ALWAYS_OFF,
                    vertical_scrollbar_policy=ui.ScrollBarPolicy.SCROLLBAR_AS_NEEDED)
                with sf_camera:
                    with ui.VStack(spacing=10, style={"margin":1}):
                        with ui.HStack(height=22,spacing=6):
                            ui.Label("Type:",width=44, style={"font_size":11,"color":0xFFAAAAAA})
                            cam_combo=ui.ComboBox(0,*PRESET_KEYS)
                        cam_combo.model.add_item_changed_fn(on_combo_change)
                        with ui.ZStack(height=22):
                            ui.Rectangle(style={"background_color":0xFF0C0C0C,"border_radius":4})
                            lbl_specs=ui.Label("Res 1920x1080  Sensor 1/2.8\"  Focal 2.8mm  H 5.37  V 3.02",
                                word_wrap=False, style={"font_size":12,"color":0xFF4477AA,"margin":8})
                        ui.Label("Drag to Viewport  |  Place Camera", height=16,
                                 style={"font_size":12,"color":0xFF667799})
                        with ui.HStack(height=60, spacing=6):
                            def _make_drag_fn(preset_idx):
                                def _drag():
                                    self._drag_active     = True
                                    self._drag_kind        = "camera"
                                    FloorPlanToolExtension._drag_kind = "camera"
                                    self._drag_preset_idx = preset_idx
                                    FloorPlanToolExtension._drag_preset_idx = preset_idx
                                    lbl = PRESET_KEYS[preset_idx].split()[0]
                                    print(f"[FPT] Drag started — {lbl}")
                                    # Cache scene geometry for per-frame AABB raycasting
                                    try:
                                        cx_, cy_, fz_, w_, d_, _, tf_ = get_floor_info()
                                        self._drag_floor_z    = fz_
                                        self._drag_scene_cx   = cx_
                                        self._drag_scene_cy   = cy_
                                        self._drag_scene_size = max(w_, d_, 1.0)
                                        self._drag_ghost_h    = tf_ / 5.0
                                        self._drag_aabbs      = self._cache_scene_aabbs()
                                        print(f"[FPT] Cached {len(self._drag_aabbs)} AABBs  floor_z={fz_:.3f}")
                                    except Exception as _e:
                                        print(f"[FPT] drag init: {_e}")
                                        self._drag_aabbs = []
                                    return FloorPlanToolExtension._DRAG_TOKEN
                                return _drag
                            for _pi, _pkey in enumerate(PRESET_KEYS):
                                _plabel = CAM_PRESETS[_pkey]["label"]  # "2MP" / "4MP" / "8MP"
                                _db = ui.Button(
                                    f" {_plabel}",
                                    height=60,
                                    style={
                                        "background_color": 0xFF0A1E32,
                                        "color":            0xFF88BBFF,
                                        "border_color":     0xFF224488,
                                        "border_width":     1.5,
                                        "border_radius":    6,
                                        "font_size":        13,
                                    },
                                )
                                _db.set_drag_fn(_make_drag_fn(_pi))

                        ui.Button("Add Camera",height=36,clicked_fn=add_camera,
                                  style={"background_color":0xFF1E3A6A,"color":0xFFFFFFFF,"font_size":12,"border_radius":6})
                        ui.Line(height=8, style={"color":0xFF2A2A2A})
                        with ui.HStack(height=22):
                            ui.Label("Camera List", style={"font_size":11,"color":0xFFAAAAAA})
                            ui.Spacer()
                            btn_perspective[0] = ui.Button("Perspective", width=80, height=22,
                                clicked_fn=lambda: switch_to_cam("/OmniverseKit_Persp"),
                                style=_perspective_btn_style(_active_cam[0] in (None, "/OmniverseKit_Persp")))
                        with ui.ZStack(height=22):
                            ui.Rectangle(style={"background_color":0xFF0C0C0C,"border_radius":4})
                            cam_filter_field = ui.StringField(height=22, style={"font_size":11})
                        cam_filter_field.model.add_value_changed_fn(_on_cam_filter_changed)
                        # Bounded-height inner scroll — with 70-80 cameras the
                        # list no longer pushes Zone Controls/joysticks below
                        # further and further down the outer panel; it scrolls
                        # on its own within this fixed ~240px window instead.
                        cam_list_scroll = ui.ScrollingFrame(height=240,
                            horizontal_scrollbar_policy=ui.ScrollBarPolicy.SCROLLBAR_ALWAYS_OFF,
                            vertical_scrollbar_policy=ui.ScrollBarPolicy.SCROLLBAR_AS_NEEDED)
                        with cam_list_scroll:
                            cam_list_frame=ui.Frame()
                        with cam_list_frame:
                            ui.Label("No cameras added yet.", style={"font_size":10,"color":0xFF444444})
                        cam_detail_frame = ui.Frame()
                        with cam_detail_frame:
                            ui.Label("Select a camera above to see its zone details.",
                                     height=18, style={"font_size":9,"color":0xFF444444})
                        ui.Line(style={"color":0xFF2A2A2A}, height=8)
                        ui.Label("Zone Controls",height=18,
                                 style={"font_size":12,"color":0xFF888888})
                        ui.Label("Zone = translucent frustum box (20% opacity) "
                                 "showing camera coverage area.",
                                 word_wrap=True,height=28,
                                 style={"font_size":9,"color":0xFF445555})
                        lbl_total_area[0] = ui.Label("Total Floor Area: —", height=16,
                                 style={"font_size":10,"color":0xFF77CCFF})
                        ui.Button("Generate All Zones", height=34,
                                  clicked_fn=_generate_all_zones,
                                  style={"background_color":0xFF1E3A6A,"color":0xFFFFFFFF,
                                         "font_size":12,"border_radius":5})
                        with ui.HStack(height=30, spacing=6):
                            ui.Button("Show All", height=30,
                                      clicked_fn=_show_all_zones,
                                      style={"background_color":0xFF103A10,"color":0xFF88FF88,
                                             "font_size":11,"border_radius":5})
                            ui.Button("Hide All", height=30,
                                      clicked_fn=_hide_all_zones,
                                      style={"background_color":0xFF3A2A10,"color":0xFFFFAA44,
                                             "font_size":11,"border_radius":5})
                        ui.Line(style={"color":0xFF2A2A2A})
                        ui.Label("Zone Range  (drag to resize all zones)",height=18,
                                 style={"font_size":12,"color":0xFF888888})
                        with ui.HStack(height=26,spacing=6):
                            ui.Label("Range",width=46,
                                     style={"font_size":11,"color":0xFFAAAAAA})
                            sl_cam_range=ui.FloatSlider(min=1.0,max=200.0)
                            sl_cam_range.model.set_value(_cam_range[0])
                            sl_cam_range.model.add_value_changed_fn(_on_range_change)
                        ui.Line(style={"color":0xFF2A2A2A}, height=8)
                        ui.Label("Viewport Navigation", style={"font_size":10,"color":0xFFAAAAAA}, height=8)
                        with ui.HStack(height=22,spacing=6):
                            ui.Label("Speed",width=46, style={"font_size":10,"color":0xFFAAAAAA}, height=8)
                            sl_spd=ui.FloatSlider(min=0.05,max=100.0); sl_spd.model.set_value(0.5)
                            sl_spd.model.add_value_changed_fn(lambda m: _cam_speed.__setitem__(0, m.get_value_as_float()))

                        def _wall_collision_btn_style(active):
                            return {"background_color": 0xFF1A4A1A if active else 0xFF2A2A2A,
                                    "color":             0xFF88FF88 if active else 0xFFAAAAAA,
                                    "font_size": 11, "border_radius": 5}

                        btn_wall_collision = [None]
                        def _toggle_wall_collision():
                            _wall_collision[0] = not _wall_collision[0]
                            if _wall_collision[0]:
                                # Refresh the geometry cache now — a
                                # deliberate, expected pause (like Generate
                                # Zone) rather than a hitch mid-drag later.
                                # _clamp_move_by_walls also lazily refreshes
                                # this itself if it's ever empty, but doing
                                # it here means turning the toggle on always
                                # reflects the CURRENT scene, not whatever
                                # was cached the last time any zone was
                                # generated (which may be stale or absent).
                                set_status("Caching scene geometry for wall collision…")
                                _cache_scene_triangles(force=True)   # always reflect the CURRENT scene
                            set_status(f"Wall Collision: {'ON' if _wall_collision[0] else 'OFF'}")
                            btn_wall_collision[0].text = f"Wall Collision: {'ON' if _wall_collision[0] else 'OFF'}"
                            btn_wall_collision[0].style = _wall_collision_btn_style(_wall_collision[0])

                        btn_wall_collision[0] = ui.Button(
                            f"Wall Collision: {'ON' if _wall_collision[0] else 'OFF'}",
                            height=26, clicked_fn=_toggle_wall_collision,
                            style=_wall_collision_btn_style(_wall_collision[0]))
                        ui.Label("When on, cameras stop at walls (and other tall "
                                 "obstacles) instead of moving through them.",
                                 word_wrap=True, height=24,
                                 style={"font_size":9,"color":0xFF667766})

                        # ── Virtual Joystick ─────────────────────────────────
                        _JS_SIZE  = 130
                        _JS_THUMB = 18
                        _JS_MAX_R = (_JS_SIZE // 2) - _JS_THUMB - 4   # 43 px
                        _JS_CX    = _JS_SIZE // 2                      # 65 px

                        # Store geometry + closure refs for the drag update method
                        # Falls back to the Perspective camera when no user camera is
                        # selected, so the joysticks navigate the viewport itself
                        # instead of silently doing nothing.
                        self._js_cam_getter   = lambda: (_active_cam[0] or "/OmniverseKit_Persp")
                        self._js_speed_getter = lambda: _cam_speed[0]
                        # Fetch fresh each call (not a closure over the possibly-stale
                        # `stage` variable) — GetPrimAtPath on a stale Stage object
                        # fails with a cryptic pybind11 overload-mismatch error rather
                        # than a clean "invalid stage" message.
                        self._js_stage_getter = lambda: ctx.get_stage()
                        self._js_max_r        = float(_JS_MAX_R)
                        self._js_thumb_r      = float(_JS_THUMB)
                        self._js_cx           = float(_JS_CX)

                        with ui.HStack(spacing=8):
                            ui.Spacer()
                            with ui.VStack(spacing=4, width=_JS_SIZE):
                                ui.Label("Move X/Y", width=_JS_SIZE, alignment=ui.Alignment.CENTER,
                                         style={"font_size":10,"color":0xFF888888}, height=8)
                                # Single fixed-size Frame pins the joystick to an exact 130x130 box
                                # regardless of the ScrollingFrame's dynamic layout above it. Every
                                # element below is a direct ZStack child — one Frame, one ZStack,
                                # one shared coordinate origin for all Placers.
                                # margin=0 is required: the ancestor VStack sets style={"margin":6},
                                # and omni.ui cascades "margin" down to descendants that don't
                                # override it — confirmed via debug print to shift the thumb +6px
                                # right/down. This stops that cascade at the joystick's entry point.
                                with ui.Frame(width=_JS_SIZE, height=_JS_SIZE, style={"margin": 0}):
                                    with ui.ZStack(width=_JS_SIZE, height=_JS_SIZE):
                                        # Background
                                        _js_bg = ui.Rectangle(
                                            width=_JS_SIZE, height=_JS_SIZE,
                                            style={
                                                "background_color": 0xFF000000,
                                                "border_color":     0xFF444444,
                                                "border_width":     1.5,
                                                "border_radius":    14,
                                            })
                                        self._js_bg_rect = _js_bg

                                        # No separate boundary widget — the background rect itself
                                        # is the drag surface; _JS_MAX_R (derived from _JS_SIZE) is
                                        # the clamp radius used purely in the drag math below.

                                        # Thumb — starts centered; offset reassigned during drag.
                                        # stable_size=True tested and confirmed harmful: it stretches
                                        # the Circle to the Placer's leaked ambient size (142px) instead
                                        # of respecting its own explicit 36x36 — leave at default False.
                                        with ui.Placer(draggable=False,
                                                       offset_x=_JS_CX - _JS_THUMB,
                                                       offset_y=_JS_CX - _JS_THUMB) as _thumb_placer:
                                            ui.Circle(
                                                width=_JS_THUMB * 2,
                                                height=_JS_THUMB * 2,
                                                style={
                                                    "background_color": 0xFF888888,
                                                    "border_color":     0xFF555555,
                                                    "border_width":     1,
                                                    "border_radius":    _JS_THUMB,
                                                },
                                            )
                                        self._js_placer = _thumb_placer

                                        # Hit area — on top so it captures presses over the whole pad
                                        _js_hit = ui.Rectangle(
                                            width=_JS_SIZE, height=_JS_SIZE,
                                            style={"background_color": 0x00000000})

                                        def _js_pressed(*args):
                                            if len(args) < 3 or args[2] != 0:
                                                return
                                            self._js_active = True
                                            self._js_refresh_win_origin()   # cache once for the whole drag
                                            # Record cursor at press as center reference — the user presses
                                            # the grey circle which is at the pad center, so this position
                                            # IS the visual center. Avoids all widget coord-space issues.
                                            try:
                                                sx, sy = self._js_cursor_pos()
                                                self._js_center_win = (sx, sy) if sx is not None else None
                                            except Exception:
                                                self._js_center_win = None
                                            # Spin up a dedicated per-frame subscription (mirrors FPV marker drag)
                                            if self._js_drag_sub is None:
                                                self._js_drag_sub = (
                                                    omni.kit.app.get_app()
                                                    .get_update_event_stream()
                                                    .create_subscription_to_pop(
                                                        self._on_js_drag_update,
                                                        name="fpt_joystick_drag")
                                                )

                                        _js_hit.set_mouse_pressed_fn(_js_pressed)

                                        # Live coordinate readout, overlaid inside the pad near
                                        # the bottom edge (on top of the background/hit rect, but
                                        # a Label doesn't consume mouse-press events so it doesn't
                                        # block dragging).
                                        with ui.Placer(offset_x=0, offset_y=_JS_SIZE - 18):
                                            self._js_coord_lbl = ui.Label(
                                                "X: 0.00   Y: 0.00", width=_JS_SIZE,
                                                alignment=ui.Alignment.CENTER,
                                                style={"font_size":10,"color":0xFFFFCC44}, height=16)

                            # Height slider — up/down camera movement. Absolute, not
                            # momentary: the thumb position directly IS the camera's
                            # height (0..self._jsv_max_ft, matching this pipeline's
                            # existing "10 ft" unit-scale convention), and it stays
                            # where you leave it instead of springing back to center.
                            # Same box-pinning pattern as the other joysticks: margin=0
                            # Frame, everything below is a direct ZStack child.
                            # 40px was too narrow for an 18px-radius (36px-wide) thumb —
                            # only 2px of clearance on each side, visibly cramped. Widened
                            # to give the thumb comfortable room inside the track.
                            _JSV_W     = 56
                            _JSV_H     = _JS_SIZE
                            _JSV_THUMB = _JS_THUMB
                            _JSV_CX    = _JSV_W // 2

                            self._jsv_thumb_r = float(_JSV_THUMB)
                            self._jsv_cx = float(_JSV_CX)
                            self._jsv_track_h = float(_JSV_H)

                            def _jsv_read_active_height():
                                """Best-effort current height (ft) of whichever camera
                                the slider actually controls, for initializing/resyncing
                                the display — never writes anything back. Uses the exact
                                same fallback as _js_cam_getter (the function that
                                actually MOVES the camera): _active_cam[0] if a placed
                                camera is selected, else "/OmniverseKit_Persp" — so the
                                display always matches what dragging the slider would
                                actually move, including in Perspective/free-nav mode.
                                A previous version special-cased any /OmniverseKit_
                                camera to always show 0.0, which was wrong: the slider
                                still moves the Perspective camera in that mode, so the
                                readout should show ITS real height, not a fixed 0."""
                                try:
                                    cam_path = _active_cam[0] or "/OmniverseKit_Persp"
                                    s = _rs()
                                    if not s:
                                        return 0.0
                                    prim = s.GetPrimAtPath(cam_path)
                                    if not prim or not prim.IsValid():
                                        return 0.0
                                    vecs = UsdGeom.XformCommonAPI(prim).GetXformVectors(Usd.TimeCode.Default())
                                    t = vecs[0]
                                    _cx, _cy, fz, _w, _d, _unit, tf = get_floor_info()
                                    upf = tf / 10.0
                                    # Use THIS camera's own floor Z when it's a
                                    # placed, floor-stamped camera (_cam_floor) —
                                    # the whole-stage ground fz above is only the
                                    # right baseline for the free-nav Perspective
                                    # camera, which has no floor of its own. Read
                                    # and write (_jsv_move_camera) must agree on
                                    # this or the slider's displayed height and
                                    # what dragging it actually does diverge.
                                    try:
                                        from planlift_floorview.extension import _INSTANCE as _fpv_jsv
                                        _fidx = _cam_floor.get(cam_path)
                                        if _fidx is None and _fpv_jsv is not None:
                                            # Unstamped — most notably the
                                            # free-nav Perspective camera,
                                            # which is never "placed" so
                                            # never gets a stamp. Infer its
                                            # floor from its OWN current Z
                                            # instead of the whole-building
                                            # ground, or "Perspective
                                            # floating near floor 3" reads
                                            # as some large ground-relative
                                            # number that then becomes the
                                            # next dropped camera's height.
                                            _fidx = _fpv_jsv._floor_idx_for_z(t[2])
                                        if _fidx is not None and _fpv_jsv is not None:
                                            fz = _fpv_jsv._floor_z(_fidx)
                                    except Exception:
                                        pass
                                    v = (t[2] - fz) / upf if upf else 0.0
                                    v_clamped = max(0.0, min(self._jsv_max_ft, v))
                                    print(f"[JSV] read height: cam={cam_path} t.z={t[2]:.4f} fz={fz:.4f} "
                                          f"upf={upf:.4f} raw_ft={v:.3f} clamped_ft={v_clamped:.3f}")
                                    return v_clamped
                                except Exception as e:
                                    print(f"[JSV] read height error: {e}")
                                    return 0.0

                            def _jsv_sync_display(v):
                                """Clamp v to range, reposition the thumb, and update the
                                live label — pure display, does not move any camera."""
                                v = max(0.0, min(self._jsv_max_ft, float(v)))
                                self._jsv_height_ft = v
                                frac = v / self._jsv_max_ft if self._jsv_max_ft > 0 else 0.0
                                usable = _JSV_H - _JSV_THUMB * 2
                                y = (1.0 - frac) * usable
                                try:
                                    self._jsv_placer.offset_y = y
                                except Exception:
                                    pass
                                try:
                                    self._jsv_coord_lbl.text = f"{v:.1f} ft"
                                except Exception:
                                    pass

                            self._jsv_sync_fn = _jsv_sync_display
                            # Exposed so switch_to_cam (defined earlier, outside this
                            # nested scope) can resync the slider when the active
                            # camera changes.
                            self._jsv_read_height_fn = _jsv_read_active_height

                            with ui.VStack(spacing=4, width=_JSV_W):
                                ui.Label("Up/Dn", width=_JSV_W, alignment=ui.Alignment.CENTER,
                                         style={"font_size":10,"color":0xFF888888}, height=8)
                                with ui.Frame(width=_JSV_W, height=_JSV_H, style={"margin": 0}):
                                    with ui.ZStack(width=_JSV_W, height=_JSV_H):
                                        _jsv_bg = ui.Rectangle(
                                            width=_JSV_W, height=_JSV_H,
                                            style={
                                                "background_color": 0xFF000000,
                                                "border_color":     0xFF444444,
                                                "border_width":     1.5,
                                                "border_radius":    _JSV_W / 4,
                                            })
                                        self._jsv_bg_rect = _jsv_bg

                                        # Thumb — locked to horizontal center; vertical offset
                                        # IS the current height value, set by _jsv_sync_display.
                                        with ui.Placer(draggable=False,
                                                       offset_x=_JSV_CX - _JSV_THUMB,
                                                       offset_y=_JSV_H - _JSV_THUMB * 2) as _thumbv_placer:
                                            ui.Circle(
                                                width=_JSV_THUMB * 2,
                                                height=_JSV_THUMB * 2,
                                                style={
                                                    "background_color": 0xFF888888,
                                                    "border_color":     0xFF555555,
                                                    "border_width":     1,
                                                },
                                            )
                                        self._jsv_placer = _thumbv_placer

                                        # Hit area — on top so it captures presses over the whole pad
                                        _jsv_hit = ui.Rectangle(
                                            width=_JSV_W, height=_JSV_H,
                                            style={"background_color": 0x00000000})

                                        def _jsv_pressed(*args):
                                            if len(args) < 3 or args[2] != 0:
                                                return
                                            self._jsv_active = True
                                            self._jsv_press_value = self._jsv_height_ft
                                            self._jsv_cached_fz = None   # recompute floor Z once at the start of this drag
                                            self._js_refresh_win_origin()
                                            try:
                                                sx, sy = self._js_cursor_pos()
                                                self._jsv_center_win = (sx, sy) if sx is not None else None
                                            except Exception:
                                                self._jsv_center_win = None
                                            if self._jsv_drag_sub is None:
                                                self._jsv_drag_sub = (
                                                    omni.kit.app.get_app()
                                                    .get_update_event_stream()
                                                    .create_subscription_to_pop(
                                                        self._on_jsv_drag_update,
                                                        name="fpt_joystick_v_drag")
                                                )

                                        _jsv_hit.set_mouse_pressed_fn(_jsv_pressed)

                                        # At the widened 56px track, "10.0 ft" now fits inside
                                        # the pad's own bounds — no more overflow needed.
                                        with ui.Placer(offset_x=0, offset_y=_JSV_H / 2 - 8):
                                            self._jsv_coord_lbl = ui.Label(
                                                "0.0 ft", width=_JSV_W,
                                                alignment=ui.Alignment.CENTER,
                                                style={"font_size":9,"color":0xFFFFCC44}, height=16)

                            # Initialize the slider from whatever camera is active right
                            # now (falls back to 0.0 ft if none) instead of always
                            # starting at 0, so it doesn't visually contradict the
                            # camera's real height the moment the panel opens.
                            _jsv_sync_display(_jsv_read_active_height())

                            # Rotation joystick — Pitch X (vertical drag) / Roll Z (horizontal
                            # drag) only. Yaw Y is intentionally not controlled by this joystick.
                            # Same pattern as the other joysticks: margin=0 Frame pins an exact
                            # pixel box, background/thumb/hit-area are direct ZStack children.
                            _JSR_SIZE  = 130
                            _JSR_THUMB = 18
                            _JSR_MAX_R = (_JSR_SIZE // 2) - _JSR_THUMB - 4
                            _JSR_CX    = _JSR_SIZE // 2

                            self._jsr_max_r   = float(_JSR_MAX_R)
                            self._jsr_thumb_r = float(_JSR_THUMB)
                            self._jsr_cx      = float(_JSR_CX)

                            with ui.VStack(spacing=4, width=_JSR_SIZE):
                                ui.Label("Tilt / Roll", width=_JSR_SIZE, alignment=ui.Alignment.CENTER,
                                         style={"font_size":10,"color":0xFF888888}, height=8)
                                with ui.Frame(width=_JSR_SIZE, height=_JSR_SIZE, style={"margin": 0}):
                                    with ui.ZStack(width=_JSR_SIZE, height=_JSR_SIZE):
                                        _jsr_bg = ui.Rectangle(
                                            width=_JSR_SIZE, height=_JSR_SIZE,
                                            style={
                                                "background_color": 0xFF000000,
                                                "border_color":     0xFF444444,
                                                "border_width":     1.5,
                                                "border_radius":    14,
                                            })
                                        self._jsr_bg_rect = _jsr_bg

                                        with ui.Placer(draggable=False,
                                                       offset_x=_JSR_CX - _JSR_THUMB,
                                                       offset_y=_JSR_CX - _JSR_THUMB) as _thumbr_placer:
                                            ui.Circle(
                                                width=_JSR_THUMB * 2,
                                                height=_JSR_THUMB * 2,
                                                style={
                                                    "background_color": 0xFF888888,
                                                    "border_color":     0xFF555555,
                                                    "border_width":     1,
                                                },
                                            )
                                        self._jsr_placer = _thumbr_placer

                                        _jsr_hit = ui.Rectangle(
                                            width=_JSR_SIZE, height=_JSR_SIZE,
                                            style={"background_color": 0x00000000})

                                        def _jsr_pressed(*args):
                                            if len(args) < 3 or args[2] != 0:
                                                return
                                            self._jsr_active = True
                                            self._js_refresh_win_origin()
                                            try:
                                                sx, sy = self._js_cursor_pos()
                                                self._jsr_center_win = (sx, sy) if sx is not None else None
                                            except Exception:
                                                self._jsr_center_win = None
                                            if self._jsr_drag_sub is None:
                                                self._jsr_drag_sub = (
                                                    omni.kit.app.get_app()
                                                    .get_update_event_stream()
                                                    .create_subscription_to_pop(
                                                        self._on_jsr_drag_update,
                                                        name="fpt_joystick_r_drag")
                                                )

                                        _jsr_hit.set_mouse_pressed_fn(_jsr_pressed)

                                        # "Pitch: -23.4°   Roll: +43.1°" doesn't fit one line at
                                        # a legible size inside a 130px box, so it's split across
                                        # two lines instead of shrinking the font further.
                                        with ui.Placer(offset_x=0, offset_y=_JSR_SIZE - 34):
                                            self._jsr_coord_lbl = ui.Label(
                                                "Tilt: 0.0°\nRoll: 0.0°", width=_JSR_SIZE,
                                                alignment=ui.Alignment.CENTER,
                                                style={"font_size":10,"color":0xFFFFCC44}, height=32)
                            ui.Spacer()
                        ui.Spacer(height=4)

                        ui.Line(height=0, style={"color":0xFF2A2A2A}, spacing=4)

                # ── RAIN TAB ──
                sf_rain = ui.ScrollingFrame(height=_fill_h, visible=False,
                    horizontal_scrollbar_policy=ui.ScrollBarPolicy.SCROLLBAR_ALWAYS_OFF,
                    vertical_scrollbar_policy=ui.ScrollBarPolicy.SCROLLBAR_AS_NEEDED)
                with sf_rain:
                    with ui.VStack(spacing=8, style={"margin":10}):
                        ui.Label("Rain System", style={"font_size":14,"color":0xFF88CCFF})
                        ui.Line(style={"color":0xFF222222})
                        btn_rain_bake = ui.Button("Bake Rain System", height=42, clicked_fn=bake_rain,
                            style={"background_color":0xFF1A3A5A,"color":0xFFFFFFFF,"font_size":13,"border_radius":6})
                        ui.Line(style={"color":0xFF222222})
                        btn_rain_start = ui.Button("Start Rain", height=42, clicked_fn=start_rain,
                            style={"background_color":0xFF1A5A2A,"color":0xFFFFFFFF,"font_size":13,"border_radius":6})
                        btn_rain_start.visible = False
                        btn_rain_stop = ui.Button("Stop Rain", height=42, clicked_fn=stop_rain,
                            style={"background_color":0xFF5A2A1A,"color":0xFFFFFFFF,"font_size":13,"border_radius":6})
                        btn_rain_stop.visible = False
                        ui.Line(style={"color":0xFF222222})
                        ui.Button("Clear Rain", height=32, clicked_fn=clear_rain,
                            style={"background_color":0xFF3A1A1A,"color":0xFFFF8888,"font_size":11,"border_radius":5})
                        ui.Spacer(height=10)

                ui.Line(style={"color":0xFF1E1E1E})
                lbl_status=ui.Label("Ready.", word_wrap=True,
                    style={"font_size":10,"color":0xFF66AA66,"margin":5})

        def switch_tab(tab):
            sf_lights.visible  = (tab == 0)
            sf_camera.visible  = (tab == 1)
            sf_rain.visible    = (tab == 2)
            btn_tab_light.style={"background_color":0xFF2D5A2D if tab==0 else 0xFF111111,
                "color":0xFFFFFFFF if tab==0 else 0xFF555555,"font_size":12,"border_radius":0}
            btn_tab_cam.style={"background_color":0xFF1E3A6A if tab==1 else 0xFF111111,
                "color":0xFFFFFFFF if tab==1 else 0xFF555555,"font_size":12,"border_radius":0}
            btn_tab_rain.style={"background_color":0xFF1A3A5A if tab==2 else 0xFF111111,
                "color":0xFFFFFFFF if tab==2 else 0xFF555555,"font_size":12,"border_radius":0}

        btn_tab_light.set_clicked_fn(lambda: switch_tab(0))
        btn_tab_cam.set_clicked_fn(lambda: switch_tab(1))
        btn_tab_rain.set_clicked_fn(lambda: switch_tab(2))

        # ── Store state needed by the viewport drop callback ──────────────
        # We capture the live closures so _on_viewport_drop can call them
        # without re-entering _build_ui.
        self._ds = {
            "ctx":          ctx,
            "CAMS_PATH":    CAMS_PATH,
            "cam_count":    _cam_count,
            "cam_list":     _cam_list,
            "CAM_PRESETS":  CAM_PRESETS,
            "PRESET_KEYS":  PRESET_KEYS,
            "cam_combo":      cam_combo,
            "switch_to_cam":  switch_to_cam,
            "schedule_rebuild": _schedule_rebuild,
            "set_status":     set_status,
            "ensure":         ensure,
            "get_floor_info": get_floor_info,
            "UsdGeom":        UsdGeom,
            "Gf":             Gf,
            "Sdf":            Sdf,
            "IND_LIGHTS_PATH":  IND_LIGHTS_PATH,
            "ind_light_count":  _ind_light_count,
            "ind_light_list":   _ind_light_list,
            "sl_intensity":     sl_intensity,
            "sl_lwidth":        sl_lwidth,
            "sl_ldepth":        sl_ldepth,
            "sl_temp":          sl_temp,
            "set_attr":         set_attr,
            "schedule_ind_light_list_rebuild": _schedule_ind_light_list_rebuild,
            "select_ind_light": _select_ind_light,
            "cam_colors":     _cam_colors,
            "cam_floor":      _cam_floor,
            "cam_optical_path": _cam_optical_path,
            "wall_collision":   _wall_collision,
            "clamp_move_by_walls": _clamp_move_by_walls,
        }
        # Register the viewport drop listener now that the UI is ready
        self._setup_drop_listener()

        # Warm the get_floor_info() cache off the critical path so the very
        # first joystick interaction (height joystick first click / Move X/Y
        # first release) doesn't eat the ~1s full-stage bbox walk. The stage
        # is usually empty at UI-build time, so retry a few times until the
        # timelapse USD is actually loaded and the walk returns real bounds.
        async def _warm_floor_info_cache():
            app_obj = omni.kit.app.get_app()
            for _ in range(30):            # ~30 frames + settle, then give up
                await app_obj.next_update_async()
            _EMPTY = (0.0, 0.0, 0.0, 10.0, 10.0, "m", 3.048)   # get_floor_info's empty-stage sentinel
            for attempt in range(6):       # retry while stage still loading
                try:
                    val = get_floor_info(force=True)
                    if val and tuple(val) != _EMPTY:
                        break
                except Exception:
                    pass
                for _ in range(60):
                    await app_obj.next_update_async()
        try:
            self._warm_task = asyncio.ensure_future(_warm_floor_info_cache())
        except Exception:
            pass

        # Expose internals so Floor Plan View can add cameras to the list
        self._cam_list            = _cam_list
        self._cam_floor           = _cam_floor
        self._cam_count           = _cam_count
        self._do_schedule_rebuild = _schedule_rebuild

        # Expose internals so Floor Plan View can add individual lights to the list
        self._ind_light_list                = _ind_light_list
        self._ind_light_count               = _ind_light_count
        self._do_schedule_light_list_rebuild = _schedule_ind_light_list_rebuild

        print("[FPT] Floor Plan Tool v16 ready")
        self._win = win

    # ── Drag-to-viewport: drop listener ──────────────────────────────────

    _DRAG_TOKEN      = "FLOORPLAN_CAMERA_DRAG"
    _drag_active     = False   # set True in set_drag_fn, False after camera placed
    _drag_preset_idx = 0       # class-level so Floor Plan View can read it at drop time
    _drag_kind       = "camera"   # "camera" or "light" — class-level so Floor Plan View can read it at drop time
    _LIGHT_SPAWN_HEIGHT_FT = 10.0  # fixed ceiling height for individually-dropped lights
    _IND_LIGHT_DEFAULT_FT  = 4.0   # default width/height of an individual light (a normal local fixture, not a scene-sized panel)

    async def _restore_win_async(self):
        await omni.kit.app.get_app().next_update_async()
        if self._win:
            self._win.visible = True

    def _win_visibility_guard(self, visible: bool):
        if not visible:
            asyncio.ensure_future(self._restore_win_async())

    def _fpv_cam_height(self, floor_idx=None):
        """Return (floor_z, height_in_scene_units) from Floor Plan View's cam
        height field, for the given floor (Floor Plan View's currently
        active floor tab when floor_idx is None). Returns (None, None) when
        Floor Plan View is not loaded (caller uses its own fallback)."""
        try:
            from planlift_floorview.extension import _INSTANCE as _fpv
            if _fpv is not None:
                idx = floor_idx if floor_idx is not None else getattr(_fpv, "_active_floor_idx", 0)
                return _fpv._floor_z(idx), _fpv._ft_to_units(_fpv._spawn_height_ft, idx)
        except Exception:
            pass
        return None, None

    def _fpv_floor_registry(self):
        """Floor Plan View's floor registry (list of {idx,label,yBase,image}),
        in floor order, or [] when it's not loaded / has no manifest yet
        (legacy single-floor scene)."""
        try:
            from planlift_floorview.extension import _INSTANCE as _fpv
            if _fpv is not None:
                return list(getattr(_fpv, "_floors_registry", []) or [])
        except Exception:
            pass
        return []

    def _fpv_active_floor_idx(self):
        """Floor Plan View's currently open floor tab, or 0 when it's not
        loaded / has no registry yet (legacy single-floor scene)."""
        try:
            from planlift_floorview.extension import _INSTANCE as _fpv
            if _fpv is not None:
                return getattr(_fpv, "_active_floor_idx", 0)
        except Exception:
            pass
        return 0

    def _fpv_floor_idx_for_z(self, z):
        """Nearest Floor Plan View registry floor to a raw world Z — used to
        tag a camera's floor after a 3D-viewport placement resolved from a
        real geometry raycast rather than an explicit floor tab."""
        try:
            from planlift_floorview.extension import _INSTANCE as _fpv
            if _fpv is not None and hasattr(_fpv, "_floor_idx_for_z"):
                return _fpv._floor_idx_for_z(z)
        except Exception:
            pass
        return self._fpv_active_floor_idx()

    # Extensions that drive viewport navigation (orbit/pan/zoom), prim
    # picking, and manipulator gizmos. Disabling them is app-wide, not
    # scoped to one window — the only reliable way found to block viewport
    # interaction, since navigation/selection register gesture handling
    # directly on the scene view rather than going through omni.ui widgets.
    _VIEWPORT_MANIPULATOR_EXTS = (
        "omni.kit.manipulator.camera",
        "omni.kit.manipulator.selection",
        "omni.kit.manipulator.prim",
        # Owns viewport keyboard shortcuts (Q/W/E/R tool-switch, G grid toggle,
        # etc.) via omni.kit.hotkeys.core. With omni.kit.manipulator.prim
        # disabled, its move/rotate/scale hotkeys reference a toolbar button
        # that no longer exists and throw AttributeError on every keypress —
        # disabling the toolbar itself removes both the error and the hotkeys.
        "omni.kit.widget.toolbar",
        # Central hotkey dispatch — every registered shortcut (G grid toggle,
        # Shift+W wireframe, etc.) routes through this regardless of which
        # extension registered it. Disabling it blocks all of them at once
        # instead of having to find and disable each feature individually.
        "omni.kit.hotkeys.core",
    )

    # Camera navigation, selection, right-click context menu, and double-click
    # center-of-interest picking are all attached to the viewport window as
    # named layers (category "manipulator") the moment the window is first
    # created — well before this extension loads. Toggling the owning
    # extension's load-state doesn't retroactively remove an already-attached
    # layer instance, so the reliable fix is finding that live layer on the
    # current viewport window and flipping its own .visible property, which
    # the layer's on_build() gesture registration checks directly.
    _VIEWPORT_LAYER_NAMES = ("Camera", "Selection", "ContextMenu", "ObjectClick")

    # Session-scoped (not saved to disk) carb setting — True once the lock has
    # been applied this Kit session. Unlike a plain Python variable, this
    # survives the module re-import a code-edit hot-reload triggers, so we
    # only actually toggle these extensions once per session instead of
    # off-on-off on every single reload, which was destabilizing window
    # docking (toggling UI-owning extensions like the toolbar forces Kit's
    # dock layout to recompute).
    _VP_LOCK_SETTING = "/app/floorplanTool/vpInteractionLocked"
    # Opt-in: only take over the viewport (disable native mouse/keyboard
    # navigation + click-selection, forcing the on-screen joysticks) when this
    # is explicitly true. Default OFF so the viewport behaves like stock Isaac
    # Sim — mouse orbit/pan/zoom, WASD fly, and left-click to select all work,
    # and the virtual joysticks are just an extra option. Set
    #   [settings.app.floorplanTool] lockViewport = true
    # in the .kit (or `--/app/floorplanTool/lockViewport=1`) to restore the
    # old joystick-only behaviour.
    _VP_LOCK_ENABLE_SETTING = "/app/floorplanTool/lockViewport"

    def _disable_viewport_manipulators(self):
        if self._vp_manipulators_disabled:
            return
        try:
            import carb
            if not carb.settings.get_settings().get(self._VP_LOCK_ENABLE_SETTING):
                # lockViewport is OFF now. If a PRIOR run (with it ON) disabled
                # the manipulator/toolbar/hotkey extensions and a hot-reload
                # then lost the local "disabled" flag, they'd be stuck off —
                # and with omni.kit.manipulator.prim disabled but the toolbar
                # still up, the W/E/R/Q viewport hotkeys throw
                #   AttributeError: 'TransformButtonGroup' has no '_move_button'
                # on every keypress. Heal it: re-enable them and clear the flag.
                _s = carb.settings.get_settings()
                if _s.get(self._VP_LOCK_SETTING):
                    print("[FPT] lockViewport off but a prior run left the viewport locked — restoring extensions")
                    try:
                        mgr = omni.kit.app.get_app().get_extension_manager()
                        for _ext in self._VIEWPORT_MANIPULATOR_EXTS:
                            try:
                                mgr.set_extension_enabled_immediate(_ext, True)
                            except Exception as _e:
                                print(f"[FPT] re-enable {_ext}: {_e}")
                    except Exception as _e:
                        print(f"[FPT] heal re-enable error: {_e}")
                    try:
                        _s.set(self._VP_LOCK_SETTING, False)
                    except Exception:
                        pass
                print("[FPT] lockViewport off — native mouse/keyboard navigation + click-select left enabled")
                return
        except Exception as e:
            print(f"[FPT] lockViewport check error: {e}")
        try:
            import carb
            if carb.settings.get_settings().get(self._VP_LOCK_SETTING):
                # Already applied earlier this session (e.g. by a previous
                # hot-reload) — mark as disabled locally without re-toggling.
                self._vp_manipulators_disabled = True
                print("[FPT] Viewport lock already applied this session — skipping re-toggle")
                return
        except Exception as e:
            print(f"[FPT] lock-setting check error: {e}")

        try:
            mgr = omni.kit.app.get_app().get_extension_manager()
            for ext_name in self._VIEWPORT_MANIPULATOR_EXTS:
                try:
                    mgr.set_extension_enabled_immediate(ext_name, False)
                except Exception as e:
                    print(f"[FPT] disable {ext_name}: {e}")
        except Exception as e:
            print(f"[FPT] _disable_viewport_manipulators (ext) error: {e}")

        try:
            import omni.kit.viewport.utility as vp_util
            vp_win = vp_util.get_active_viewport_window()
            if vp_win is None:
                print("[FPT] _disable_viewport_manipulators: no active viewport")
                return
            self._vp_win_for_manipulators = vp_win
            for lname in self._VIEWPORT_LAYER_NAMES:
                try:
                    layer = vp_win._find_viewport_layer(lname, "manipulator")
                    if layer is not None:
                        layer.visible = False
                        print(f"[FPT] Viewport layer '{lname}' hidden")
                    else:
                        print(f"[FPT] Viewport layer '{lname}' not found")
                except Exception as e:
                    print(f"[FPT] hide layer {lname}: {e}")
            self._vp_manipulators_disabled = True
            try:
                import carb
                carb.settings.get_settings().set(self._VP_LOCK_SETTING, True)
            except Exception as e:
                print(f"[FPT] lock-setting write error: {e}")
        except Exception as e:
            print(f"[FPT] _disable_viewport_manipulators (layers) error: {e}")

    def _restore_viewport_manipulators(self):
        if not self._vp_manipulators_disabled:
            return
        vp_win = getattr(self, '_vp_win_for_manipulators', None)
        if vp_win is not None:
            for lname in self._VIEWPORT_LAYER_NAMES:
                try:
                    layer = vp_win._find_viewport_layer(lname, "manipulator")
                    if layer is not None:
                        layer.visible = True
                except Exception as e:
                    print(f"[FPT] show layer {lname}: {e}")
        try:
            mgr = omni.kit.app.get_app().get_extension_manager()
            for ext_name in self._VIEWPORT_MANIPULATOR_EXTS:
                try:
                    mgr.set_extension_enabled_immediate(ext_name, True)
                except Exception as e:
                    print(f"[FPT] re-enable {ext_name}: {e}")
        except Exception as e:
            print(f"[FPT] _restore_viewport_manipulators (ext) error: {e}")
        self._vp_manipulators_disabled = False
        print("[FPT] Viewport navigation/selection restored")

    def _setup_drop_listener(self):
        """
        Register drop listeners via THREE strategies so at least one fires.

        Strategy A — add_external_drag_drop_support
          The viewport window's dedicated API.  In Kit 107.x this fires for
          BOTH OS-level file drops AND omni.ui set_drag_fn widget drags when
          the user releases over the viewport.  Callback: (payload, x, y).

        Strategy B — viewport frame set_drop_fn
          The viewport's omni.ui Frame also receives drop events for internal
          drags.  x, y are frame-local pixels.

        Strategy C — carb.input mouse subscription
          Fires on every mouse event; useful as a last resort but may miss
          events during active omni.ui drag sessions on some Kit builds.
        """
        try:
            import omni.kit.viewport.utility as vp_util
            vp_win = vp_util.get_active_viewport_window()
            if vp_win is None:
                print("[FPT] No active viewport yet — drop listener deferred")
                return

            # ── Strategy A: add_external_drag_drop_support ────────────────
            if hasattr(vp_win, "add_external_drag_drop_support"):
                self._drop_sub = vp_win.add_external_drag_drop_support(
                    self._on_external_drop)
                print("[FPT] Strategy A: external drop support registered")
            else:
                print("[FPT] Strategy A: add_external_drag_drop_support not found")

            # ── Strategy B: viewport frame set_drop_fn ────────────────────
            try:
                frame = vp_win.frame
                if frame is not None:
                    frame.set_accept_drop_fn(
                        lambda d: d == self._DRAG_TOKEN)
                    frame.set_drop_fn(self._on_frame_drop)
                    print("[FPT] Strategy B: viewport frame drop fn registered")
            except Exception as e2:
                print(f"[FPT] Strategy B skipped: {e2}")

            # ── Block viewport navigation/selection ─────────────────────────
            # Navigation and picking are driven by omni.kit.manipulator.camera
            # / .selection / .prim registering their own gesture handling
            # directly on the viewport's scene view — they don't go through
            # regular omni.ui widget mouse-press routing, so a UI overlay
            # can't intercept them. Disabling the extensions themselves is
            # the reliable way to block them; drag-and-drop into the viewport
            # (Strategy A/B above) is unaffected since it's a separate path.
            self._disable_viewport_manipulators()

        except Exception as e:
            print(f"[FPT] _setup_drop_listener error: {e}")
            import traceback; traceback.print_exc()

        # ── Strategy C: carb.input mouse subscription ─────────────────────
        try:
            import carb.input as ci
            self._ci = ci.acquire_input_interface()
            mouse = self._ci.get_mouse()
            self._mouse_sub = self._ci.subscribe_to_mouse_events(
                mouse, self._on_carb_mouse)
            print("[FPT] Strategy C: carb.input mouse listener registered")
        except Exception as e:
            print(f"[FPT] Strategy C skipped: {e}")

        # ── Cursor poller: app update event ───────────────────────────────
        # set_drop_fn fires on a background thread where print() is swallowed.
        # Subscribe to the main-thread update event instead, poll GetCursorPos
        # every frame while a drag is in flight, and cache the result so the
        # drop handler can convert screen→viewport without calling ctypes itself.
        try:
            app_obj = omni.kit.app.get_app()
            self._update_sub = app_obj.get_update_event_stream()\
                .create_subscription_to_pop(
                    self._on_update_drag, name="fpt_drag_cursor_poll")
            print("[FPT] Cursor-poll update subscription registered")
        except Exception as e:
            print(f"[FPT] Cursor-poll sub failed: {e}")

    # ── Scene AABB cache (built once at drag start) ───────────────────────

    def _cache_scene_aabbs(self):
        """Return list of (xmin,ymin,zmin,xmax,ymax,zmax) for every
        Boundable prim that is NOT part of the FloorPlanRig."""
        try:
            from pxr import UsdGeom, Usd
            stage = self._ds["ctx"].get_stage()
            if not stage:
                return []
            bc = UsdGeom.BBoxCache(Usd.TimeCode.Default(),
                [UsdGeom.Tokens.default_, UsdGeom.Tokens.render],
                useExtentsHint=True)
            result = []
            for prim in _walk_prims(stage):
                if "FloorPlanRig" in str(prim.GetPath()):
                    continue
                if not prim.IsA(UsdGeom.Boundable):
                    continue
                try:
                    rng = bc.ComputeWorldBound(prim).ComputeAlignedRange()
                    if rng.IsEmpty():
                        continue
                    mn, mx = rng.GetMin(), rng.GetMax()
                    result.append((float(mn[0]), float(mn[1]), float(mn[2]),
                                   float(mx[0]), float(mx[1]), float(mx[2])))
                except Exception:
                    continue
            return result
        except Exception as e:
            print(f"[FPT] _cache_scene_aabbs: {e}")
            return []

    # ── Per-frame AABB raycast ────────────────────────────────────────────

    def _raycast_aabbs(self, ox, oy, oz, dx, dy, dz):
        """Software slab-method ray/AABB intersection against cached boxes.
        Returns (hx, hy, hz) of the closest hit or None."""
        best_t = float('inf')
        hit = None
        for (xmn, ymn, zmn, xmx, ymx, zmx) in self._drag_aabbs:
            t_min = 0.0
            t_max = float('inf')
            for lo, hi, d, o in (
                (xmn, xmx, dx, ox),
                (ymn, ymx, dy, oy),
                (zmn, zmx, dz, oz),
            ):
                if abs(d) < 1e-9:
                    if o < lo or o > hi:
                        t_max = -1.0
                        break
                else:
                    t1, t2 = (lo - o) / d, (hi - o) / d
                    if t1 > t2:
                        t1, t2 = t2, t1
                    t_min = max(t_min, t1)
                    t_max = min(t_max, t2)
            if t_min < t_max and 0.001 < t_min < best_t:
                best_t = t_min
                hit = (ox + t_min * dx, oy + t_min * dy, oz + t_min * dz)
        return hit

    # ── Cursor position poller (runs on main update thread) ──────────────

    _GHOST_TICK = 0   # frame counter for throttling ghost updates

    def _on_update_drag(self, _event):
        """Poll cursor every frame; update ghost cone/light during viewport drag."""
        if not getattr(self, '_drag_active', False):
            if self._ghost_cone_path or self._ghost_light_path:
                self._destroy_ghost_cone()
            return
        try:
            import ctypes
            class _PT(ctypes.Structure):
                _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]
            pt = _PT()
            ctypes.windll.user32.GetCursorPos(ctypes.byref(pt))
            self._drag_screen_pos = (float(pt.x), float(pt.y))
        except Exception:
            return

        # Throttle heavier work to ~20 fps (every 3 frames at 60 fps)
        FloorPlanToolExtension._GHOST_TICK += 1
        if FloorPlanToolExtension._GHOST_TICK % 3 != 0:
            return

        try:
            vp_x, vp_y = self._screen_to_vp_px(
                self._drag_screen_pos[0], self._drag_screen_pos[1])
            if vp_x < 0:
                return
            world_pos = self._raycast_scene_or_floor(vp_x, vp_y)
            if world_pos is None:
                return
            self._ghost_last_pos = world_pos
            if getattr(self, '_drag_kind', 'camera') == 'light':
                self._update_ghost_light(world_pos)
            else:
                self._update_ghost_camera(world_pos)
        except Exception:
            pass

    # ── Joystick drag update (mirrors FPV marker drag pattern) ───────────

    def _js_refresh_win_origin(self):
        """Resolve + cache the Kit window's client-area screen origin. Called
        once when a joystick drag starts, so the per-frame handlers only do a
        cheap GetCursorPos + subtract instead of a FindWindowW window
        enumeration every single frame (that was the main joystick lag)."""
        try:
            import ctypes, omni.appwindow
            class _PT(ctypes.Structure):
                _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]
            aw    = omni.appwindow.get_default_app_window()
            title = aw.get_title() if hasattr(aw, "get_title") else ""
            hwnd  = ctypes.windll.user32.FindWindowW(None, title or None)
            if not hwnd:
                hwnd = ctypes.windll.user32.GetForegroundWindow()
            o = _PT(0, 0)
            ctypes.windll.user32.ClientToScreen(hwnd, ctypes.byref(o))
            self._js_win_origin = (float(o.x), float(o.y))
        except Exception:
            self._js_win_origin = None

    def _js_cursor_pos(self):
        """Window-relative cursor position, using the origin cached by
        _js_refresh_win_origin() so this is cheap to call per frame."""
        try:
            import ctypes
            class _PT(ctypes.Structure):
                _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]
            if self._js_win_origin is None:
                self._js_refresh_win_origin()
            if self._js_win_origin is None:
                return None, None
            ox, oy = self._js_win_origin
            c = _PT()
            ctypes.windll.user32.GetCursorPos(ctypes.byref(c))
            return float(c.x) - ox, float(c.y) - oy
        except Exception:
            return None, None

    def _on_js_drag_update(self, _):
        if not self._js_active:
            self._js_drag_sub = None
            return
        # Check left mouse button still held (mirrors FPV _on_drag_update)
        try:
            import carb.input as ci
            import omni.appwindow
            iface = ci.acquire_input_interface()
            mouse = omni.appwindow.get_default_app_window().get_mouse()
            try:
                held = float(iface.get_mouse_value(mouse, ci.MouseInput.LEFT_BUTTON)) >= 0.5
            except Exception:
                held = bool(int(iface.get_mouse_button_flags(
                    mouse, ci.MouseInput.LEFT_BUTTON)) & 0x1)
        except Exception:
            held = True
        if not held:
            self._js_active = False
            self._js_dx = 0.0
            self._js_dy = 0.0
            try:
                self._js_placer.offset_x = self._js_cx - self._js_thumb_r
                self._js_placer.offset_y = self._js_cx - self._js_thumb_r
            except Exception:
                pass
            # Coordinate label deliberately NOT reset here — it keeps showing
            # the last deflection value from the drag instead of snapping
            # back to "X: 0.00   Y: 0.00" just because the knob itself springs
            # back to center. Matches the "don't reset on release" behavior
            # already used for the Up/Dn slider and Rotate readout.
            # Resync the Up/Dn slider once, now, instead of every frame: a
            # tilted forward-move changes height as a side effect.
            try:
                self._jsv_sync_fn(self._jsv_read_height_fn())
            except Exception:
                pass
            self._js_drag_sub = None
            return
        # Get cursor position and compute displacement from joystick center
        try:
            sx, sy = self._js_cursor_pos()
            if sx is None:
                return
            if self._js_center_win is None:
                return
            cx_win, cy_win = self._js_center_win
            ox = sx - cx_win
            oy = sy - cy_win
            max_r = self._js_max_r
            ox = max(-max_r, min(max_r, ox))
            oy = max(-max_r, min(max_r, oy))
            self._js_dx = ox / max_r if max_r > 0 else 0.0
            self._js_dy = oy / max_r if max_r > 0 else 0.0
            # Reposition thumb
            self._js_placer.offset_x = self._js_cx + ox - self._js_thumb_r
            self._js_placer.offset_y = self._js_cx + oy - self._js_thumb_r
            # Live readout — Y sign flipped so "dragged up" (forward) reads as
            # positive, matching the "up = positive" convention a joystick
            # display is expected to show (screen-space Y grows downward,
            # which _joystick_move_camera already accounts for via -dy).
            try:
                self._js_coord_lbl.text = f"X: {self._js_dx:+.2f}   Y: {-self._js_dy:+.2f}"
            except Exception:
                pass
            # Move camera
            self._joystick_move_camera()
        except Exception as e:
            print(f"[JS] drag update: {e}")

    # ── Joystick camera movement ──────────────────────────────────────────

    def _joystick_move_camera(self):
        import math as _m
        from pxr import Gf, UsdGeom, Usd
        dx = self._js_dx
        dy = self._js_dy
        if abs(dx) < 0.02 and abs(dy) < 0.02:
            return
        try:
            cam_path = self._js_cam_getter()
            if not cam_path:
                return
            stage = self._js_stage_getter()
            if stage is None:
                return
            prim = stage.GetPrimAtPath(cam_path)
            if not prim or not prim.IsValid():
                return
            speed = self._js_speed_getter()

            # Direction must reflect the FULL orientation (rig's yaw plus
            # the camera's own tilt/roll), so it's read from the actual
            # Camera prim's world transform (which includes its rig
            # ancestor's yaw automatically) rather than this rig prim's own
            # transform, which only carries position + yaw — see
            # _cam_optical_path. Position itself still lives on the rig
            # (`prim`/`cam_path`), so translate is read/written there below,
            # unaffected by this direction-only lookup.
            optical_path = (self._ds["cam_optical_path"](cam_path)
                            if self._ds else cam_path)
            dir_prim = stage.GetPrimAtPath(optical_path)
            if not dir_prim or not dir_prim.IsValid():
                dir_prim = prim

            # World-space forward (-local Z) and right (+local X) from live transform
            m = UsdGeom.Xformable(dir_prim).ComputeLocalToWorldTransform(Usd.TimeCode.Default())
            fwd = Gf.Vec3d(-m[2][0], -m[2][1], -m[2][2])
            rt  = Gf.Vec3d( m[0][0],  m[0][1],  m[0][2])
            fl = _m.sqrt(fwd[0]**2 + fwd[1]**2 + fwd[2]**2)
            rl = _m.sqrt(rt[0]**2  + rt[1]**2  + rt[2]**2)
            if fl < 1e-6 or rl < 1e-6:
                return
            fwd = Gf.Vec3d(fwd[0]/fl, fwd[1]/fl, fwd[2]/fl)
            rt  = Gf.Vec3d(rt[0]/rl,  rt[1]/rl,  rt[2]/rl)

            # dy negative = dragged up = forward; dx positive = dragged right = strafe right
            move = Gf.Vec3d(
                (fwd[0] * (-dy) + rt[0] * dx) * speed,
                (fwd[1] * (-dy) + rt[1] * dx) * speed,
                (fwd[2] * (-dy) + rt[2] * dx) * speed,
            )

            vecs = UsdGeom.XformCommonAPI(prim).GetXformVectors(Usd.TimeCode.Default())
            t = list(vecs[0])
            r = list(vecs[1])
            new_x = t[0] + move[0]
            new_y = t[1] + move[1]
            t[2] += move[2]
            # Wall Collision (Viewport Navigation toggle): stop the move
            # short of any wall-like surface between the current and
            # proposed position instead of passing through it. No-op when
            # the toggle is off.
            if self._ds and self._ds.get("clamp_move_by_walls"):
                new_x, new_y = self._ds["clamp_move_by_walls"](t[0], t[1], t[2], new_x, new_y)
            t[0], t[1] = new_x, new_y
            # Kit's camera manipulator writes /OmniverseKit_Persp's transform to
            # the session layer (non-persistent, stronger opinion). Writing to
            # whatever the stage's current edit target is (typically the root
            # layer) gets silently overridden by that stronger session-layer
            # opinion — confirmed via debug readback showing our write never
            # sticking. Target the session layer explicitly to actually win,
            # but only for the special Kit viewport cameras — regular
            # user-created cameras must keep writing to the normal edit
            # target so their movement actually persists with the stage.
            # Position-only joystick — rotation is unchanged, so only write
            # translate (skipping the redundant SetRotate halves the per-frame
            # attribute writes + change notifications).
            if cam_path.startswith("/OmniverseKit_"):
                with Usd.EditContext(stage, Usd.EditTarget(stage.GetSessionLayer())):
                    UsdGeom.XformCommonAPI(prim).SetTranslate(Gf.Vec3d(*t))
            else:
                UsdGeom.XformCommonAPI(prim).SetTranslate(Gf.Vec3d(*t))
            # (Up/Dn slider resync moved to drag-end — see _on_js_drag_update.)
        except Exception as e:
            print(f"[JS] {e}")

    # ── Vertical joystick (up/down) ─────────────────────────────────────

    def _on_jsv_drag_update(self, _):
        if not self._jsv_active:
            self._jsv_drag_sub = None
            return
        # Check left mouse button still held (mirrors _on_js_drag_update)
        try:
            import carb.input as ci
            import omni.appwindow
            iface = ci.acquire_input_interface()
            mouse = omni.appwindow.get_default_app_window().get_mouse()
            try:
                held = float(iface.get_mouse_value(mouse, ci.MouseInput.LEFT_BUTTON)) >= 0.5
            except Exception:
                held = bool(int(iface.get_mouse_button_flags(
                    mouse, ci.MouseInput.LEFT_BUTTON)) & 0x1)
        except Exception:
            held = True
        if not held:
            # Slider — unlike the other joysticks, releasing does NOT reset
            # the value or thumb position; it stays exactly where left.
            self._jsv_active = False
            self._jsv_drag_sub = None
            # Push the final height into Floor Plan View once, on release,
            # instead of every frame during the drag.
            try:
                from planlift_floorview.extension import _INSTANCE as _fpv
                if _fpv is not None:
                    cp = self._js_cam_getter()
                    if cp:
                        _fpv._update_height_field_from_cam(cp)
            except Exception:
                pass
            return
        # Compute the new absolute height from how far the cursor has moved
        # (vertically) since the press, not from where in the pad you
        # clicked — screen_position_x/y on the pad widget itself is
        # unreliable in this Kit build (see the FPV marker-drag notes
        # elsewhere in this file), so this mirrors the same delta-from-press
        # approach already proven to work for the other joysticks, just
        # applied to an absolute running value instead of a relative one.
        try:
            _, sy = self._js_cursor_pos()
            if sy is None:
                return
            if self._jsv_center_win is None:
                return
            _, cy_win = self._jsv_center_win
            delta_px = sy - cy_win
            # Dragging UP (negative delta_px) increases height.
            delta_ft = (-delta_px / self._jsv_track_h * self._jsv_max_ft
                        if self._jsv_track_h > 0 else 0.0)
            self._jsv_sync_fn(self._jsv_press_value + delta_ft)
            # Move camera (Floor Plan View's height field is resynced on
            # release, not every frame — see the `not held` branch above).
            self._jsv_move_camera()
        except Exception as e:
            print(f"[JSV] drag update: {e}")

    def _jsv_move_camera(self):
        from pxr import Gf, UsdGeom, Usd
        try:
            cam_path = self._js_cam_getter()
            if not cam_path:
                print("[JSV] move: no cam_path from _js_cam_getter — aborting")
                return
            stage = self._js_stage_getter()
            if stage is None:
                print(f"[JSV] move: cam_path={cam_path} but stage is None — aborting")
                return
            prim = stage.GetPrimAtPath(cam_path)
            if not prim or not prim.IsValid():
                print(f"[JSV] move: cam_path={cam_path} — prim invalid/missing — aborting")
                return
            # fz (the active floor's base Z) and upf (units-per-foot) do NOT
            # change while you drag the height slider — but computing them
            # calls get_floor_info(), which walks the ENTIRE stage and world-
            # bounds every Boundable prim. That per-frame full-stage traversal
            # was the height-joystick lag. Compute once at the start of the
            # drag (invalidated in _jsv_pressed) and reuse.
            if self._jsv_cached_fz is None:
                get_floor_info = self._ds.get("get_floor_info") if self._ds else None
                if not get_floor_info:
                    print(f"[JSV] move: no get_floor_info in self._ds — aborting")
                    return
                _cx, _cy, fz, _w, _d, _unit, tf = get_floor_info()
                upf = tf / 10.0
                # Floor-aware fz — without this a floor-3 camera's height was
                # written relative to the ground and snapped back to floor 1.
                try:
                    cam_floor = (self._ds.get("cam_floor") if self._ds else None) or {}
                    from planlift_floorview.extension import _INSTANCE as _fpv_jsv
                    _fidx = cam_floor.get(cam_path)
                    if _fidx is None and _fpv_jsv is not None:
                        _cur_t = UsdGeom.XformCommonAPI(prim).GetXformVectors(Usd.TimeCode.Default())[0]
                        _fidx = _fpv_jsv._floor_idx_for_z(_cur_t[2])
                    if _fidx is not None and _fpv_jsv is not None:
                        fz = _fpv_jsv._floor_z(_fidx)
                except Exception as _e:
                    print(f"[JSV] move: floor-z lookup failed, using ground fz: {_e}")
                _xy = list(UsdGeom.XformCommonAPI(prim).GetXformVectors(Usd.TimeCode.Default())[0])[:2]
                self._jsv_cached_fz, self._jsv_cached_upf = fz, upf
                self._jsv_cached_xy = _xy
            fz, upf = self._jsv_cached_fz, self._jsv_cached_upf

            # Absolute set — the slider's thumb position IS the camera's
            # height. X/Y are frozen for the drag (cached above), so nothing
            # is read from USD per frame; only Z changes.
            t = [self._jsv_cached_xy[0], self._jsv_cached_xy[1],
                 fz + self._jsv_height_ft * upf]
            # Height-only — write just translate, no rotate, no per-frame
            # readback/print. See _joystick_move_camera: Kit's Perspective
            # camera needs the session layer targeted or the write is
            # silently overridden.
            if cam_path.startswith("/OmniverseKit_"):
                with Usd.EditContext(stage, Usd.EditTarget(stage.GetSessionLayer())):
                    UsdGeom.XformCommonAPI(prim).SetTranslate(Gf.Vec3d(*t))
            else:
                UsdGeom.XformCommonAPI(prim).SetTranslate(Gf.Vec3d(*t))
        except Exception as e:
            print(f"[JSV] {e}")

    # ── Rotation joystick (Pitch X / Roll Z — Yaw Y not controlled) ────────

    def _on_jsr_drag_update(self, _):
        if not self._jsr_active:
            self._jsr_drag_sub = None
            return
        # Check left mouse button still held (mirrors _on_js_drag_update)
        try:
            import carb.input as ci
            import omni.appwindow
            iface = ci.acquire_input_interface()
            mouse = omni.appwindow.get_default_app_window().get_mouse()
            try:
                held = float(iface.get_mouse_value(mouse, ci.MouseInput.LEFT_BUTTON)) >= 0.5
            except Exception:
                held = bool(int(iface.get_mouse_button_flags(
                    mouse, ci.MouseInput.LEFT_BUTTON)) & 0x1)
        except Exception:
            held = True
        if not held:
            self._jsr_active = False
            self._jsr_dx = 0.0
            self._jsr_dy = 0.0
            try:
                self._jsr_placer.offset_x = self._jsr_cx - self._jsr_thumb_r
                self._jsr_placer.offset_y = self._jsr_cx - self._jsr_thumb_r
            except Exception:
                pass
            self._jsr_drag_sub = None
            return
        # Get cursor position and compute displacement from joystick center
        try:
            sx, sy = self._js_cursor_pos()
            if sx is None:
                return
            if self._jsr_center_win is None:
                return
            cx_win, cy_win = self._jsr_center_win
            ox = sx - cx_win
            oy = sy - cy_win
            max_r = self._jsr_max_r
            ox = max(-max_r, min(max_r, ox))
            oy = max(-max_r, min(max_r, oy))
            self._jsr_dx = ox / max_r if max_r > 0 else 0.0
            self._jsr_dy = oy / max_r if max_r > 0 else 0.0
            # Reposition thumb
            self._jsr_placer.offset_x = self._jsr_cx + ox - self._jsr_thumb_r
            self._jsr_placer.offset_y = self._jsr_cx + oy - self._jsr_thumb_r
            # Rotate camera
            self._jsr_rotate_camera()
        except Exception as e:
            print(f"[JSR] drag update: {e}")

    def _jsr_rotate_camera(self):
        from pxr import Gf, UsdGeom, Usd
        dx = self._jsr_dx   # horizontal drag -> Roll Z
        dy = self._jsr_dy   # vertical drag   -> Pitch X
        if abs(dx) < 0.02 and abs(dy) < 0.02:
            return
        try:
            cam_path = self._js_cam_getter()
            if not cam_path:
                return
            stage = self._js_stage_getter()
            if stage is None:
                return
            # Tilt/roll live on the actual Camera prim, not the rig (which
            # only ever holds translate + yaw) — see _cam_optical_path.
            optical_path = (self._ds["cam_optical_path"](cam_path)
                            if self._ds else cam_path)
            prim = stage.GetPrimAtPath(optical_path)
            if not prim or not prim.IsValid():
                return

            ROT_DEG_PER_FRAME = 1.5  # sensitivity at full deflection

            vecs = UsdGeom.XformCommonAPI(prim).GetXformVectors(Usd.TimeCode.Default())
            t = list(vecs[0])
            r = list(vecs[1])

            # dy negative = dragged up = pitch up (mirrors the position joystick's
            # "up = forward" convention). r[0] stores pitch with a +90 offset baked in
            # (90 = level), matching the old Pitch slider's convention.
            pitch = max(-90.0, min(90.0, (r[0] - 90.0) + (-dy) * ROT_DEG_PER_FRAME))
            # Roll is unclamped — full 360° continuous rotation, wrapped to (-180, 180]
            # so the accumulated value doesn't grow unbounded over a long drag session.
            roll = r[2] + (-dx) * ROT_DEG_PER_FRAME
            roll = ((roll + 180.0) % 360.0) - 180.0
            r[0] = pitch + 90.0
            r[2] = roll
            # r[1] (Yaw Y) intentionally untouched

            # Live readout — shows the camera's actual absolute pitch/roll
            # angles (not the joystick's own -1..1 deflection like the other
            # two pads), since a raw angle is what's actually meaningful
            # here. Left showing the last value on release rather than
            # resetting to 0 — the camera doesn't snap back to level when
            # you let go of the stick, so 0 would be misleading.
            try:
                self._jsr_coord_lbl.text = f"Tilt: {pitch:+.1f}°\nRoll: {roll:+.1f}°"
            except Exception:
                pass

            # Rotation-only joystick — position is unchanged, so only write
            # rotate. See _joystick_move_camera: Kit's Perspective camera
            # needs the session layer targeted or the write is overridden.
            if cam_path.startswith("/OmniverseKit_"):
                with Usd.EditContext(stage, Usd.EditTarget(stage.GetSessionLayer())):
                    UsdGeom.XformCommonAPI(prim).SetRotate(Gf.Vec3f(*r), UsdGeom.XformCommonAPI.RotationOrderXYZ)
            else:
                UsdGeom.XformCommonAPI(prim).SetRotate(Gf.Vec3f(*r), UsdGeom.XformCommonAPI.RotationOrderXYZ)
        except Exception as e:
            print(f"[JSR] {e}")

    # ── Quiet viewport-pixel conversion (no console output) ──────────────

    def _screen_to_vp_px(self, screen_x: float, screen_y: float):
        """Convert absolute screen coords → viewport-local pixels.
        Returns (-1,-1) if outside viewport. No console output (called per frame)."""
        try:
            import ctypes
            import omni.appwindow
            import omni.kit.viewport.utility as vp_util

            class _PT(ctypes.Structure):
                _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]

            aw    = omni.appwindow.get_default_app_window()
            title = aw.get_title() if hasattr(aw, "get_title") else ""
            hwnd  = ctypes.windll.user32.FindWindowW(None, title or None)
            if not hwnd:
                hwnd = ctypes.windll.user32.GetForegroundWindow()
            pt2 = _PT(0, 0)
            ctypes.windll.user32.ClientToScreen(hwnd, ctypes.byref(pt2))
            kit_x, kit_y = float(pt2.x), float(pt2.y)

            vp_win = vp_util.get_active_viewport_window()
            if vp_win is None:
                return -1.0, -1.0

            rel_x = screen_x - kit_x
            rel_y = screen_y - kit_y
            px = rel_x - float(vp_win.position_x)
            py = rel_y - float(vp_win.position_y)
            vp_w = float(vp_win.width)
            vp_h = float(vp_win.height)
            if px < 0 or py < 0 or px >= vp_w or py >= vp_h:
                return -1.0, -1.0
            return px, py
        except Exception:
            return -1.0, -1.0

    # ── Combined scene/floor-plane raycast ────────────────────────────────

    def _raycast_scene_or_floor(self, screen_x: float, screen_y: float):
        """
        Build a camera ray from the viewport pixel, then:
          1. Try AABB intersection against cached scene geometry.
          2. Fall back to floor-plane (Z = floor_z) with bounds clamping.
        Returns (wx, wy, wz) or None.
        """
        try:
            import omni.kit.viewport.utility as vp_util
            from pxr import Gf

            vp_win = vp_util.get_active_viewport_window()
            if vp_win is None:
                return None
            vp_api  = vp_win.viewport_api
            res     = vp_api.resolution
            w, h    = float(res[0]), float(res[1])

            view     = vp_api.view
            proj     = vp_api.projection
            inv_view = view.GetInverse()
            inv_proj = proj.GetInverse()

            cam_pos = inv_view.ExtractTranslation()

            ndc_x =  2.0 * screen_x / w - 1.0
            ndc_y = -2.0 * screen_y / h + 1.0

            def rmul(vec, mat):
                return tuple(sum(vec[i] * mat[i][j] for i in range(4)) for j in range(4))

            cx, cy, cz, cw = rmul((ndc_x, ndc_y, 1.0, 1.0), inv_proj)
            if abs(cw) > 1e-9:
                cx /= cw; cy /= cw; cz /= cw
            wx, wy, wz, _ = rmul((cx, cy, cz, 1.0), inv_view)

            ray_dir = (Gf.Vec3d(wx, wy, wz) - cam_pos).GetNormalized()
            ox = float(cam_pos[0]); oy = float(cam_pos[1]); oz = float(cam_pos[2])
            dx = float(ray_dir[0]); dy = float(ray_dir[1]); dz = float(ray_dir[2])

            # ── 1. AABB scene raycast ─────────────────────────────────────
            if self._drag_aabbs:
                hit = self._raycast_aabbs(ox, oy, oz, dx, dy, dz)
                if hit is not None:
                    return hit

            # ── 2. Floor-plane fallback with scene-bounds clamping ────────
            floor_z = self._drag_floor_z
            if abs(dz) < 1e-9:
                return None
            t = (floor_z - oz) / dz
            if t <= 0:
                return None
            hx = ox + t * dx
            hy = oy + t * dy
            # Clamp: reject if more than 1.5× scene size from centre
            limit = self._drag_scene_size * 1.5
            cx_s  = self._drag_scene_cx
            cy_s  = self._drag_scene_cy
            if abs(hx - cx_s) > limit or abs(hy - cy_s) > limit:
                return None
            return (hx, hy, floor_z)

        except Exception as e:
            print(f"[FPT] _raycast_scene_or_floor: {e}")
            return None

    # ── Ghost cone prim ───────────────────────────────────────────────────

    def _find_camera_body_usd(self) -> "str | None":
        """Walk Isaac Sim extension directories looking for camera_body.usd.
        No name filter — checks every extension subdir. Prints [FPT-BODY] lines."""
        import os

        # Relative paths to try inside each extension directory
        _REL = [
            "data/sensors/camera/camera_body.usd",
            "data/camera/camera_body.usd",
            "omni/isaac/sensor/data/sensors/camera/camera_body.usd",
            "data/sensors/camera.usd",
            "data/camera.usd",
        ]

        def _probe(ext_path: str) -> "str | None":
            """Check known relative paths, then walk data/ for any camera*.usd."""
            for rel in _REL:
                cand = f"{ext_path}/{rel}"
                if os.path.exists(cand):
                    return cand
            data_dir = f"{ext_path}/data"
            if os.path.isdir(data_dir):
                for root, _, files in os.walk(data_dir):
                    for f in files:
                        if f.endswith(".usd") and "camera" in f.lower() and "body" in f.lower():
                            return f"{root}/{f}".replace("\\", "/")
            return None

        # 1. Extension manager (if already enabled)
        try:
            import omni.kit.app
            mgr    = omni.kit.app.get_app().get_extension_manager()
            ext_id = mgr.get_enabled_extension_id("omni.isaac.sensor")
            if ext_id:
                p = mgr.get_extension_path(ext_id).replace("\\", "/").rstrip("/")
                print(f"[FPT-BODY] mgr path: {p}")
                r = _probe(p)
                if r:
                    print(f"[FPT-BODY] FOUND: {r}")
                    return r
        except Exception:
            pass

        # 2. Collect extension folder candidates from kit path upward (3 levels)
        ext_dirs = []
        try:
            import carb.tokens
            kit_path = carb.tokens.get_tokens_interface().resolve("${kit}") \
                           .replace("\\", "/").rstrip("/")
            print(f"[FPT-BODY] kit: {kit_path}")
            # The real asset — confirmed to exist on this machine at
            # <kit>/resources/models/camera/camera.usd. Not under any
            # extension's own data/ folder (every _REL candidate above is
            # extension-relative), so the extension-dir walk below would
            # never have found it regardless of which extension happened
            # to be enabled.
            kit_cam = f"{kit_path}/resources/models/camera/camera.usd"
            if os.path.exists(kit_cam):
                print(f"[FPT-BODY] FOUND (kit resources): {kit_cam}")
                return kit_cam
            d = kit_path
            for _ in range(3):
                for sub in ("exts", "extscache", "extsDeprecated", "extsPhysics"):
                    if os.path.isdir(f"{d}/{sub}"):
                        ext_dirs.append(f"{d}/{sub}")
                parent = d.rsplit("/", 1)[0]
                if parent == d:
                    break
                d = parent
        except Exception as e:
            print(f"[FPT-BODY] token: {e}")

        # 3. User folder from kit config
        user = "D:/Piyush/isaac_extensions"
        if os.path.isdir(user):
            ext_dirs.append(user)

        # Search every subdirectory in each ext_dir (no name filter)
        for ext_dir in ext_dirs:
            try:
                entries = [e for e in os.listdir(ext_dir) if os.path.isdir(f"{ext_dir}/{e}")]
                sensor_hits = [e for e in entries if "sensor" in e.lower() or "camera" in e.lower()]
                print(f"[FPT-BODY] {ext_dir}: {len(entries)} dirs  sensor/cam: {sensor_hits}")
                for entry in entries:
                    full = f"{ext_dir}/{entry}"
                    r = _probe(full)
                    if r:
                        print(f"[FPT-BODY] FOUND: {r}")
                        return r
            except Exception as e:
                print(f"[FPT-BODY] scan error {ext_dir}: {e}")

        # Last-resort: list what IS in D:/Piyush/isaac_extensions so we can see what's there
        user = "D:/Piyush/isaac_extensions"
        if os.path.isdir(user):
            try:
                items = sorted(os.listdir(user))
                print(f"[FPT-BODY] user dir ({len(items)} items): {items[:20]}")
            except Exception:
                pass
        else:
            print(f"[FPT-BODY] user dir does not exist: {user}")

        print("[FPT-BODY] camera_body.usd not found — camera will show frustum only")
        return None

    def _attach_camera_body(self, stage, cam_path: str):
        """Reverted — see this function's own git history / conversation:
        every camera on this install already has a visible representation
        (Kit's own built-in camera icon/gizmo, not a referenced asset —
        confirmed by the Stage tree showing no CameraModel child under any
        existing camera, before any of this). Adding a referenced or
        procedural body here just draws a SECOND thing on top of that
        built-in icon instead of replacing it. Left as a no-op on purpose
        — the actual fix for "the camera looks tiny" belongs wherever
        Kit's own built-in camera gizmo size is controlled, not here."""
        pass

    def _update_ghost_camera(self, world_pos):
        """Create (once) or move the ghost Camera prim during drag.
        Z is locked to the cam height from Floor Plan View so the ghost tracks
        XY freely but never changes altitude. Subsequent calls only update
        translate so updates are cheap."""
        if not self._ds:
            return
        try:
            # Override Z: use FPV cam height for whichever floor this
            # raycast position actually landed on (ignore raycast Z itself,
            # but its floor still decides which floor's spawn height
            # applies) — keeps the ghost's preview height matching what
            # _spawn_camera_at_pixel will actually commit on drop.
            _floor_idx = self._fpv_floor_idx_for_z(world_pos[2])
            _fpv_fz, _fpv_h = self._fpv_cam_height(_floor_idx)
            if _fpv_fz is not None:
                world_pos = (world_pos[0], world_pos[1], _fpv_fz + _fpv_h)
        except Exception:
            pass
        try:
            from pxr import UsdGeom, Gf
            stage = self._ds["ctx"].get_stage()
            if not stage:
                return

            ghost_path = f"{self._ds['CAMS_PATH']}/_GhostCamera"
            prim = stage.GetPrimAtPath(ghost_path)

            if not prim.IsValid():
                # ── First call: create the ghost Camera prim ──────────────────
                import omni.kit.commands
                self._ds["ensure"](self._ds["CAMS_PATH"])
                omni.kit.commands.execute(
                    "CreatePrimWithDefaultXformCommand",
                    prim_type="Camera",
                    prim_path=ghost_path,
                    select_new_prim=False)
                prim = stage.GetPrimAtPath(ghost_path)
                if prim.IsValid():
                    pidx = getattr(self, '_drag_preset_idx', 0)
                    ps   = self._ds["CAM_PRESETS"][self._ds["PRESET_KEYS"][pidx]]
                    cam  = UsdGeom.Camera(prim)
                    cam.GetFocalLengthAttr().Set(ps["focal_mm"])
                    cam.GetHorizontalApertureAttr().Set(ps["h_ap"])
                    cam.GetVerticalApertureAttr().Set(ps["v_ap"])
                    UsdGeom.Xformable(prim).ClearXformOpOrder()
                    xf = UsdGeom.XformCommonAPI(prim)
                    xf.SetTranslate(Gf.Vec3d(*world_pos))
                    xf.SetRotate(Gf.Vec3f(90, 0, 0),
                                 UsdGeom.XformCommonAPI.RotationOrderXYZ)
                    self._attach_camera_body(stage, ghost_path)
                self._ghost_cone_path = ghost_path
            else:
                # ── Subsequent calls: only update position (cheap) ────────────
                UsdGeom.XformCommonAPI(prim).SetTranslate(
                    Gf.Vec3d(float(world_pos[0]),
                             float(world_pos[1]),
                             float(world_pos[2])))

        except Exception as e:
            print(f"[FPT] _update_ghost_camera: {e}")

    def _update_ghost_light(self, world_pos):
        """Create (once) or move the ghost RectLight prim during drag.
        Z is locked to the fixed ceiling spawn height (lights are always
        ceiling-mounted, not placed wherever the cursor raycasts to)."""
        if not self._ds:
            return
        try:
            _, _, fz_, _, _, _, tf_ = self._ds["get_floor_info"]()
            units_per_foot = tf_ / 10.0
            world_pos = (world_pos[0], world_pos[1],
                         fz_ + self._LIGHT_SPAWN_HEIGHT_FT * units_per_foot)
        except Exception:
            pass
        try:
            from pxr import UsdGeom, UsdLux, Gf
            stage = self._ds["ctx"].get_stage()
            if not stage:
                return

            ghost_path = f"{self._ds['IND_LIGHTS_PATH']}/_GhostLight"
            prim = stage.GetPrimAtPath(ghost_path)

            if not prim.IsValid():
                # ── First call: create the ghost RectLight prim ───────────────
                self._ds["ensure"](self._ds["IND_LIGHTS_PATH"])
                UsdLux.RectLight.Define(stage, ghost_path)
                prim = stage.GetPrimAtPath(ghost_path)
                if prim.IsValid():
                    set_attr = self._ds["set_attr"]
                    Sdf = self._ds["Sdf"]
                    set_attr(prim, "width", float(self._ds["sl_lwidth"].model.get_value_as_float()), Sdf.ValueTypeNames.Float)
                    set_attr(prim, "height", float(self._ds["sl_ldepth"].model.get_value_as_float()), Sdf.ValueTypeNames.Float)
                    set_attr(prim, "intensity", float(self._ds["sl_intensity"].model.get_value_as_float()), Sdf.ValueTypeNames.Float)
                    set_attr(prim, "colorTemperature", float(self._ds["sl_temp"].model.get_value_as_float()), Sdf.ValueTypeNames.Float)
                    set_attr(prim, "enableColorTemperature", True, Sdf.ValueTypeNames.Bool)
                    set_attr(prim, "color", Gf.Vec3f(1, 1, 1), Sdf.ValueTypeNames.Color3f)
                    UsdGeom.Xformable(prim).ClearXformOpOrder()
                    xf = UsdGeom.XformCommonAPI(prim)
                    xf.SetTranslate(Gf.Vec3d(*world_pos))
                    xf.SetRotate(Gf.Vec3f(0, 0, 0), UsdGeom.XformCommonAPI.RotationOrderXYZ)
                self._ghost_light_path = ghost_path
            else:
                # ── Subsequent calls: only update position (cheap) ────────────
                UsdGeom.XformCommonAPI(prim).SetTranslate(
                    Gf.Vec3d(float(world_pos[0]),
                             float(world_pos[1]),
                             float(world_pos[2])))

        except Exception as e:
            print(f"[FPT] _update_ghost_light: {e}")

    def _destroy_ghost_cone(self):
        """Remove the preview cone/light from the stage (whichever is set)."""
        if not self._ds:
            self._ghost_cone_path  = None
            self._ghost_light_path = None
            return
        try:
            import omni.kit.commands
            stage = self._ds["ctx"].get_stage()
            for attr in ("_ghost_cone_path", "_ghost_light_path"):
                path = getattr(self, attr, None)
                if not path:
                    continue
                setattr(self, attr, None)
                if stage and stage.GetPrimAtPath(path).IsValid():
                    omni.kit.commands.execute("DeletePrims", paths=[path])
        except Exception:
            pass

    # ── Drop callbacks ────────────────────────────────────────────────────

    def _on_external_drop(self, payload: str, x: float, y: float):
        """Strategy A callback: viewport add_external_drag_drop_support."""
        print(f"[FPT-A] external drop  payload='{payload}'  pos=({x:.0f},{y:.0f})")
        if payload != self._DRAG_TOKEN:
            return False
        self._drag_active = False
        spawn_fn = self._spawn_light_at_pixel if self._drag_kind == "light" else self._spawn_camera_at_pixel
        spawn_fn(x, y)
        return True

    def _on_frame_drop(self, payload: str, x: float = -1.0, y: float = -1.0):
        """Strategy B callback: viewport frame set_drop_fn.
        Kit 107.x calls this as fn(payload) with no x,y — we use the screen
        position cached by _on_update_drag (main thread) instead."""
        try:
            print(f"[FPT-B] frame drop  repr={repr(payload)}  pos=({x:.0f},{y:.0f})")
            if str(payload).strip() != self._DRAG_TOKEN:
                print(f"[FPT-B] payload mismatch — expected {self._DRAG_TOKEN!r}")
                return
            self._drag_active = False

            if x < 0 or y < 0:
                # Use the cursor position polled on the main update thread
                sx, sy = getattr(self, '_drag_screen_pos', (-1.0, -1.0))
                print(f"[FPT-B] cached screen pos ({sx:.0f},{sy:.0f})")
                if sx < 0:
                    print("[FPT-B] no cached cursor pos — drop ignored"); return
                x, y = self._screen_to_viewport_local(sx, sy)
                print(f"[FPT-B] viewport local ({x:.0f},{y:.0f})")
                if x < 0:
                    print("[FPT-B] cursor outside viewport — drop ignored"); return

            spawn_fn = self._spawn_light_at_pixel if self._drag_kind == "light" else self._spawn_camera_at_pixel
            spawn_fn(x, y)
        except Exception as e:
            import traceback
            print(f"[FPT-B] handler error: {e}")
            traceback.print_exc()

    def _screen_to_viewport_local(self, screen_x: float, screen_y: float):
        """
        Convert absolute screen coordinates → viewport-local pixel coordinates.
        Returns (-1,-1) if conversion fails or cursor is outside the viewport.
        Runs on the main thread so prints appear in the Kit console.
        """
        try:
            import ctypes
            import omni.appwindow
            import omni.kit.viewport.utility as vp_util

            class _PT(ctypes.Structure):
                _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]

            # ── Kit client-area origin (screen coords) ─────────────────────
            kit_x, kit_y = 0.0, 0.0
            try:
                aw    = omni.appwindow.get_default_app_window()
                title = aw.get_title() if hasattr(aw, "get_title") else ""
                hwnd  = ctypes.windll.user32.FindWindowW(None, title or None)
                if not hwnd:
                    hwnd = ctypes.windll.user32.GetForegroundWindow()
                pt2 = _PT(0, 0)
                ctypes.windll.user32.ClientToScreen(hwnd, ctypes.byref(pt2))
                kit_x = float(pt2.x)
                kit_y = float(pt2.y)
                print(f"[FPT] Kit client origin ({kit_x:.0f},{kit_y:.0f})")
            except Exception as e_win:
                print(f"[FPT] HWND lookup failed ({e_win}) — assuming (0,0)")

            # ── Viewport bounds (Kit-client-relative) ──────────────────────
            rel_x = screen_x - kit_x
            rel_y = screen_y - kit_y

            vp_win = vp_util.get_active_viewport_window()
            if vp_win is None:
                print("[FPT] no active viewport"); return -1.0, -1.0

            vp_x = float(vp_win.position_x); vp_y = float(vp_win.position_y)
            vp_w = float(vp_win.width);       vp_h = float(vp_win.height)
            print(f"[FPT] viewport ({vp_x:.0f},{vp_y:.0f}) {vp_w:.0f}×{vp_h:.0f}"
                  f"  kit-rel cursor ({rel_x:.0f},{rel_y:.0f})")

            px = rel_x - vp_x
            py = rel_y - vp_y
            if px < 0 or py < 0 or px >= vp_w or py >= vp_h:
                print(f"[FPT] cursor ({px:.0f},{py:.0f}) outside viewport bounds")
                return -1.0, -1.0
            return px, py

        except Exception as e:
            import traceback
            print(f"[FPT] _screen_to_viewport_local error: {e}")
            traceback.print_exc()
            return -1.0, -1.0

    def _on_carb_mouse(self, event, *_):
        """Strategy C callback: carb.input mouse events."""
        try:
            import carb.input as ci
            # Handle both LEFT_BUTTON_UP and BUTTON_UP naming variants
            btn_up = getattr(ci.MouseEventType, "LEFT_BUTTON_UP",
                     getattr(ci.MouseEventType, "BUTTON_UP", None))
            if btn_up is None or event.type != btn_up:
                return
        except Exception:
            return

        if not self._drag_active:
            return
        self._drag_active = False
        print("[FPT-C] carb.input mouse-up during drag")

        try:
            import omni.appwindow
            import omni.kit.viewport.utility as vp_util

            aw    = omni.appwindow.get_default_app_window()
            win_w = float(aw.get_width())
            win_h = float(aw.get_height())

            # Try common attribute names for normalised cursor position
            nc = (getattr(event, "normalized_coords", None) or
                  getattr(event, "normalizedCoords",  None) or
                  getattr(event, "normalized_pos",    None))
            if nc is None:
                print("[FPT-C] cannot read cursor position from event"); return
            abs_x = nc.x * win_w
            abs_y = nc.y * win_h

            vp_win = vp_util.get_active_viewport_window()
            if vp_win is None: return

            vp_x = float(vp_win.position_x); vp_y = float(vp_win.position_y)
            vp_w = float(vp_win.width);       vp_h = float(vp_win.height)
            if not (vp_x <= abs_x <= vp_x + vp_w and
                    vp_y <= abs_y <= vp_y + vp_h):
                print(f"[FPT-C] cursor ({abs_x:.0f},{abs_y:.0f}) outside viewport"); return

            px = abs_x - vp_x;  py = abs_y - vp_y
            print(f"[FPT-C] viewport-local pos ({px:.0f},{py:.0f})")
            spawn_fn = self._spawn_light_at_pixel if self._drag_kind == "light" else self._spawn_camera_at_pixel
            spawn_fn(px, py)

        except Exception as e:
            import traceback
            print(f"[FPT-C] error: {e}"); traceback.print_exc()

    def _spawn_camera_at_pixel(self, pixel_x: float, pixel_y: float):
        """Use the ghost's last computed position (or fresh raycast) to place a camera."""
        ds = self._ds
        if not ds:
            print("[FPT] drop state not initialised"); return

        set_status = ds["set_status"]
        set_status("Placing camera…")

        # Ghost pre-computed position is already at exact scene surface
        world_pos = self._ghost_last_pos
        self._ghost_last_pos = None
        self._destroy_ghost_cone()

        try:
            from pxr import UsdGeom, Gf, Sdf

            # If no ghost position (e.g. drag was too fast), do a fresh raycast
            if world_pos is None:
                world_pos = self._raycast_scene_or_floor(pixel_x, pixel_y)
            if world_pos is None:
                set_status("Ray missed scene — drop closer to the model")
                return
            wx, wy, wz = world_pos
            # Infer which floor this raycast/ghost position actually landed
            # on from its raw Z (before the "lock to floor base + spawn
            # height" override below overwrites it) — so a camera dropped
            # visibly onto floor 2's geometry gets tagged floor 2 even
            # though its final height then gets locked to that floor's own
            # spawn height, same as before this multi-floor support existed.
            _floor_idx = self._fpv_floor_idx_for_z(wz)
            # Lock Z to the cam height set in Floor Plan View for THIS floor
            # (ignore raycast Z)
            _fpv_fz, _fpv_h = self._fpv_cam_height(_floor_idx)
            if _fpv_fz is not None:
                wz = _fpv_fz + _fpv_h

            # 2. Unique prim path
            ctx       = ds["ctx"]
            stage     = ctx.get_stage()
            CAMS_PATH = ds["CAMS_PATH"]
            ds["ensure"](CAMS_PATH)

            pidx      = getattr(self, '_drag_preset_idx', 0)
            ps        = ds["CAM_PRESETS"][ds["PRESET_KEYS"][pidx]]
            idx       = ds["cam_count"][0]; ds["cam_count"][0] += 1
            base_path = f"{CAMS_PATH}/Cam_Drop_{idx:02d}_{ps['label']}"
            try:
                import omni.usd as ousd
                cam_path = ousd.get_stage_next_free_path(stage, base_path, False)
            except Exception:
                cam_path = base_path
            optical_path = cam_path + "/Camera"

            # Rig: an Xform holding world position and yaw (rotateZ) only —
            # see _cam_optical_path. Keeping yaw separate from tilt/roll
            # means panning this camera later can never bleed into them.
            stage.DefinePrim(cam_path, "Xform")
            rig_prim = stage.GetPrimAtPath(cam_path)
            UsdGeom.Xformable(rig_prim).ClearXformOpOrder()
            rig_xf = UsdGeom.XformCommonAPI(rig_prim)
            rig_xf.SetTranslate(Gf.Vec3d(wx, wy, wz))
            rig_xf.SetRotate(Gf.Vec3f(0, 0, 0), UsdGeom.XformCommonAPI.RotationOrderXYZ)

            # 3. Create Camera prim via Kit command (includes built-in visual body)
            import omni.kit.commands
            omni.kit.commands.execute("CreatePrimWithDefaultXformCommand",
                                      prim_type="Camera",
                                      prim_path=optical_path,
                                      select_new_prim=False)
            prim = stage.GetPrimAtPath(optical_path)
            if not prim.IsValid():
                UsdGeom.Camera.Define(stage, optical_path)   # fallback
                prim = stage.GetPrimAtPath(optical_path)
            cam = UsdGeom.Camera(prim)
            cam.GetFocalLengthAttr().Set(ps["focal_mm"])
            cam.GetHorizontalApertureAttr().Set(ps["h_ap"])
            cam.GetVerticalApertureAttr().Set(ps["v_ap"])
            cam.GetClippingRangeAttr().Set(Gf.Vec2f(0.001, 1_000_000.0))
            prim.CreateAttribute("info:res_w", Sdf.ValueTypeNames.Int).Set(ps["res_w"])
            prim.CreateAttribute("info:res_h", Sdf.ValueTypeNames.Int).Set(ps["res_h"])
            # Clear default xform ops from the command, then apply our placement.
            # Translate stays at the rig's origin — position lives on the rig.
            UsdGeom.Xformable(prim).ClearXformOpOrder()
            xf = UsdGeom.XformCommonAPI(prim)
            xf.SetTranslate(Gf.Vec3d(0, 0, 0))
            xf.SetRotate(Gf.Vec3f(90, 0, 0),
                         UsdGeom.XformCommonAPI.RotationOrderXYZ)
            self._attach_camera_body(stage, optical_path)
            ds["cam_list"].append(cam_path)
            ds["cam_colors"][cam_path] = CAM_COLORS[idx % len(CAM_COLORS)]
            ds["cam_floor"][cam_path] = _floor_idx
            ds["schedule_rebuild"]()   # refresh the camera list UI panel
            # Stay in perspective view so the user can see the physical camera body.
            # Clicking its icon in the Floor Plan panel switches to its POV.
            name = cam_path.split("/")[-1]
            set_status(f"Camera placed  ({wx:.2f}, {wy:.2f}, {wz:.2f})  —  {name}")
            print(f"[FPT] Spawned: {cam_path}")

        except Exception as e:
            import traceback
            print(f"[FPT] _spawn_camera_at_pixel error: {e}")
            traceback.print_exc()
            try: ds["set_status"](f"Error: {e}")
            except: pass

    def _spawn_light_at_pixel(self, pixel_x: float, pixel_y: float):
        """Use the ghost's last computed position (or fresh raycast) to place
        an individual light. Z is always the fixed ceiling spawn height."""
        ds = self._ds
        if not ds:
            print("[FPT] drop state not initialised"); return

        set_status = ds["set_status"]
        set_status("Placing light…")

        world_pos = self._ghost_last_pos
        self._ghost_last_pos = None
        self._destroy_ghost_cone()

        try:
            from pxr import UsdGeom, UsdLux, Gf, Sdf

            if world_pos is None:
                world_pos = self._raycast_scene_or_floor(pixel_x, pixel_y)
            if world_pos is None:
                set_status("Ray missed scene — drop closer to the model")
                return
            wx, wy, _ = world_pos
            _, _, fz, _, _, _, tf = ds["get_floor_info"]()
            wz = fz + self._LIGHT_SPAWN_HEIGHT_FT * (tf / 10.0)

            ctx = ds["ctx"]
            stage = ctx.get_stage()
            IND_LIGHTS_PATH = ds["IND_LIGHTS_PATH"]
            ds["ensure"](IND_LIGHTS_PATH)

            idx = ds["ind_light_count"][0]; ds["ind_light_count"][0] += 1
            base_path = f"{IND_LIGHTS_PATH}/Light_{idx:02d}"
            try:
                import omni.usd as ousd
                light_path = ousd.get_stage_next_free_path(stage, base_path, False)
            except Exception:
                light_path = base_path

            UsdLux.RectLight.Define(stage, light_path)
            prim = stage.GetPrimAtPath(light_path)
            set_attr = ds["set_attr"]
            # Normal local fixture size — NOT the scene-sized sliders (see
            # add_individual_light). Editable per-light afterwards.
            _ilsz = self._IND_LIGHT_DEFAULT_FT * (tf / 10.0)
            set_attr(prim, "width", _ilsz, Sdf.ValueTypeNames.Float)
            set_attr(prim, "height", _ilsz, Sdf.ValueTypeNames.Float)
            set_attr(prim, "intensity", float(ds["sl_intensity"].model.get_value_as_float()), Sdf.ValueTypeNames.Float)
            set_attr(prim, "exposure", 0.0, Sdf.ValueTypeNames.Float)
            set_attr(prim, "colorTemperature", float(ds["sl_temp"].model.get_value_as_float()), Sdf.ValueTypeNames.Float)
            set_attr(prim, "enableColorTemperature", True, Sdf.ValueTypeNames.Bool)
            set_attr(prim, "color", Gf.Vec3f(1, 1, 1), Sdf.ValueTypeNames.Color3f)
            UsdGeom.Xformable(prim).ClearXformOpOrder()
            xf = UsdGeom.XformCommonAPI(prim)
            xf.SetTranslate(Gf.Vec3d(wx, wy, wz))
            xf.SetRotate(Gf.Vec3f(0, 0, 0), UsdGeom.XformCommonAPI.RotationOrderXYZ)

            ds["ind_light_list"].append(light_path)
            ds["schedule_ind_light_list_rebuild"]()
            # Floor Plan View only auto-refreshes on camera-state polling —
            # lights aren't polled, so a viewport drop needs an explicit nudge
            # or its marker wouldn't appear until something else triggers a redraw.
            try:
                from planlift_floorview.extension import _INSTANCE as _fpv
                if _fpv is not None:
                    _fpv._schedule_rebuild()
            except Exception:
                pass
            name = light_path.split("/")[-1]
            set_status(f"Light placed  ({wx:.2f}, {wy:.2f}, {wz:.2f})  —  {name}")
            print(f"[FPT] Spawned light: {light_path}")

        except Exception as e:
            import traceback
            print(f"[FPT] _spawn_light_at_pixel error: {e}")
            traceback.print_exc()
            try: ds["set_status"](f"Error: {e}")
            except: pass

