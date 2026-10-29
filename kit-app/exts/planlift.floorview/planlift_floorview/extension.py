"""PlanLift Floor View — interactive 2D floor plan with camera markers.

Interaction modes:
  1. Click a marker  → switch the 3D viewport to that camera
  2. Drag a marker   → reposition the camera in 3D world space
  3. Drop a preset from Floor Plan Tool → spawn a new camera at that position

Header controls:
  - Cam height float-drag  → move active camera to that height
  - 7 ft / 8 ft / 9 ft buttons → select spawn height (9 ft default)
"""
import asyncio
import math
import time
from pathlib import Path

import carb
import omni.ext
import omni.kit.app
import omni.ui as ui

ISAAC_TMP   = Path("D:/PlanLift-Backend/output/isaac_tmp")
_LABEL_H    = 18
_DRAG_TOKEN = "FLOORPLAN_CAMERA_DRAG"

_CAM_PRESETS = {
    "2MP  (1920x1080)": dict(label="2MP", res_w=1920, res_h=1080,
                             focal_mm=2.8, h_ap=5.37, v_ap=3.02),
    "4MP  (2560x1440)": dict(label="4MP", res_w=2560, res_h=1440,
                             focal_mm=2.8, h_ap=5.60, v_ap=3.15),
    "8MP  (3840x2160)": dict(label="8MP", res_w=3840, res_h=2160,
                             focal_mm=2.8, h_ap=7.20, v_ap=5.40),
}
_PRESET_KEYS = list(_CAM_PRESETS.keys())

_ICON_SIZE   = 24   # camera body reference dimension (px)
_ICON_CANVAS = 72   # full PIL canvas (3× for rotated-arrow clearance)
_LIGHT_ICON_SIZE = 16   # individual-light marker (plain square, no PIL icon)
_ROT_RING_R  = 50   # rotation-gizmo ring radius (px) around the active camera's marker —
                     # MUST stay clearly bigger than half of _ICON_CANVAS (72), otherwise
                     # the ring's hit-box sits entirely underneath the icon's own hit-box
                     # and never receives any clicks at all (every "ring" click silently
                     # falls through to the icon's position-drag handler instead)

# Module-level instance so Floor Plan Tool can read _spawn_height_ft and helpers.
_INSTANCE = None  # type: PlanLiftFloorViewExtension | None


def set_floor_images(mapping):
    """Called once per Isaac Sim launch, after furniture injection succeeds
    (mirrors planlift_ui_setup.set_agent_context's timing/pattern) — hands
    over {floorKey(str): image_path} for a multi-floor building so the Floor
    Plan View window can offer a floor selector instead of always showing
    whichever single *_floorplan.png happens to be newest. Safe to call more
    than once (e.g. Kit hot-reload)."""
    inst = _INSTANCE
    if inst is None:
        carb.log_warn("[FPV] set_floor_images called before extension startup — ignored")
        return
    inst._floor_images = dict(mapping or {})
    if not inst._floor_images:
        carb.log_info("[FPV] set_floor_images: 0 floors (single-floor building)")
        return
    # Default to the lowest floor key (numeric sort when possible, matching
    # the web app's own Floor 1/Floor 2/... ordering) instead of whatever
    # the newest-file glob would otherwise have picked — unless a floor is
    # already selected and still valid (e.g. a Kit hot-reload re-delivering
    # the same mapping), in which case stay put.
    def _sort_key(k):
        try:
            return (0, float(k))
        except (TypeError, ValueError):
            return (1, str(k))
    target = inst._active_floor if inst._active_floor in inst._floor_images \
        else sorted(inst._floor_images.keys(), key=_sort_key)[0]
    inst._select_floor(target)
    carb.log_info(f"[FPV] set_floor_images: {len(inst._floor_images)} floor(s)")


class PlanLiftFloorViewExtension(omni.ext.IExt):
    WINDOW_NAME = "Floor Plan View"

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def on_startup(self, ext_id: str):
        global _INSTANCE
        _INSTANCE = self
        print("[FPV] on_startup")
        self._win              = None
        self._floorplan_path   = None
        self._fp_image_mtime   = None
        self._img_widget       = None
        self._marker_frame     = None
        self._viewport_camera  = None

        # Zoom state — markers stay a fixed pixel size regardless of zoom
        # (see _rebuild_markers/_show_floorplan); only the image + scroll
        # container grow, which is what lets the user zoom in for precise
        # marker placement without the markers ballooning along with it.
        self._fp_zoom       = 1.0
        self._fp_zoom_path  = None   # path the current zoom level applies to
        self._fp_native_ar  = 0.65   # native image height/width, updated on load
        self._fp_base_w     = 356.0  # logical width (px) at 100% zoom
        self._fp_scroll      = None
        self._zoom_label     = None

        # Pan state — left-click-drag on empty floor plan area scrolls the
        # zoomed view. Reuses the same "poll global mouse state on the app
        # update stream" idiom as marker dragging below (_on_drag_update),
        # since that's the confirmed-working pattern for drag tracking here.
        self._fp_pan_active      = False
        self._fp_pan_start_pos   = (None, None)
        self._fp_pan_start_scroll = (0.0, 0.0)
        self._fp_pan_update_sub  = None

        self._poll_tick        = 0
        self._update_sub       = None
        self._cam_spawn_count  = 0
        self._icon_seq         = 0   # increments each rebuild to bust Kit's image cache
        self._cam_state_hash   = None  # last-seen hash; skip rebuild if unchanged

        # Multi-floor state — {floorKey(str): image_path}, set by
        # set_floor_images() once api_server.py's injected script hands it
        # over (after furniture injection, same timing as
        # planlift_ui_setup.set_agent_context). Empty means "no floor
        # concept" (single-floor building) — _on_update's existing
        # newest-*_floorplan.png glob stays the fallback in that case.
        self._floor_images       = {}
        self._active_floor       = None   # currently selected floor key (str), or None

        # Height state
        self._spawn_height_ft           = 9.0
        self._ht_btns                   = {}
        self._cam_height_field          = None
        self._updating_cam_height_field = False

        # Drag-to-reposition state
        self._fp_any_dragging    = False
        self._fp_drag_cam        = None
        self._fp_drag_placer     = None
        self._fp_drag_update_sub = None
        self._fp_drag_last_t     = 0.0
        self._fp_drag_start_pos  = (None, None)  # cursor pos at press time
        self._fp_drag_moved      = False          # True once threshold crossed
        self._fp_drag_marker_size = _ICON_CANVAS  # set per-press: camera icon vs light square
        self._fp_drag_kind        = "camera"      # "camera" or "light" — Wall Collision only applies to cameras

        # Rotation gizmo state — drag the ring around the *selected* camera's
        # marker to change its yaw (only axis meaningful in a top-down view).
        self._rot_active      = False
        self._rot_cam_path    = None
        self._rot_center_win  = None   # (sx, sy) marker's screen-space center at press
        self._rot_press_angle = None   # cursor's screen angle around that center, at press
        self._rot_press_yaw   = 0.0    # rig's r[2] (yaw) at press — see _on_rotate_drag_update
        self._rot_drag_sub    = None
        self._rot_last_t      = 0.0    # throttle for the live icon re-render during drag
        self._rot_local_center_offset  = None  # camera-local offset to the body's visual center
        self._rot_target_world_center  = None  # world position that center must stay pinned to

        self._task = asyncio.ensure_future(self._deferred_startup())

    def on_shutdown(self):
        global _INSTANCE
        if _INSTANCE is self:
            _INSTANCE = None
        print("[FPV] on_shutdown")
        self._fp_drag_update_sub = None
        self._fp_pan_update_sub  = None
        self._rot_drag_sub       = None
        self._update_sub         = None
        if self._win:
            try:
                self._win.destroy()
            except Exception:
                pass
        self._win = None

    # ── Deferred startup ──────────────────────────────────────────────────────

    async def _deferred_startup(self):
        app = omni.kit.app.get_app()
        for _ in range(80):
            await app.next_update_async()
        try:
            self._build_ui()
        except Exception as e:
            import traceback
            print(f"[FPV] _build_ui error: {e}")
            traceback.print_exc()

        # Dock below Env Day Cycle Control
        for _ in range(500):
            await app.next_update_async()
            env_w = ui.Workspace.get_window("Env Day Cycle Control")
            fp_w  = ui.Workspace.get_window(self.WINDOW_NAME)
            if env_w and fp_w:
                for _ in range(20):
                    await app.next_update_async()
                env_w = ui.Workspace.get_window("Env Day Cycle Control")
                fp_w  = ui.Workspace.get_window(self.WINDOW_NAME)
                if env_w and fp_w:
                    try:
                        fp_w.dock_in(env_w, ui.DockPosition.BOTTOM, 0.4)
                        print("[FPV] docked below Env Day Cycle Control")
                    except Exception as e:
                        print(f"[FPV] dock error: {e}")
                break

        self._update_sub = (
            app.get_update_event_stream()
            .create_subscription_to_pop(self._on_update, name="planlift_floorview_poll")
        )
        asyncio.ensure_future(self._poll_markers())

    # ── UI build ──────────────────────────────────────────────────────────────

    def _build_ui(self):
        self._win = ui.Window(self.WINDOW_NAME, width=380, height=460)
        self._win.set_visibility_changed_fn(self._on_win_visibility_changed)
        self._show_placeholder()
        print(f"[FPV] window created: {self.WINDOW_NAME}")

    def _on_win_visibility_changed(self, visible: bool):
        if not visible:
            async def _restore():
                await omni.kit.app.get_app().next_update_async()
                if self._win:
                    self._win.visible = True
            asyncio.ensure_future(_restore())

    def _show_placeholder(self):
        if not self._win:
            return
        self._img_widget       = None
        self._marker_frame     = None
        self._cam_height_field = None
        self._ht_btns          = {}
        with self._win.frame:
            with ui.VStack():
                ui.Spacer()
                ui.Label(
                    "Waiting for floor plan…\n\nClick  'Open in Isaac Sim'\nin the web editor.",
                    style={"color": 0xFF777799, "font_size": 12},
                    alignment=ui.Alignment.CENTER,
                    word_wrap=True,
                )
                ui.Spacer()

    def _show_floorplan(self, img_path: str):
        if not self._win:
            return
        self._cam_height_field = None
        self._ht_btns          = {}
        self._cam_state_hash   = None  # force a fresh marker rebuild after layout

        # Reset zoom only when this is a genuinely new image, not when this
        # rebuild was triggered by our own zoom +/-/Fit buttons re-showing
        # the same path at a new zoom level.
        if img_path != getattr(self, "_fp_zoom_path", None):
            self._fp_zoom      = 1.0
            self._fp_zoom_path = img_path
            try:
                from PIL import Image as _PILImage
                with _PILImage.open(img_path) as _im:
                    nw, nh = _im.size
                self._fp_native_ar = (nh / nw) if nw > 0 else 0.65
            except Exception:
                self._fp_native_ar = 0.65

        disp_w = self._fp_base_w * self._fp_zoom
        disp_h = disp_w * self._fp_native_ar

        with self._win.frame:
            with ui.VStack(spacing=2):
                # ── Filename ──────────────────────────────────────────────
                ui.Label(
                    Path(img_path).name,
                    height=_LABEL_H,
                    style={"color": 0xFF445566, "font_size": 10},
                    alignment=ui.Alignment.CENTER,
                )
                # ── Floor selector (multi-floor buildings only) ────────────
                # Mirrors the web app's own Labelling-tab floor tabs.
                # Clicking a floor swaps the displayed 2D image (via
                # _select_floor -> _show_floorplan, which is this very
                # function, so the button set gets rebuilt fresh each time
                # too) and filters camera/light markers to that floor.
                if len(self._floor_images) > 1:
                    with ui.HStack(height=22, spacing=4,
                                   style={"margin_left": 6, "margin_right": 6}):
                        ui.Label("Floor:", width=34,
                                 style={"font_size": 10, "color": 0xFF7690AC})
                        def _floor_sort_key(k):
                            try:
                                return (0, float(k))
                            except (TypeError, ValueError):
                                return (1, str(k))
                        for fk in sorted(self._floor_images.keys(), key=_floor_sort_key):
                            active = (fk == self._active_floor)
                            def _make_floor_fn(k=fk):
                                return lambda: self._select_floor(k)
                            ui.Button(
                                str(fk), width=30, height=20,
                                clicked_fn=_make_floor_fn(),
                                style={
                                    "background_color": 0xFF2A5A2A if active else 0xFF1A1A22,
                                    "color": 0xFF88FF88 if active else 0xFF7690AC,
                                    "font_size": 10, "border_radius": 4,
                                },
                            )
                        ui.Spacer()
                # ── Height controls ───────────────────────────────────────
                with ui.HStack(height=24, spacing=4,
                               style={"margin_left": 6, "margin_right": 6}):
                    ui.Label("Cam:", width=28,
                             style={"font_size": 10, "color": 0xFF7690AC})
                    self._cam_height_field = ui.FloatDrag(
                        min=0.0, max=100.0, step=0.1, width=56)
                    self._cam_height_field.model.set_value(self._spawn_height_ft)
                    self._cam_height_field.model.add_value_changed_fn(
                        self._on_cam_height_changed)
                    ui.Label("ft", width=14,
                             style={"font_size": 10, "color": 0xFF7690AC})
                    ui.Spacer()
                    for ft in [7, 8, 9]:
                        def _make_fn(h=ft):
                            def _fn():
                                self._spawn_height_ft = float(h)
                                self._updating_cam_height_field = True
                                try:
                                    if self._cam_height_field:
                                        self._cam_height_field.model.set_value(float(h))
                                except Exception:
                                    pass
                                self._updating_cam_height_field = False
                                self._refresh_ht_btns()
                                self._apply_height_to_active_cam()
                            return _fn
                        active = abs(ft - self._spawn_height_ft) < 0.01
                        btn = ui.Button(
                            f"{ft} ft", width=42, height=22,
                            clicked_fn=_make_fn(ft),
                            style=self._ht_btn_style(active),
                        )
                        self._ht_btns[ft] = btn
                # ── Zoom controls ──────────────────────────────────────────
                # Markers are placed at a fixed pixel size (_ICON_CANVAS /
                # _LIGHT_ICON_SIZE) regardless of the image's rendered size —
                # only their percent-based POSITION depends on the container,
                # so zooming the image in never scales the markers up with
                # it, which is what makes precise placement possible.
                with ui.HStack(height=20, spacing=4,
                               style={"margin_left": 6, "margin_right": 6}):
                    ui.Button("−", width=22, height=20, clicked_fn=self._zoom_out,
                              style={"font_size": 12})
                    self._zoom_label = ui.Label(
                        f"{int(round(self._fp_zoom * 100))}%", width=40,
                        alignment=ui.Alignment.CENTER,
                        style={"font_size": 10, "color": 0xFF7690AC})
                    ui.Button("+", width=22, height=20, clicked_fn=self._zoom_in,
                              style={"font_size": 12})
                    ui.Button("Fit", width=32, height=20, clicked_fn=self._zoom_reset,
                              style={"font_size": 10})
                    ui.Spacer()
                # ── Floor plan image + markers + drop zone ────────────────
                self._fp_scroll = ui.ScrollingFrame(
                    horizontal_scrollbar_policy=ui.ScrollBarPolicy.SCROLLBAR_AS_NEEDED,
                    vertical_scrollbar_policy=ui.ScrollBarPolicy.SCROLLBAR_AS_NEEDED,
                )
                with self._fp_scroll:
                    with ui.ZStack(width=disp_w, height=disp_h):
                        self._img_widget = ui.Image(
                            "file:///" + Path(img_path).as_posix(),
                            fill_policy=ui.FillPolicy.STRETCH,
                        )
                        # Background pan/zoom surface — sits under the marker
                        # layer, so a marker's own opaque hit-rect still wins
                        # inside its own bounds. Right-click-hold drags/pans
                        # the view; left-click is reserved entirely for
                        # marker selection/movement, so the two never fight
                        # over the same mouse button.
                        pan_hit = ui.Rectangle(
                            opaque_for_mouse_events=True,
                            style={"background_color": 0x00000000})
                        pan_hit.set_mouse_pressed_fn(
                            lambda _x, _y, btn, _m: self._on_pan_press() if btn == 1 else None)
                        pan_hit.set_mouse_wheel_fn(self._on_fp_wheel)
                        self._marker_frame = ui.Frame()
                        drop_zone = ui.Frame()
                        drop_zone.set_accept_drop_fn(
                            lambda d: str(d).strip() == _DRAG_TOKEN)
                        drop_zone.set_drop_fn(self._on_camera_drop)
        self._rebuild_markers()

    def _active_floor_spawn_kwargs(self):
        """floor_base_z/wall_height_m kwargs for _spawn_light/_spawn_camera,
        derived from whichever floor is currently selected in this window —
        empty dict (their existing whole-stage-bbox default/single-floor
        behavior, unchanged) when no floor is selected."""
        if self._active_floor is None:
            return {}
        rng = self._floor_z_ranges().get(self._active_floor)
        if rng is None:
            return {}
        wall_height_ft = self._units_to_ft(rng[1] - rng[0])
        return {"floor_base_z": rng[0], "wall_height_m": wall_height_ft * 0.3048}

    def _select_floor(self, floor_key):
        """Switch the window to a specific floor: swap the displayed 2D
        image and rebuild markers filtered to that floor (via
        _rebuild_markers -> _floor_z_ranges/self._active_floor). No-op if
        floor_key isn't one we actually have an image for."""
        if floor_key not in self._floor_images:
            return
        self._active_floor   = floor_key
        self._floorplan_path = self._floor_images[floor_key]
        self._fp_image_mtime = None  # not file-mtime-tracked like the glob path
        self._show_floorplan(self._floorplan_path)

    # ── Zoom controls ──────────────────────────────────────────────────────────

    def _set_zoom(self, z: float):
        z = max(0.5, min(4.0, z))
        if abs(z - self._fp_zoom) < 1e-6 or not self._floorplan_path:
            return
        frac = None
        try:
            sf = self._fp_scroll
            if sf is not None:
                mx, my = (sf.scroll_x_max or 0.0), (sf.scroll_y_max or 0.0)
                frac = (sf.scroll_x / mx if mx > 0 else 0.0,
                        sf.scroll_y / my if my > 0 else 0.0)
        except Exception:
            frac = None
        self._fp_zoom = z
        self._show_floorplan(self._floorplan_path)
        if frac is not None:
            asyncio.ensure_future(self._restore_scroll_frac(*frac))

    def _zoom_in(self):
        self._set_zoom(self._fp_zoom * 1.25)

    def _zoom_out(self):
        self._set_zoom(self._fp_zoom / 1.25)

    def _zoom_reset(self):
        self._set_zoom(1.0)

    async def _restore_scroll_frac(self, fx: float, fy: float):
        app = omni.kit.app.get_app()
        for _ in range(3):
            await app.next_update_async()
        try:
            sf = self._fp_scroll
            if sf is not None:
                mx, my = (sf.scroll_x_max or 0.0), (sf.scroll_y_max or 0.0)
                sf.scroll_x = fx * mx
                sf.scroll_y = fy * my
        except Exception:
            pass

    def _on_fp_wheel(self, *args):
        """Mouse-wheel-over-floorplan → zoom. omni.ui's exact wheel-callback
        signature isn't pinned down here (no local Kit SDK to check against),
        so this picks out whichever numeric arg looks like a wheel delta
        (small magnitude) rather than assuming a fixed argument order —
        wrapped defensively so a signature mismatch just no-ops instead of
        crashing the extension."""
        try:
            nums = [float(a) for a in args if isinstance(a, (int, float))]
            nonzero = [n for n in nums if abs(n) > 1e-6]
            if not nonzero:
                return
            delta = min(nonzero, key=abs)
            self._set_zoom(self._fp_zoom * (1.1 if delta > 0 else 1 / 1.1))
        except Exception as e:
            print(f"[FPV] wheel zoom: {e}")

    # ── Pan (left-click-drag on empty floor plan area) ─────────────────────────

    def _on_pan_press(self):
        if self._fp_any_dragging or self._rot_active:
            return
        self._fp_pan_active     = True
        self._fp_pan_start_pos  = self._cursor_kit_pos()
        sf = self._fp_scroll
        self._fp_pan_start_scroll = (
            (sf.scroll_x if sf is not None else 0.0),
            (sf.scroll_y if sf is not None else 0.0),
        )
        if self._fp_pan_update_sub is None:
            self._fp_pan_update_sub = (
                omni.kit.app.get_app()
                .get_update_event_stream()
                .create_subscription_to_pop(self._on_pan_update, name="fpv_pan_drag")
            )

    def _on_pan_update(self, _event):
        if not self._fp_pan_active:
            self._fp_pan_update_sub = None
            return
        try:
            import carb.input as ci
            import omni.appwindow
            iface = ci.acquire_input_interface()
            mouse = omni.appwindow.get_default_app_window().get_mouse()
            try:
                held = float(iface.get_mouse_value(mouse, ci.MouseInput.RIGHT_BUTTON)) >= 0.5
            except Exception:
                held = True
            if not held:
                self._fp_pan_active     = False
                self._fp_pan_update_sub = None
                return
            sx, sy = self._cursor_kit_pos()
            if sx is None or self._fp_pan_start_pos[0] is None:
                return
            dx = sx - self._fp_pan_start_pos[0]
            dy = sy - self._fp_pan_start_pos[1]
            sf = self._fp_scroll
            if sf is not None:
                sf.scroll_x = self._fp_pan_start_scroll[0] - dx
                sf.scroll_y = self._fp_pan_start_scroll[1] - dy
        except Exception as e:
            print(f"[FPV] pan update: {e}")

    # ── Height controls ───────────────────────────────────────────────────────

    @staticmethod
    def _ht_btn_style(active: bool) -> dict:
        return {
            "background_color": 0xFF2A5A2A if active else 0xFF222222,
            "color":            0xFF88FF88 if active else 0xFFAAAAAA,
            "font_size": 10, "border_radius": 4,
        }

    @staticmethod
    def _clamped_offset_pct(u: float, half_size_pct: float) -> float:
        """Percent offset for a marker of half-width/half-height
        half_size_pct (as a % of the container), centered on u (a 0..1
        fraction along that axis) — clamped so the marker never extends
        past the container's own edges.

        Centering a fixed-size marker exactly on u naturally pushes it
        outside the frame whenever u is near 0 or 1 — half the marker
        sticks out past that edge, more so for anything bigger than the
        marker itself (the rotation ring around the active camera's icon
        is bigger than the icon it surrounds). This is what was causing
        the Floor Plan View to jitter/"resize" repeatedly whenever a
        camera sat at or near an edge: the overflow grows the parent
        frame, which changes img.computed_width/height, which recomputes
        u/v (both depend on it) and the marker position again, repeating.

        Clamping the drawn offset (not u itself, so the underlying world
        position used for actually moving the camera is unaffected — only
        the marker's on-screen position) keeps every marker fully inside
        the frame regardless of where the camera actually is."""
        lo, hi = 0.0, max(0.0, 100.0 - 2.0 * half_size_pct)
        return max(lo, min(u * 100.0 - half_size_pct, hi))

    def _refresh_ht_btns(self):
        for ft, btn in self._ht_btns.items():
            btn.style = self._ht_btn_style(abs(ft - self._spawn_height_ft) < 0.01)

    def _on_cam_height_changed(self, model):
        if self._updating_cam_height_field:
            return
        self._spawn_height_ft = model.get_value_as_float()
        self._refresh_ht_btns()
        self._apply_height_to_active_cam()

    def _rig_path_for_height(self, path):
        """self._viewport_camera mirrors whatever the live viewport reports,
        which for a placed camera is the optical Camera prim (see
        floorplan.tool's _cam_optical_path) — not the rig that actually
        owns translate/height. Resolve back to the rig (its parent) so
        height reads/writes land on the right prim. The built-in
        Perspective camera has no rig and is returned unchanged."""
        if not path or "/OmniverseKit_" in path:
            return path
        try:
            import omni.usd
            stage = omni.usd.get_context().get_stage()
            prim = stage.GetPrimAtPath(path) if stage else None
            if prim and prim.IsValid() and prim.GetTypeName() == "Camera":
                parent = prim.GetParent()
                if parent and parent.IsValid():
                    return str(parent.GetPath())
        except Exception:
            pass
        return path

    def _apply_height_to_active_cam(self):
        p = self._rig_path_for_height(self._viewport_camera)
        if not p or "/OmniverseKit_" in p:
            return
        t, r = self._get_cam_xform(p)
        if t is None:
            return
        t[2] = self._floor_z() + self._ft_to_units(self._spawn_height_ft)
        self._set_cam_xform(p, t, r)
        # Keep Floor Plan Tool's height joystick in sync — it's a separate
        # widget controlling the same camera, so without this push it keeps
        # showing whatever height it last read while this field moves away
        # from it. Mirrors the read-then-sync idiom FPT already uses on its
        # own camera-switch (self._jsv_sync_fn(self._jsv_read_height_fn())).
        try:
            from floorplan.tool import extension as _fpt_mod
            fpt = getattr(_fpt_mod, "_INSTANCE", None)
            if fpt is not None and hasattr(fpt, "_jsv_sync_fn") and hasattr(fpt, "_jsv_read_height_fn"):
                fpt._jsv_sync_fn(fpt._jsv_read_height_fn())
        except Exception as _e:
            print(f"[FPV] fpt jsv sync: {_e}")

    def _ft_to_units(self, feet: float) -> float:
        b = self._floor_plane_bounds()
        if b is None:
            return feet * 0.3048
        x_min, x_max, y_min, y_max = b
        extent = max(abs(x_max - x_min), abs(y_max - y_min))
        # Threshold matches floorplan.tool's get_floor_info() (kept in sync
        # — see its comment for the full explanation): 100 wrongly
        # classified a genuinely large, correctly meter-scaled building as
        # centimetre-scale, confirmed live against a real 121m building.
        return feet * (30.48 if extent > 1000 else 0.3048)

    def _units_to_ft(self, units: float) -> float:
        b = self._floor_plane_bounds()
        if b is None:
            return units / 0.3048
        x_min, x_max, y_min, y_max = b
        extent = max(abs(x_max - x_min), abs(y_max - y_min))
        return units / (30.48 if extent > 1000 else 0.3048)

    def _update_height_field_from_cam(self, camera_path: str):
        """Read camera's actual Z from stage, convert to feet, update the Cam: FloatDrag."""
        try:
            t, _ = self._get_cam_xform(camera_path)
            if t is None:
                return
            height_ft = max(0.0, self._units_to_ft(t[2] - self._floor_z()))
            self._spawn_height_ft = height_ft
            self._updating_cam_height_field = True
            try:
                if self._cam_height_field:
                    self._cam_height_field.model.set_value(height_ft)
            except Exception:
                pass
            self._updating_cam_height_field = False
            self._refresh_ht_btns()
        except Exception as e:
            print(f"[FPV] height field sync: {e}")

    # ── Coordinate mapping ────────────────────────────────────────────────────

    def _world_to_uv(self, wx: float, wy: float):
        b = self._floor_plane_bounds()
        if b is None:
            return 0.5, 0.5
        x_min, x_max, y_min, y_max = b
        u = (wx - x_min) / max(x_max - x_min, 1e-6)
        v = 1.0 - (wy - y_min) / max(y_max - y_min, 1e-6)
        return min(max(u, 0.0), 1.0), min(max(v, 0.0), 1.0)

    def _uv_to_world(self, u: float, v: float):
        b = self._floor_plane_bounds()
        if b is None:
            return 0.0, 0.0
        x_min, x_max, y_min, y_max = b
        return (x_min + u * (x_max - x_min),
                y_min + (1.0 - v) * (y_max - y_min))

    # ── PIL camera icon ───────────────────────────────────────────────────────

    def _make_camera_icon_png(self, angle_deg: float, is_selected: bool,
                               seq: int) -> str:
        """Render a rotated camera icon (rect body + directional arrow) to PNG."""
        try:
            from PIL import Image, ImageDraw
            img  = Image.new("RGBA", (_ICON_CANVAS, _ICON_CANVAS), (0, 0, 0, 0))
            draw = ImageDraw.Draw(img)
            cx = cy = _ICON_CANVAS // 2
            fill    = (30, 200, 60, 235) if is_selected else (210, 30, 30, 235)
            outline = (255, 255, 255, 200)
            hw = int(_ICON_SIZE * 0.55)
            hh = int(_ICON_SIZE * 0.38)
            r  = max(3, _ICON_SIZE // 7)
            x0, y0, x1, y1 = cx - hw, cy - hh, cx + hw, cy + hh
            try:
                draw.rounded_rectangle([x0, y0, x1, y1], radius=r,
                                       fill=fill, outline=outline, width=1)
            except (TypeError, AttributeError):
                try:
                    draw.rounded_rectangle([x0, y0, x1, y1], radius=r, fill=fill)
                except AttributeError:
                    draw.rectangle([x0, y0, x1, y1], fill=fill)
            # Directional arrow on the +X side (camera "lens" direction)
            fb = int(_ICON_SIZE * 0.26)
            fl = int(_ICON_SIZE * 0.42)
            draw.polygon([(cx + hw,      cy - fb),
                           (cx + hw + fl, cy),
                           (cx + hw,      cy + fb)], fill=fill)
            try:
                resample = Image.Resampling.BICUBIC
            except AttributeError:
                resample = Image.BICUBIC
            rotated = img.rotate(angle_deg, resample=resample, expand=False)
            # Cycle through 8 filename slots to force Kit to reload from disk.
            slot  = seq % 8
            a_key = int(round(angle_deg / 5.0) * 5) % 360
            name  = f"_fpv_icon_{slot}_{'s' if is_selected else 'u'}_{a_key}.png"
            out   = ISAAC_TMP / name
            rotated.save(str(out))
            return str(out)
        except Exception as e:
            print(f"[FPV] icon render: {e}")
            return ""

    # ── Marker overlay ────────────────────────────────────────────────────────

    def _compute_cam_hash(self) -> int:
        """Cheap fingerprint of current camera state (positions + active camera).
        Returns 0 on error so a mismatch always causes a rebuild."""
        try:
            data   = self._camera_data()
            active = self._get_active_viewport_camera() or ""
            return hash(
                tuple((p, int(wx * 50), int(wy * 50), int(a * 2), int(wz * 50))
                      for wx, wy, a, _, p, wz in data)
                + (active,)
            )
        except Exception:
            return 0

    def _rebuild_markers(self):
        if not self._marker_frame:
            return
        if self._fp_any_dragging:
            return
        self._icon_seq += 1
        seq        = self._icon_seq
        active_cam = self._get_active_viewport_camera()
        self._viewport_camera = active_cam
        # active_cam comes from the live viewport, so for a placed camera
        # it's the optical Camera path (rig_path + "/Camera"), not the rig
        # path _camera_data returns below — resolve the mapping once here
        # rather than per-marker, so the is_active comparison further down
        # actually matches instead of silently never highlighting anything.
        _optical_fn = None
        try:
            from floorplan.tool import extension as _fpt_mod
            fpt = getattr(_fpt_mod, "_INSTANCE", None)
            if fpt and fpt._ds:
                _optical_fn = fpt._ds.get("cam_optical_path")
        except Exception:
            pass
        # Multi-floor filtering: when a floor is selected, only show markers
        # whose Z falls inside that floor's own geometry range (with a
        # generous margin either side so a camera/light spawned slightly
        # above/below its floor's exact wall bounds — the normal case —
        # doesn't get hidden). None/missing range = show everything, which
        # is also the correct behavior for a single-floor building (no
        # floor selected at all).
        _active_z_range = None
        if self._active_floor is not None:
            _active_z_range = self._floor_z_ranges().get(self._active_floor)
        _z_margin = self._ft_to_units(3.0)

        def _on_active_floor(z):
            if _active_z_range is None:
                return True
            return (_active_z_range[0] - _z_margin) <= z <= (_active_z_range[1] + _z_margin)

        # Compute frame pixel dimensions so we can CENTER each icon on the
        # camera UV position rather than anchoring from its top-left corner.
        img = self._img_widget
        fw  = img.computed_width  if (img and img.computed_width  > 0) else 380
        fh  = img.computed_height if (img and img.computed_height > 0) else 380
        half_x_pct = (_ICON_CANVAS / 2) / fw * 100
        half_y_pct = (_ICON_CANVAS / 2) / fh * 100
        self._marker_frame.clear()
        with self._marker_frame:
            with ui.ZStack():
                for (wx, wy, angle_deg, _name, ppath, wz) in self._camera_data():
                    if not _on_active_floor(wz):
                        continue
                    u, v      = self._world_to_uv(wx, wy)
                    ppath_optical = _optical_fn(ppath) if _optical_fn else ppath
                    is_active = (ppath_optical == active_cam)
                    icon_path = self._make_camera_icon_png(angle_deg, is_active, seq)

                    # Rotation gizmo — only for the currently selected camera.
                    # A separate, larger Placer+ZStack centered on the same
                    # point as the icon (not nested inside the icon's own
                    # fixed _ICON_CANVAS ZStack, so it isn't clipped to that
                    # smaller box). Declared BEFORE the icon block below, so
                    # in z-order the icon's own hit-rect (added after) still
                    # gets first claim on clicks within its own bounds —
                    # select/reposition keeps working unchanged, and only
                    # clicks in the ring area outside the icon fall through
                    # to rotation.
                    if is_active:
                        ring_d = _ROT_RING_R * 2
                        half_ring_pct_x = (ring_d / 2) / fw * 100
                        half_ring_pct_y = (ring_d / 2) / fh * 100
                        ring_placer = ui.Placer(
                            offset_x=ui.Percent(self._clamped_offset_pct(u, half_ring_pct_x)),
                            offset_y=ui.Percent(self._clamped_offset_pct(v, half_ring_pct_y)),
                        )
                        with ring_placer:
                            with ui.ZStack(width=ring_d, height=ring_d):
                                ring_hit = ui.Rectangle(
                                    opaque_for_mouse_events=True,
                                    style={"background_color": 0x00000000})
                                ring_hit.set_mouse_pressed_fn(
                                    lambda _x, _y, btn, _m, _p=ppath:
                                        self._on_rotate_press(_p) if btn == 0 else None)
                                ui.Circle(style={
                                    "background_color": 0x00000000,
                                    "border_width": 2.0,
                                    "border_color": 0xFF33CCFF,
                                })

                    # Subtract half-canvas percent so the icon body center aligns
                    # with the camera's UV position, not its top-left corner.
                    placer = ui.Placer(
                        offset_x=ui.Percent(self._clamped_offset_pct(u, half_x_pct)),
                        offset_y=ui.Percent(self._clamped_offset_pct(v, half_y_pct)),
                    )
                    with placer:
                        with ui.ZStack(width=_ICON_CANVAS, height=_ICON_CANVAS):
                            if icon_path:
                                ui.Image(
                                    "file:///" + Path(icon_path).as_posix(),
                                    fill_policy=ui.FillPolicy.STRETCH,
                                )
                            else:
                                # PIL unavailable — plain circle fallback
                                ui.Circle(style={
                                    "background_color": 0xFF33DD55 if is_active else 0xFF181818,
                                    "border_width": 1.5,
                                    "border_color": 0xFFFFFFFF if is_active else 0xFF888888,
                                })
                            hit = ui.Rectangle(
                                opaque_for_mouse_events=True,
                                style={"background_color": 0x00000000})
                            hit.set_mouse_pressed_fn(
                                lambda _x, _y, btn, _m, _p=ppath, _pl=placer:
                                    self._on_marker_press(_p, _pl) if btn == 0 else None)

                # ── Individual lights — plain square marker, no PIL icon
                # (lights aren't directional, so no rotation arrow needed).
                half_lx_pct = (_LIGHT_ICON_SIZE / 2) / fw * 100
                half_ly_pct = (_LIGHT_ICON_SIZE / 2) / fh * 100
                for (wx, wy, lpath, wz) in self._light_data():
                    if not _on_active_floor(wz):
                        continue
                    u, v = self._world_to_uv(wx, wy)
                    placer = ui.Placer(
                        offset_x=ui.Percent(self._clamped_offset_pct(u, half_lx_pct)),
                        offset_y=ui.Percent(self._clamped_offset_pct(v, half_ly_pct)),
                    )
                    with placer:
                        with ui.ZStack(width=_LIGHT_ICON_SIZE, height=_LIGHT_ICON_SIZE):
                            ui.Rectangle(style={
                                "background_color": 0xFFDDCC33,
                                "border_width": 1.5,
                                "border_color": 0xFF998822,
                                "border_radius": 2,
                            })
                            hit = ui.Rectangle(
                                opaque_for_mouse_events=True,
                                style={"background_color": 0x00000000})
                            hit.set_mouse_pressed_fn(
                                lambda _x, _y, btn, _m, _p=lpath, _pl=placer:
                                    self._on_marker_press(_p, _pl, kind="light") if btn == 0 else None)

    def _schedule_rebuild(self):
        async def _do():
            await omni.kit.app.get_app().next_update_async()
            if not self._fp_any_dragging:
                self._rebuild_markers()
        asyncio.ensure_future(_do())

    async def _poll_markers(self):
        while True:
            try:
                await asyncio.sleep(0.4)
                if not self._floorplan_path or self._fp_any_dragging:
                    continue
                new_hash = self._compute_cam_hash()
                if new_hash != self._cam_state_hash:
                    self._cam_state_hash = new_hash
                    self._rebuild_markers()
            except asyncio.CancelledError:
                break
            except Exception as e:
                print(f"[FPV] marker poll: {e}")

    # ── Drag — reposition an existing camera ──────────────────────────────────

    def _on_marker_press(self, cam_path: str, placer, kind: str = "camera"):
        if kind != "light":
            self._select_camera(cam_path)
        self._fp_drag_cam        = cam_path
        self._fp_drag_placer     = placer
        self._fp_drag_kind        = kind
        self._fp_drag_marker_size = _LIGHT_ICON_SIZE if kind == "light" else _ICON_CANVAS
        self._fp_any_dragging    = True
        self._fp_drag_last_t     = 0.0
        self._fp_drag_start_pos  = self._cursor_kit_pos()
        self._fp_drag_moved      = False
        if self._fp_drag_update_sub is None:
            self._fp_drag_update_sub = (
                omni.kit.app.get_app()
                .get_update_event_stream()
                .create_subscription_to_pop(self._on_drag_update,
                                            name="fpv_marker_drag")
            )

    def _on_drag_update(self, _event):
        if not self._fp_any_dragging or not self._fp_drag_cam:
            self._fp_drag_update_sub = None
            return
        try:
            import carb.input as ci
            import omni.appwindow
            iface = ci.acquire_input_interface()
            mouse = omni.appwindow.get_default_app_window().get_mouse()
            try:
                held = float(iface.get_mouse_value(
                    mouse, ci.MouseInput.LEFT_BUTTON)) >= 0.5
            except Exception:
                try:
                    held = bool(int(iface.get_mouse_button_flags(
                        mouse, ci.MouseInput.LEFT_BUTTON)) & 0x1)
                except Exception:
                    held = True
            if not held:
                self._fp_any_dragging    = False
                self._fp_drag_cam        = None
                self._fp_drag_placer     = None
                self._fp_drag_update_sub = None
                self._schedule_rebuild()
                return
            now = time.time()
            if now - self._fp_drag_last_t < 1.0 / 30.0:
                return
            self._fp_drag_last_t = now
            img = self._img_widget
            if not img or not img.computed_width or not img.computed_height:
                return
            sx, sy = self._cursor_kit_pos()
            if sx is None:
                return
            # Don't move the camera until the cursor has actually dragged ≥8 px
            # from the initial press position (prevents a click from jumping it).
            if not self._fp_drag_moved:
                sp = self._fp_drag_start_pos
                if sp[0] is not None:
                    if math.sqrt((sx - sp[0])**2 + (sy - sp[1])**2) < 8:
                        return
                self._fp_drag_moved = True
            u = min(max((sx - img.screen_position_x) / img.computed_width,  0.0), 1.0)
            v = min(max((sy - img.screen_position_y) / img.computed_height, 0.0), 1.0)
            wx, wy = self._uv_to_world(u, v)
            t, r = self._get_cam_xform(self._fp_drag_cam)
            if t is None:
                return
            if self._fp_drag_kind != "light":
                wx, wy = self._clamp_move_by_walls(t[0], t[1], t[2], wx, wy)
            t[0], t[1] = wx, wy
            self._set_cam_xform(self._fp_drag_cam, t, r)
            if self._fp_drag_placer:
                # Same centering correction as _rebuild_markers — marker size
                # depends on what's being dragged (camera icon vs light square).
                _fw = img.computed_width  if img.computed_width  > 0 else 380
                _fh = img.computed_height if img.computed_height > 0 else 380
                _msz = getattr(self, '_fp_drag_marker_size', _ICON_CANVAS)
                self._fp_drag_placer.offset_x = ui.Percent(
                    self._clamped_offset_pct(u, (_msz / 2) / _fw * 100))
                self._fp_drag_placer.offset_y = ui.Percent(
                    self._clamped_offset_pct(v, (_msz / 2) / _fh * 100))
        except Exception as e:
            print(f"[FPV] drag update: {e}")

    # ── Rotation gizmo — drag the ring around the selected camera's marker
    # to change its yaw. Delta-based (tracks how far the cursor has swept
    # around the marker's screen-space center since the press), not an
    # absolute "angle at click position" — screen-space angle and world-space
    # heading aren't a clean 1:1 mapping once the floor plan image isn't
    # square (STRETCH fill can scale X/Y differently), so tracking a
    # relative sweep and applying that same delta to the camera's actual
    # yaw is robust to that distortion in a way computing an absolute
    # target angle wouldn't be. ─────────────────────────────────────────────

    def _clamp_move_by_walls(self, ox, oy, oz, nx, ny):
        """Forwards to floorplan.tool's Wall Collision clamp (the toggle,
        state, and scene-geometry cache all live there). Returns (nx, ny)
        unchanged if that extension isn't available or the toggle is off."""
        try:
            from floorplan.tool import extension as _fpt_mod
            fpt = getattr(_fpt_mod, "_INSTANCE", None)
            if fpt and fpt._ds and "clamp_move_by_walls" in fpt._ds:
                return fpt._ds["clamp_move_by_walls"](ox, oy, oz, nx, ny)
        except Exception:
            pass
        return nx, ny

    def _cam_body_local_center_offset(self, cam_path: str):
        """Rig-local-space offset from the rig's own origin to the geometric
        center of its attached camera body model (floorplan.tool's
        add_camera -> _attach_camera_body, which references camera_body.usd
        at "<rig>/Camera/CameraModel"), if one exists. The referenced
        asset's own origin is not guaranteed to sit at its visual center
        (e.g. a unit-scale mismatch in the source file can put it meters
        away) — rotating the rig around its raw origin then makes the body
        swing/arc rather than spin in place. Computed from the body's actual
        world bounding box (not hardcoded geometry constants, which live in
        a different extension) so it stays correct even if that geometry
        changes. Returns None if there's no body to compensate for.

        cam_path here is the RIG path (see floorplan.tool's
        _cam_optical_path / add_camera and this file's _on_rotate_press,
        which passes its own cam_path straight through) — NOT the optical
        Camera prim itself, which is why the body lookup below needs the
        extra "/Camera" segment; this function predates the rig/camera
        split and was still pointed at the old pre-split single-prim
        layout ("<cam_path>/CamBody") until fixed, silently returning None
        for every real camera and leaving rotate-drag on the uncompensated
        raw-origin path below the whole time."""
        try:
            import omni.usd
            from pxr import Usd, UsdGeom, Gf
            stage = omni.usd.get_context().get_stage()
            if not stage:
                return None
            cam_prim  = stage.GetPrimAtPath(cam_path)
            body_prim = stage.GetPrimAtPath(f"{cam_path}/Camera/CameraModel")
            if not cam_prim or not cam_prim.IsValid() or not body_prim or not body_prim.IsValid():
                return None
            bc  = UsdGeom.BBoxCache(Usd.TimeCode.Default(),
                [UsdGeom.Tokens.default_, UsdGeom.Tokens.render], useExtentsHint=True)
            rng = bc.ComputeWorldBound(body_prim).ComputeAlignedRange()
            if rng.IsEmpty():
                return None
            mn, mx = rng.GetMin(), rng.GetMax()
            world_center = Gf.Vec3d((mn[0]+mx[0])/2.0, (mn[1]+mx[1])/2.0, (mn[2]+mx[2])/2.0)
            cam_mat = UsdGeom.Xformable(cam_prim).ComputeLocalToWorldTransform(Usd.TimeCode.Default())
            local_center = cam_mat.GetInverse().Transform(world_center)
            return (float(local_center[0]), float(local_center[1]), float(local_center[2]))
        except Exception as e:
            print(f"[FPV] body center offset: {e}")
            return None

    def _on_rotate_press(self, cam_path: str):
        img = self._img_widget
        if not img or not img.computed_width or not img.computed_height:
            return
        t, r = self._get_cam_xform(cam_path)
        if t is None or r is None:
            return
        u, v = self._world_to_uv(t[0], t[1])
        cx = img.screen_position_x + u * img.computed_width
        cy = img.screen_position_y + v * img.computed_height
        sx, sy = self._cursor_kit_pos()
        if sx is None:
            return
        self._rot_active      = True
        self._rot_cam_path    = cam_path
        self._rot_center_win  = (cx, cy)
        self._rot_press_angle = math.degrees(math.atan2(sy - cy, sx - cx))
        self._rot_press_yaw   = r[2]
        self._rot_last_t      = 0.0
        self._rot_local_center_offset = None
        self._rot_target_world_center = None
        try:
            import omni.usd
            from pxr import UsdGeom, Usd, Gf
            stage = omni.usd.get_context().get_stage()
            prim  = stage.GetPrimAtPath(cam_path)
            if prim and prim.IsValid():
                mat = UsdGeom.Xformable(prim).ComputeLocalToWorldTransform(Usd.TimeCode.Default())
                # Pin the body's current visual-center world position so
                # the drag can rotate around it instead of the camera's
                # raw lens-tip origin.
                offset = self._cam_body_local_center_offset(cam_path)
                self._rot_local_center_offset = offset
                if offset is not None:
                    wc = mat.Transform(Gf.Vec3d(*offset))
                    self._rot_target_world_center = (float(wc[0]), float(wc[1]), float(wc[2]))
        except Exception as e:
            print(f"[FPV] rotate press setup: {e}")
        if self._rot_drag_sub is None:
            self._rot_drag_sub = (
                omni.kit.app.get_app()
                .get_update_event_stream()
                .create_subscription_to_pop(self._on_rotate_drag_update,
                                            name="fpv_marker_rotate")
            )

    def _on_rotate_drag_update(self, _event):
        if not self._rot_active or not self._rot_cam_path:
            self._rot_drag_sub = None
            return
        try:
            import carb.input as ci
            import omni.appwindow
            iface = ci.acquire_input_interface()
            mouse = omni.appwindow.get_default_app_window().get_mouse()
            try:
                held = float(iface.get_mouse_value(
                    mouse, ci.MouseInput.LEFT_BUTTON)) >= 0.5
            except Exception:
                try:
                    held = bool(int(iface.get_mouse_button_flags(
                        mouse, ci.MouseInput.LEFT_BUTTON)) & 0x1)
                except Exception:
                    held = True
            if not held:
                self._rot_active   = False
                self._rot_cam_path = None
                self._rot_drag_sub = None
                self._schedule_rebuild()   # final unthrottled re-render
                return
            now = time.time()
            if now - self._rot_last_t < 1.0 / 12.0:
                return
            self._rot_last_t = now
            sx, sy = self._cursor_kit_pos()
            if sx is None or self._rot_center_win is None:
                return
            cx, cy = self._rot_center_win
            cur_angle = math.degrees(math.atan2(sy - cy, sx - cx))
            # Screen-space Y grows downward while world Y grows "up" in the
            # floor plan (see _world_to_uv's V flip), which flips the
            # rotational sense between screen angle and world heading —
            # negate the delta so dragging clockwise on screen turns the
            # camera clockwise in the floor plan too.
            delta_screen     = cur_angle - self._rot_press_angle
            world_delta_deg  = -delta_screen
            t, r = self._get_cam_xform(self._rot_cam_path)
            if t is None or r is None:
                return

            # self._rot_cam_path is now the camera's RIG (see
            # floorplan.tool's _cam_optical_path / add_camera) — an Xform
            # that only ever holds translate + a yaw rotateZ, with r[0]
            # and r[1] permanently 0. Tilt/roll live entirely on the
            # separate child Camera prim and are never touched from here,
            # so writing straight to r[2] is now ALWAYS a pure rotation
            # about world Z, regardless of the camera's current tilt/roll
            # (unlike the old single-prim scheme, where yaw only stayed
            # purely vertical at exactly the level pitch baseline).
            #
            # world_delta_deg is an ABSOLUTE offset from the press-time
            # cursor angle, not a per-frame increment — this update fires
            # every app tick while the button is held, not just on mouse
            # movement, so it stays constant (not zero) whenever the cursor
            # stops moving. Adding it to the live r[2] (already updated by
            # the previous tick) re-applied that same constant offset again
            # every single frame, spinning the camera continuously even
            # with the mouse dead still — the actual bug just reported.
            # Computing off the yaw cached at press time instead makes this
            # an absolute target: recomputing it while the cursor is still
            # yields the exact same value every tick, a harmless no-op.
            r[2] = self._rot_press_yaw + world_delta_deg

            offset = self._rot_local_center_offset
            target = self._rot_target_world_center
            if offset is not None and target is not None:
                # Rotate around the body's visual center, not the raw lens-
                # tip origin: write the new rotation first (translate still
                # unchanged), see where that puts the body's center, then
                # shift the translate by whatever's needed to pin that
                # center back to where it was before this rotation — so the
                # body spins in place instead of swinging around the lens tip.
                import omni.usd
                from pxr import UsdGeom, Usd, Gf
                self._set_cam_xform(self._rot_cam_path, t, r)
                stage = omni.usd.get_context().get_stage()
                prim  = stage.GetPrimAtPath(self._rot_cam_path)
                mat   = UsdGeom.Xformable(prim).ComputeLocalToWorldTransform(Usd.TimeCode.Default())
                cur_center = mat.Transform(Gf.Vec3d(*offset))
                t2 = [t[0] + (target[0] - cur_center[0]),
                      t[1] + (target[1] - cur_center[1]),
                      t[2] + (target[2] - cur_center[2])]
                self._set_cam_xform(self._rot_cam_path, t2, r)
            else:
                self._set_cam_xform(self._rot_cam_path, t, r)

            # Live-refresh so the icon's arrow and the ring track the drag
            # in real time (throttled above — full PIL re-render + disk
            # write per camera icon, too costly to do at full frame rate).
            self._rebuild_markers()
        except Exception as e:
            print(f"[FPV] rotate drag: {e}")

    # ── Drop — spawn a new camera from a Floor Plan Tool preset ──────────────

    def _on_camera_drop(self, payload, x: float = -1.0, y: float = -1.0):
        if str(payload).strip() != _DRAG_TOKEN:
            return
        try:
            img = self._img_widget
            if not img or not img.computed_width or not img.computed_height:
                return
            if x < 0 or y < 0:
                sx, sy = self._cursor_kit_pos()
                if sx is None:
                    return
                x = sx - img.screen_position_x
                y = sy - img.screen_position_y
            u = min(max(x / img.computed_width,  0.0), 1.0)
            v = min(max(y / img.computed_height, 0.0), 1.0)
            wx, wy = self._uv_to_world(u, v)

            drag_kind = "camera"
            try:
                from floorplan.tool.extension import FloorPlanToolExtension
                drag_kind = getattr(FloorPlanToolExtension, "_drag_kind", "camera")
            except Exception:
                pass

            if drag_kind == "light":
                self._spawn_light(wx, wy, **self._active_floor_spawn_kwargs())
                return

            preset_idx = 0
            try:
                from floorplan.tool.extension import FloorPlanToolExtension
                preset_idx = int(getattr(FloorPlanToolExtension, "_drag_preset_idx", 0))
            except Exception:
                pass
            ps = _CAM_PRESETS[_PRESET_KEYS[min(preset_idx, len(_PRESET_KEYS) - 1)]]
            self._spawn_camera(ps, wx, wy, **self._active_floor_spawn_kwargs())
        except Exception as e:
            import traceback
            print(f"[FPV] camera drop: {e}")
            traceback.print_exc()

    def _spawn_camera(self, ps: dict, wx: float, wy: float, floor_base_z: float = None, wall_height_m: float = None, room_name: str = None):
        """floor_base_z/wall_height_m — see _spawn_light's docstring.
        room_name, when given (the AI agent placing this in a specific
        labeled room), is stamped as custom data on the rig prim so the
        Camera List in Floor Plan Tool can show which room a camera is
        actually in instead of just its generic spawn name — read back via
        Usd.ModelAPI-independent GetCustomDataByKey, purely additive
        metadata that doesn't affect the camera itself."""
        try:
            import omni.usd
            import omni.kit.commands
            from pxr import UsdGeom, Gf, Sdf

            stage = omni.usd.get_context().get_stage()
            if not stage:
                return

            # Prefer whichever top-level prim ALREADY has a FloorPlanRig
            # child (the root floorplan.tool itself established) over a
            # naive "first child" guess — USD doesn't guarantee creation
            # order survives in GetChildren(), and floorplan.tool's own
            # add_camera/add_light already hit this exact bug: a plain
            # first-child guess can land a NEW camera/light under a
            # DIFFERENT top-level prim (e.g. /World) than the one
            # everything else (switch_to_cam, the camera/light lists) was
            # built under (e.g. /Render) — confirmed live: an AI-agent
            # camera spawned this way never showed up as switchable at
            # all, silently refused by switch_to_cam, while floorplan.tool's
            # own manually-added cameras (using the fixed lookup below)
            # worked fine. See floorplan.tool's own STAGE_ROOT comment for
            # the original bug report this mirrors.
            roots = stage.GetPseudoRoot().GetChildren()
            _existing_root = next(
                (str(c.GetPath()) for c in roots if stage.GetPrimAtPath(f"{c.GetPath()}/FloorPlanRig")),
                None)
            stage_root = _existing_root or (str(roots[0].GetPath()) if roots else "/World")
            rig_path   = f"{stage_root}/FloorPlanRig"
            cams_path  = f"{rig_path}/Cameras"
            for p in (rig_path, cams_path):
                if not stage.GetPrimAtPath(p):
                    stage.DefinePrim(p, "Xform")

            self._cam_spawn_count += 1
            base = f"{cams_path}/Cam_FPV_{self._cam_spawn_count:02d}_{ps['label']}"
            try:
                cam_path = omni.usd.get_stage_next_free_path(stage, base, False)
            except Exception:
                cam_path = base
            optical_path = cam_path + "/Camera"

            fz = self._floor_z() if floor_base_z is None else floor_base_z
            wall_height_units = self._ft_to_units(wall_height_m / 0.3048) if wall_height_m else None
            h  = self._capped_spawn_height_units(self._spawn_height_ft, wall_height_units)

            # Rig: an Xform holding world position and yaw (rotateZ) only —
            # mirrors floorplan.tool's add_camera/_cam_optical_path. Keeping
            # yaw separate from tilt/roll (which live on the child Camera
            # prim below) means panning this camera later can never bleed
            # into them.
            stage.DefinePrim(cam_path, "Xform")
            rig_prim = stage.GetPrimAtPath(cam_path)
            UsdGeom.Xformable(rig_prim).ClearXformOpOrder()
            rig_xf = UsdGeom.XformCommonAPI(rig_prim)
            rig_xf.SetTranslate(Gf.Vec3d(wx, wy, fz + h))
            rig_xf.SetRotate(Gf.Vec3f(0, 0, 0), UsdGeom.XformCommonAPI.RotationOrderXYZ)
            if room_name:
                rig_prim.SetCustomDataByKey("planlift:room", room_name)

            omni.kit.commands.execute("CreatePrimWithDefaultXformCommand",
                                      prim_type="Camera",
                                      prim_path=optical_path,
                                      select_new_prim=False)
            prim = stage.GetPrimAtPath(optical_path)
            if not prim.IsValid():
                UsdGeom.Camera.Define(stage, optical_path)
                prim = stage.GetPrimAtPath(optical_path)

            cam = UsdGeom.Camera(prim)
            cam.GetFocalLengthAttr().Set(ps["focal_mm"])
            cam.GetHorizontalApertureAttr().Set(ps["h_ap"])
            cam.GetVerticalApertureAttr().Set(ps["v_ap"])
            cam.GetClippingRangeAttr().Set(Gf.Vec2f(0.001, 1_000_000.0))
            prim.CreateAttribute("info:res_w", Sdf.ValueTypeNames.Int).Set(ps["res_w"])
            prim.CreateAttribute("info:res_h", Sdf.ValueTypeNames.Int).Set(ps["res_h"])

            # Force-unlocked: Kit's viewport menubar camera tool stores a
            # per-camera "omni:kit:cameraLock" bool (the little padlock next
            # to the camera name); when set, both WASD/mouse navigation
            # while looking through that camera AND its own transform
            # manipulator in the viewport are frozen. New cameras created
            # via Kit's default commands can come out of the box locked —
            # explicitly clearing it here guarantees every camera this app
            # spawns (button, drag-drop, or the AI agent) is immediately
            # navigable and draggable, no manual unlock click required.
            prim.CreateAttribute("omni:kit:cameraLock", Sdf.ValueTypeNames.Bool, custom=True).Set(False)

            # Translate stays at the rig's origin — position lives on the rig.
            UsdGeom.Xformable(prim).ClearXformOpOrder()
            xf = UsdGeom.XformCommonAPI(prim)
            xf.SetTranslate(Gf.Vec3d(0, 0, 0))
            xf.SetRotate(Gf.Vec3f(90, 0, 0),
                         UsdGeom.XformCommonAPI.RotationOrderXYZ)

            print(f"[FPV] spawned: {cam_path} at {self._spawn_height_ft:.1f} ft")
            self._notify_fpt_camera_added(cam_path)
            self._schedule_rebuild()
            return cam_path
        except Exception as e:
            import traceback
            print(f"[FPV] spawn_camera: {e}")
            traceback.print_exc()
            return None

    def _notify_fpt_camera_added(self, cam_path: str):
        """Push the newly spawned camera into Floor Plan Tool's camera list
        and clean up the ghost cone that was showing during the drag."""
        try:
            from floorplan.tool import extension as _fpt_mod
            fpt = getattr(_fpt_mod, "_INSTANCE", None)
            if fpt is None:
                return
            # Stop the ghost cone — the drop landed on Floor Plan View,
            # not the viewport, so FPT never got a chance to clean up.
            try:
                _fpt_mod.FloorPlanToolExtension._drag_active = False
                fpt._drag_active = False
            except Exception:
                pass
            try:
                if hasattr(fpt, "_destroy_ghost_cone"):
                    fpt._destroy_ghost_cone()
            except Exception:
                pass
            # Add the camera to FPT's list and rebuild its UI.
            if hasattr(fpt, "_cam_list") and cam_path not in fpt._cam_list:
                fpt._cam_list.append(cam_path)
            if hasattr(fpt, "_do_schedule_rebuild"):
                fpt._do_schedule_rebuild()
        except Exception as e:
            print(f"[FPV] fpt notify: {e}")

    def _spawn_light(self, wx: float, wy: float, floor_base_z: float = None, wall_height_m: float = None, room_name: str = None):
        """Create an individual light directly (mirrors _spawn_camera — this
        extension creates the prim itself rather than calling back into
        Floor Plan Tool), using the Intensity/Width/Depth/Temp sliders
        already exposed via FPT's self._ds, and FPT's fixed ceiling spawn
        height converted through this extension's own unit heuristic
        (same precedent as _spawn_camera's self._ft_to_units).

        floor_base_z/wall_height_m, when given (the AI agent placing
        something on a specific floor of a multi-floor building), override
        the whole-stage floor/ceiling measurement _floor_z()/
        _capped_spawn_height_units() would otherwise use — see
        _capped_spawn_height_units's docstring for why that matters.

        room_name, when given, is stamped as custom data on the light prim —
        see _spawn_camera's docstring for why."""
        try:
            import omni.usd
            from pxr import UsdGeom, UsdLux, Gf, Sdf

            stage = omni.usd.get_context().get_stage()
            if not stage:
                return

            # Same existing-root lookup _spawn_camera uses — see its
            # comment for why a naive first-child guess silently puts new
            # prims under the wrong top-level root.
            roots = stage.GetPseudoRoot().GetChildren()
            _existing_root = next(
                (str(c.GetPath()) for c in roots if stage.GetPrimAtPath(f"{c.GetPath()}/FloorPlanRig")),
                None)
            stage_root      = _existing_root or (str(roots[0].GetPath()) if roots else "/World")
            rig_path        = f"{stage_root}/FloorPlanRig"
            ind_lights_path = f"{rig_path}/IndLights"
            for p in (rig_path, ind_lights_path):
                if not stage.GetPrimAtPath(p):
                    stage.DefinePrim(p, "Xform")

            from floorplan.tool import extension as _fpt_mod
            fpt = getattr(_fpt_mod, "_INSTANCE", None)
            if fpt is None or not getattr(fpt, "_ds", None):
                print("[FPV] spawn_light: Floor Plan Tool not ready")
                return
            ds = fpt._ds

            idx  = ds["ind_light_count"][0]; ds["ind_light_count"][0] += 1
            base = f"{ind_lights_path}/Light_FPV_{idx:02d}"
            try:
                light_path = omni.usd.get_stage_next_free_path(stage, base, False)
            except Exception:
                light_path = base

            UsdLux.RectLight.Define(stage, light_path)
            prim = stage.GetPrimAtPath(light_path)
            set_attr = ds["set_attr"]
            set_attr(prim, "width", float(ds["sl_lwidth"].model.get_value_as_float()), Sdf.ValueTypeNames.Float)
            set_attr(prim, "height", float(ds["sl_ldepth"].model.get_value_as_float()), Sdf.ValueTypeNames.Float)
            set_attr(prim, "intensity", float(ds["sl_intensity"].model.get_value_as_float()), Sdf.ValueTypeNames.Float)
            set_attr(prim, "exposure", 0.0, Sdf.ValueTypeNames.Float)
            set_attr(prim, "colorTemperature", float(ds["sl_temp"].model.get_value_as_float()), Sdf.ValueTypeNames.Float)
            set_attr(prim, "enableColorTemperature", True, Sdf.ValueTypeNames.Bool)
            set_attr(prim, "color", Gf.Vec3f(1, 1, 1), Sdf.ValueTypeNames.Color3f)

            fz = self._floor_z() if floor_base_z is None else floor_base_z
            wall_height_units = self._ft_to_units(wall_height_m / 0.3048) if wall_height_m else None
            h  = self._capped_spawn_height_units(getattr(fpt, "_LIGHT_SPAWN_HEIGHT_FT", 10.0), wall_height_units)
            UsdGeom.Xformable(prim).ClearXformOpOrder()
            xf = UsdGeom.XformCommonAPI(prim)
            xf.SetTranslate(Gf.Vec3d(wx, wy, fz + h))
            xf.SetRotate(Gf.Vec3f(0, 0, 0), UsdGeom.XformCommonAPI.RotationOrderXYZ)
            if room_name:
                prim.SetCustomDataByKey("planlift:room", room_name)

            print(f"[FPV] spawned light: {light_path}")
            self._notify_fpt_light_added(light_path)
            self._schedule_rebuild()
            return light_path
        except Exception as e:
            import traceback
            print(f"[FPV] spawn_light: {e}")
            traceback.print_exc()
            return None

    def _notify_fpt_light_added(self, light_path: str):
        """Push the newly spawned light into Floor Plan Tool's individual
        light list and clean up the ghost light that was showing during
        the drag (mirrors _notify_fpt_camera_added)."""
        try:
            from floorplan.tool import extension as _fpt_mod
            fpt = getattr(_fpt_mod, "_INSTANCE", None)
            if fpt is None:
                return
            try:
                _fpt_mod.FloorPlanToolExtension._drag_active = False
                fpt._drag_active = False
            except Exception:
                pass
            try:
                if hasattr(fpt, "_destroy_ghost_cone"):
                    fpt._destroy_ghost_cone()
            except Exception:
                pass
            if hasattr(fpt, "_ind_light_list") and light_path not in fpt._ind_light_list:
                fpt._ind_light_list.append(light_path)
            if hasattr(fpt, "_do_schedule_light_list_rebuild"):
                fpt._do_schedule_light_list_rebuild()
        except Exception as e:
            print(f"[FPV] fpt light notify: {e}")

    # ── Camera xform helpers ──────────────────────────────────────────────────

    def _get_cam_xform(self, path):
        try:
            import omni.usd
            from pxr import UsdGeom, Usd
            stage = omni.usd.get_context().get_stage()
            if not stage:
                return None, None
            prim = stage.GetPrimAtPath(path)
            if not prim or not prim.IsValid():
                return None, None
            vecs = UsdGeom.XformCommonAPI(prim).GetXformVectors(Usd.TimeCode.Default())
            return list(vecs[0]), list(vecs[1])
        except Exception:
            return None, None

    def _set_cam_xform(self, path, t, r):
        try:
            import omni.usd
            from pxr import UsdGeom, Gf
            stage = omni.usd.get_context().get_stage()
            if not stage:
                return
            prim = stage.GetPrimAtPath(path)
            if not prim or not prim.IsValid():
                return
            xf = UsdGeom.XformCommonAPI(prim)
            xf.SetTranslate(Gf.Vec3d(*t))
            xf.SetRotate(Gf.Vec3f(*r), UsdGeom.XformCommonAPI.RotationOrderXYZ)
        except Exception as e:
            print(f"[FPV] set_cam_xform: {e}")

    # ── OS cursor position (Windows) ──────────────────────────────────────────

    def _cursor_kit_pos(self):
        try:
            import ctypes
            import omni.appwindow

            class _PT(ctypes.Structure):
                _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]

            aw    = omni.appwindow.get_default_app_window()
            title = aw.get_title() if hasattr(aw, "get_title") else ""
            hwnd  = ctypes.windll.user32.FindWindowW(None, title or None)
            if not hwnd:
                hwnd = ctypes.windll.user32.GetForegroundWindow()
            origin = _PT(0, 0)
            ctypes.windll.user32.ClientToScreen(hwnd, ctypes.byref(origin))
            cursor = _PT()
            ctypes.windll.user32.GetCursorPos(ctypes.byref(cursor))
            return float(cursor.x - origin.x), float(cursor.y - origin.y)
        except Exception:
            return None, None

    # ── Viewport camera selection ─────────────────────────────────────────────

    def _select_camera(self, camera_path: str):
        print(f"[FPV] selecting: {camera_path}")
        self._viewport_camera = camera_path
        # The actual Camera prim for a placed camera lives at
        # camera_path + "/Camera" (see floorplan.tool's _cam_optical_path)
        # — camera_path itself is the rig (position + yaw only). The
        # built-in Perspective camera has no rig, so this falls through
        # to camera_path unchanged if floorplan.tool isn't available.
        optical_path = camera_path
        try:
            from floorplan.tool import extension as _fpt_mod
            fpt = getattr(_fpt_mod, "_INSTANCE", None)
            if fpt and fpt._ds and "cam_optical_path" in fpt._ds:
                optical_path = fpt._ds["cam_optical_path"](camera_path)
        except Exception:
            pass
        # Switch the 3D viewport
        try:
            import omni.kit.viewport.utility as _vpu
            vp = _vpu.get_active_viewport()
            if vp is not None:
                try:
                    vp.camera_path = optical_path
                except AttributeError:
                    vp.set_active_camera(optical_path)
        except Exception as e:
            print(f"[FPV] viewport switch: {e}")
        # Show this camera's actual height in the Cam: field
        self._update_height_field_from_cam(camera_path)
        # Sync the Floor Plan Tool's active-camera highlight
        try:
            from floorplan.tool import extension as _fpt_mod
            fpt = getattr(_fpt_mod, "_INSTANCE", None)
            if fpt and fpt._ds:
                fpt._ds["switch_to_cam"](camera_path)
        except Exception as e:
            print(f"[FPV] fpt cam sync: {e}")

    def _get_active_viewport_camera(self):
        try:
            import omni.kit.viewport.utility as _vpu
            vp = _vpu.get_active_viewport()
            if vp is not None:
                try:
                    return str(vp.camera_path)
                except AttributeError:
                    return str(vp.get_active_camera())
        except Exception:
            pass
        return None

    # ── Scene helpers ─────────────────────────────────────────────────────────

    # _floor_plane_bounds()/_floor_z() each do a UsdGeom.BBoxCache pass, and
    # _floor_z()'s in particular is over the WHOLE stage (GetPseudoRoot()) —
    # confirmed live as part of why the floorplan.tool Up/Dn joystick lags:
    # _update_height_field_from_cam calls both of these on EVERY frame while
    # dragging (many times a second), even though the camera moving doesn't
    # change either result — both only depend on the building's own
    # geometry. Same short-TTL cache pattern as floorplan.tool's
    # get_floor_info() fix, just as instance attributes since these are
    # methods rather than nested closures.
    def _floor_plane_bounds(self):
        import time as _fpb_time
        now = _fpb_time.time()
        cached = getattr(self, '_fpb_cache', None)
        if cached is not None and (now - getattr(self, '_fpb_cache_ts', 0.0)) < 0.5:
            return cached
        result = self._floor_plane_bounds_uncached()
        self._fpb_cache = result
        self._fpb_cache_ts = now
        return result

    def _floor_plane_bounds_uncached(self):
        try:
            import omni.usd
            from pxr import Usd, UsdGeom
            stage = omni.usd.get_context().get_stage()
            if not stage:
                return None
            bc = UsdGeom.BBoxCache(
                Usd.TimeCode.Default(),
                [UsdGeom.Tokens.default_, UsdGeom.Tokens.render],
                useExtentsHint=True,
            )
            # Prefer the actual floor-plan-image plane's own bounds. As of
            # api_server.py's isaac worker sizing that plane from the
            # browser's world_scale, it's pixel-perfect to editor.html's true
            # image footprint — reading it back directly is exact, no
            # bbox+padding guess needed. A guessed bbox (the /PlanLift_Furniture
            # fallback below) is normally SMALLER than the true image (any
            # blank margin around the building isn't covered by geometry),
            # which is what made camera markers here read a little off from
            # where they actually sit relative to the pillar/wall geometry.
            img_prim = stage.GetPrimAtPath("/PlanLift_FloorPlanImage")
            if img_prim and img_prim.IsValid():
                rng = bc.ComputeWorldBound(img_prim).ComputeAlignedRange()
                if not rng.IsEmpty():
                    mn, mx = rng.GetMin(), rng.GetMax()
                    return mn[0], mx[0], mn[1], mx[1]

            # Fallback for older scenes without that plane (or an empty one).
            prim   = stage.GetPrimAtPath("/PlanLift_Furniture")
            target = prim if (prim and prim.IsValid()) else stage.GetPseudoRoot()
            rng = bc.ComputeWorldBound(target).ComputeAlignedRange()
            if rng.IsEmpty():
                return None
            mn, mx = rng.GetMin(), rng.GetMax()
            hw = abs(mx[0] - mn[0]) / 2.0 * 1.08
            hd = abs(mx[1] - mn[1]) / 2.0 * 1.08
            cx = (mn[0] + mx[0]) / 2.0
            cy = (mn[1] + mx[1]) / 2.0
            return cx - hw, cx + hw, cy - hd, cy + hd
        except Exception as e:
            print(f"[FPV] bounds: {e}")
            return None

    def _floor_z(self):
        import time as _fz_time
        now = _fz_time.time()
        cached = getattr(self, '_fz_cache', None)
        if cached is not None and (now - getattr(self, '_fz_cache_ts', 0.0)) < 0.5:
            return cached
        result = self._floor_z_uncached()
        self._fz_cache = result
        self._fz_cache_ts = now
        return result

    def _floor_z_uncached(self):
        try:
            import omni.usd
            from pxr import Usd, UsdGeom
            stage = omni.usd.get_context().get_stage()
            if not stage:
                return 0.0
            bc  = UsdGeom.BBoxCache(Usd.TimeCode.Default(),
                                    [UsdGeom.Tokens.default_],
                                    useExtentsHint=True)
            rng = bc.ComputeWorldBound(stage.GetPseudoRoot()).ComputeAlignedRange()
            return float(rng.GetMin()[2]) if not rng.IsEmpty() else 0.0
        except Exception:
            return 0.0

    def _ceiling_z(self):
        """Top of the actual wall geometry (/PlanLift_Furniture's own bbox
        max Z — same anchor prim _floor_plane_bounds() already trusts for
        this scene's real footprint), or None if it can't be measured yet.
        Reads the real, as-built wall height instead of assuming any
        particular number, so a spawn height that happens to match or
        exceed it can be caught regardless of what the building was
        actually configured with."""
        try:
            import omni.usd
            from pxr import Usd, UsdGeom
            stage = omni.usd.get_context().get_stage()
            if not stage:
                return None
            prim = stage.GetPrimAtPath("/PlanLift_Furniture")
            if not (prim and prim.IsValid()):
                return None
            bc = UsdGeom.BBoxCache(Usd.TimeCode.Default(),
                                    [UsdGeom.Tokens.default_, UsdGeom.Tokens.render],
                                    useExtentsHint=True)
            rng = bc.ComputeWorldBound(prim).ComputeAlignedRange()
            return float(rng.GetMax()[2]) if not rng.IsEmpty() else None
        except Exception:
            return None

    def _capped_spawn_height_units(self, desired_ft, wall_height_units=None):
        """Convert a desired camera/light spawn height (ft) to stage units,
        capped 0.5 ft below the real ceiling for this scene. Without this,
        a spawn height that matches or exceeds the actual wall height (the
        camera tool's own default is 9 ft, the light tool's is 10 ft —
        either one lands exactly at or above a typical 8-9 ft wall) places
        the camera/light at or above the ceiling, invisible from inside the
        room. Only ever lowers the requested height, never raises it, and
        falls back to the plain conversion if the walls can't be measured
        yet (nothing loaded, or an odd/zero-height bbox).

        wall_height_units, when given, is trusted directly instead of being
        derived from _ceiling_z()/_floor_z() — those measure the WHOLE
        stage's bbox, which for a multi-floor building spans every stacked
        floor (the topmost floor's ceiling, not the specific floor this
        item is actually being spawned on). Callers placing something on a
        known floor (the AI agent, given that floor's real wall height from
        editor.html) should pass it; single-floor callers leave it out and
        get the old whole-stage-bbox behavior unchanged."""
        h_units = self._ft_to_units(desired_ft)
        if wall_height_units is None:
            cz = self._ceiling_z()
            if cz is None:
                return h_units
            wall_height_units = cz - self._floor_z()
        if wall_height_units <= 0:
            return h_units
        margin_units = self._ft_to_units(0.5)
        max_units = max(margin_units, wall_height_units - margin_units)
        return min(h_units, max_units)

    def _floor_z_ranges(self):
        """{floorKey(str): (z_min, z_max)} of each floor's geometry group
        under /PlanLift_Furniture — those groups are named floor_0, floor_1,
        ... by editor.html's own build3D()/renderFloorLevel (confirmed
        directly on a converted USD earlier this session). floorKey here
        uses the same 1-indexed convention as self._floor_images'/
        set_floor_images()'s keys (editor.html's floorKey — floor_N's own
        index + 1), so a lookup by self._active_floor works directly.
        Assumes sequential floor numbering (no gaps) — matches the same
        assumption floorplan.tool's own per-floor "Floors" visibility tab
        already makes."""
        try:
            import omni.usd
            from pxr import Usd, UsdGeom
            stage = omni.usd.get_context().get_stage()
            if not stage:
                return {}
            furniture = stage.GetPrimAtPath("/PlanLift_Furniture")
            if not (furniture and furniture.IsValid()):
                return {}
            bc = UsdGeom.BBoxCache(Usd.TimeCode.Default(),
                                    [UsdGeom.Tokens.default_, UsdGeom.Tokens.render],
                                    useExtentsHint=True)
            ranges = {}
            for p in Usd.PrimRange(furniture):
                name = p.GetName()
                if not name.startswith("floor_") or p.GetTypeName() != "Xform":
                    continue
                try:
                    idx0 = int(name.split("_", 1)[1])
                except ValueError:
                    continue
                rng = bc.ComputeWorldBound(p).ComputeAlignedRange()
                if rng.IsEmpty():
                    continue
                ranges[str(idx0 + 1)] = (float(rng.GetMin()[2]), float(rng.GetMax()[2]))
            return ranges
        except Exception as e:
            print(f"[FPV] floor_z_ranges: {e}")
            return {}

    def _camera_data(self):
        try:
            import omni.usd
            from pxr import Usd, UsdGeom, Gf
            stage = omni.usd.get_context().get_stage()
            if not stage:
                return []
            result = []
            for prim in Usd.PrimRange(stage.GetPseudoRoot()):
                if prim.GetTypeName() != "Camera":
                    continue
                prim_path_str = str(prim.GetPath())
                if "/OmniverseKit_" in prim_path_str or "_GhostCamera" in prim_path_str:
                    continue
                mat = UsdGeom.Xformable(prim).ComputeLocalToWorldTransform(
                    Usd.TimeCode.Default())
                pos = mat.ExtractTranslation()
                fwd = Gf.Matrix4d(mat).TransformDir(Gf.Vec3d(0, 0, -1))
                angle_deg = math.degrees(math.atan2(fwd[1], fwd[0]))
                # Placed cameras are a rig+Camera pair (see floorplan.tool's
                # _cam_optical_path) — the rig, i.e. this prim's parent, is
                # the identity everything else (markers, drag, rotate ring)
                # keys off, since position and yaw live there, not on this
                # Camera prim itself (which only holds tilt/roll, fixed at
                # local origin).
                rig_path = str(prim.GetPath().GetParentPath())
                result.append((float(pos[0]), float(pos[1]), angle_deg,
                                prim.GetName(), rig_path, float(pos[2])))
            return result
        except Exception as e:
            print(f"[FPV] camera_data: {e}")
            return []

    def _light_data(self):
        """Individually-dropped lights, read via Floor Plan Tool's tracked
        list rather than a stage-wide type scan (keeps this in sync with
        the exact set shown in FPT's "Individual Lights" list)."""
        try:
            from floorplan.tool import extension as _fpt_mod
            fpt = getattr(_fpt_mod, "_INSTANCE", None)
            if fpt is None or not hasattr(fpt, "_ind_light_list"):
                return []
            result = []
            for p in list(fpt._ind_light_list):
                t, _r = self._get_cam_xform(p)
                if t is None:
                    continue
                result.append((float(t[0]), float(t[1]), p, float(t[2])))
            return result
        except Exception as e:
            print(f"[FPV] light_data: {e}")
            return []

    # ── Floor plan image polling ──────────────────────────────────────────────

    def _on_update(self, _event):
        self._poll_tick += 1
        if self._poll_tick % 90 != 0:
            return
        if self._floor_images:
            # Multi-floor building: set_floor_images()/_select_floor()
            # already picked and is showing a specific floor's image —
            # the newest-file glob below would otherwise fight that choice
            # every 90 ticks (per-floor images also match *_floorplan.png,
            # so "newest" would just be whichever floor was captured last
            # in the browser's export loop, not the user's actual selection).
            return
        if not ISAAC_TMP.exists():
            return
        files = sorted(ISAAC_TMP.glob("*_floorplan.png"),
                       key=lambda f: f.stat().st_mtime, reverse=True)
        if not files:
            return
        latest = str(files[0])
        mtime  = files[0].stat().st_mtime
        if latest != self._floorplan_path or mtime != self._fp_image_mtime:
            self._floorplan_path = latest
            self._fp_image_mtime = mtime
            print(f"[FPV] new floor plan: {files[0].name}")
            self._show_floorplan(latest)
