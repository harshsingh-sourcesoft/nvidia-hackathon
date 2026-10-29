import asyncio
import re
import carb
import omni.ext
import omni.kit.app

USD_FILE = "D:/Piyush/Blender_usd/Blender_usd/new_update/updated_timelapse.usd"

_HIDE_WINDOWS = (
    "Content", "Render Settings",
    "Semantics Schema Editor", "Metrics Assembler",
    "Layer",
    # Stage/Property/Properties/Console intentionally left out — kept
    # visible for debugging (see the explicit force-show step in
    # _apply_layout below). Console specifically needs to stay visible so
    # the DEBUG window-title dump can actually be read in-app.
    # Toolbar/Main ToolBar also intentionally left out (used to be hidden)
    # — for debugging, full navigation is wanted: select + move/rotate/scale
    # gizmo, not just camera fly. Hiding this window was also what crashed
    # the "W" hotkey (TransformButtonGroup never got to build _move_button
    # since its window never actually rendered) — showing it fixes that too.
)

# Excludes "window"/"door" — those name the whole assembly (frame + glass),
# not just the glass pane, and were catching window-frame and door materials
# themselves (e.g. "Window_Frame_Wood", "Door_Material") and overriding them
# with glass. Real glass panes are still reliably caught without them: Pass 0
# matches the web app's own trusted "PlanLift_Glass" naming, Pass 1 catches
# actual transparency data (opacity/transmission/alpha_mode), and "pane"/
# "glazing"/"glass" below still match a pane-specific name.
_GLASS_KEYWORDS = (
    "glass", "slider", "glaze", "glazing",
    "transparent", "curtain", "pane",
)

# Stricter keywords for direct mesh-name matching (Pass 3). Deliberately
# excludes "window"/"door" since those often name the whole assembly
# (frame + glass), not just the glass pane.
_GLASS_MESH_KEYWORDS = ("glass", "pane", "glazing", "vidro", "vidrio")


# ── AI Agent panel ───────────────────────────────────────────────────────────
# A small native Kit window where the user types a natural-language request
# (e.g. "add a light in the kitchen") once Isaac Sim is open. Room names and
# their pixel-space centroids are handed in via set_agent_context(), called
# from the injected --exec script right after furniture injection finishes
# (see api_server.py's _isaac_worker) — everything from there on runs
# entirely inside Kit against the already-open stage: no relaunch needed per
# prompt, unlike the furniture/floorplan setup which only happens once.
_agent_rooms         = []   # [{"name": str, "cx": float, "cy": float}]
_agent_world_scale   = 0.0
_agent_img_w         = 0
_agent_img_h         = 0
_agent_panel         = None   # the live _AIAgentPanel instance, once created
_agent_light_counter = 0      # each "add a light" gets its own prim path
_agent_camera_counter    = 0
_agent_furniture_counter = 0


def _format_labeled_rooms_line():
    """Text for the small line under the "Ready - N room(s) labeled." status
    — names, not just a count, so the user knows exactly which words to use
    in a prompt (e.g. must match "kitchen" below, not "Kitchen 1")."""
    names = [r.get("name") for r in _agent_rooms if r.get("name")]
    if not names:
        return "No rooms labeled yet — label rooms in the browser first."
    return "Rooms: " + ", ".join(names)

# Every item the AI agent has successfully placed, so a LATER prompt like
# "shift the garage sofa 1 feet left" can find which prim that refers to.
# Purely additive bookkeeping — appended to right after a placement already
# succeeds via the existing, untouched code path; never read or written by
# the add-furniture flow itself, so it can't change how a new item gets
# positioned. "name"/"alt_index" are for the Forward/Back cycle buttons in
# the panel's persistent placed-items list (see _cycle_placed_item) — the
# index into _category_alt_cache[category] this item is currently showing.
# [{"kind": "sofa", "category": "Furniture/Sofas", "room": "Garage",
#   "prim_path": "/PlanLift_AgentFurniture_3", "name": "Arnold", "alt_index": 0,
#   "prompt_id": 1, "prompt_text": "furnish the reception as a waiting area"}]
# prompt_id/prompt_text (every kind, not just furniture) group everything
# ONE prompt placed under a single card in the panel's placed-items list —
# see _render_placed_items — so a "furnish" or "add in all rooms" request
# that drops several items at once can be removed as one unit instead of
# item-by-item.
_agent_placed_items = []
_agent_prompt_counter = 0

# {category_folder: [{"name","url","thumb"}, ...]} — the full candidate
# list for a category, fetched once (either from a suggestions listing the
# user already saw, or lazily on first Forward/Back click) and reused for
# every item in that category rather than re-hitting NVIDIA's bucket on
# every cycle click.
_category_alt_cache = {}


def _get_category_alternatives(kind, category_folder):
    """Blocking — run via run_in_executor, same pattern as
    _list_nvidia_assets (which this calls through to on a cache miss)."""
    if category_folder and category_folder in _category_alt_cache:
        return _category_alt_cache[category_folder]
    nv_result = _list_nvidia_assets(kind, limit=24)
    items = nv_result.get("items", [])
    if category_folder:
        _category_alt_cache[category_folder] = items
    return items

# ── "Move/rotate an existing item" parsing ──────────────────────────────
# Handled entirely locally (regex, no LLM round-trip) — separate and
# additive to the existing add-furniture flow below, which is untouched:
# _handle_prompt checks these FIRST and only intercepts prompts that
# unambiguously match (a trigger verb + the specific numbers each needs);
# anything that doesn't match falls through to the normal add flow exactly
# as before.
_MOVE_TRIGGER_RE = re.compile(r"\b(shift|move|reposition)\b", re.IGNORECASE)
_MOVE_DIST_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(feet|foot|ft|meters?|metres?|m)\b", re.IGNORECASE)
_MOVE_DIR_RE = re.compile(r"\b(left|right|forward|back|backward|north|south|east|west)\b", re.IGNORECASE)

_ROTATE_TRIGGER_RE = re.compile(r"\b(rotate|turn|spin)\b", re.IGNORECASE)
_ROTATE_DEG_RE = re.compile(r"(-?\d+(?:\.\d+)?)\s*(?:deg(?:rees?)?|°)", re.IGNORECASE)
_ROTATE_SENSE_RE = re.compile(
    r"\b(clockwise|counter-?clockwise|anti-?clockwise|left|right)\b", re.IGNORECASE)

# "remove"/"delete" never collides with _MOVE_TRIGGER_RE's \bmove\b — the
# "move" inside "remove" has no word boundary before it (…re|move…), so
# \bmove\b can't match there.
_REMOVE_TRIGGER_RE = re.compile(r"\b(remove|delete)\b", re.IGNORECASE)

# Disambiguates which item a move/rotate/remove command means when more
# than one of the same kind is in the same room (e.g. two sofas) — without
# this, all three always fall back to the most-recently-placed match.
# "last" maps to -1 (Python's own "last element" index), everything else
# to a plain 0-based position in PLACEMENT order (oldest first).
_ORDINAL_WORDS = {
    "1st": 0, "first": 0,
    "2nd": 1, "second": 1,
    "3rd": 2, "third": 2,
    "4th": 3, "fourth": 3,
    "5th": 4, "fifth": 4,
    "last": -1,
}
_ORDINAL_RE = re.compile(r"\b(1st|first|2nd|second|3rd|third|4th|fourth|5th|fifth|last)\b", re.IGNORECASE)


def _resolve_ordinal_from_text(prompt_text):
    """Returns a 0-based placement-order index (or -1 for "last"), or None
    if no ordinal word appears in the text."""
    m = _ORDINAL_RE.search(prompt_text or "")
    return _ORDINAL_WORDS.get(m.group(1).lower()) if m else None


def _resolve_room_and_kind_from_text(prompt_text):
    """Which already-known room name / furniture keyword appears in the
    text — reusing the same vocabulary _resolve_room and
    _nvidia_category_for already use, rather than inventing a new one, so
    "the garage sofa" naturally finds room="Garage" (from _agent_rooms)
    and kind="sofa" (from _NVIDIA_CATEGORY_MAP). Shared by both the move
    and rotate parsers below."""
    needle = prompt_text.lower()
    room_name = None
    for r in _agent_rooms:
        rn = (r.get("name") or "").strip().lower()
        if rn and rn in needle:
            room_name = r.get("name")
            break
    kind = None
    for key, _folder, _dims in _NVIDIA_CATEGORY_MAP:
        if key in needle:
            kind = key
            break
    return room_name, kind


# Used when the trigger verb (+ direction, for move) is unambiguous but
# the prompt left out the magnitude — commit to the move/rotate
# interpretation anyway rather than silently falling through to the
# add-furniture flow just because one optional detail is missing. Per
# explicit request: "user can ask anything, so prompting should be...
# aware of the thing which user is asking" — a clear "shift"/"rotate"
# verb should never get reinterpreted as "add a new one".
_MOVE_DEFAULT_DISTANCE_M = 0.3048   # 1 ft
_ROTATE_DEFAULT_DEGREES = 90.0      # a quarter turn


def _parse_move_command(prompt_text):
    """Returns {"room_name", "kind", "direction", "distance_m",
    "distance_defaulted"} if `prompt_text` unambiguously reads as
    "shift/move X [<dist> <unit>] <direction>", else None. Distance is
    OPTIONAL — direction is the only thing that must be explicit, since
    without it there's no reasonable default to guess (which way?)."""
    if not _MOVE_TRIGGER_RE.search(prompt_text or ""):
        return None
    dir_match = _MOVE_DIR_RE.search(prompt_text)
    if not dir_match:
        return None

    dist_match = _MOVE_DIST_RE.search(prompt_text)
    if dist_match:
        dist_val = float(dist_match.group(1))
        unit = dist_match.group(2).lower()
        distance_m = dist_val * 0.3048 if unit.startswith("f") else dist_val
        distance_defaulted = False
    else:
        distance_m = _MOVE_DEFAULT_DISTANCE_M
        distance_defaulted = True

    room_name, kind = _resolve_room_and_kind_from_text(prompt_text)
    ordinal = _resolve_ordinal_from_text(prompt_text)

    return {
        "room_name": room_name,
        "kind": kind,
        "direction": dir_match.group(1).lower(),
        "distance_m": distance_m,
        "distance_defaulted": distance_defaulted,
        "ordinal": ordinal,
    }


def _parse_rotate_command(prompt_text):
    """Returns {"room_name", "kind", "degrees", "degrees_defaulted"} if
    `prompt_text` unambiguously reads as "rotate/turn/spin X [<N> degrees]
    [clockwise|counterclockwise|left|right]", else None. Z-axis only — the
    only axis that makes sense for furniture sitting on a floor, per
    explicit request. Sense word (if any) sets the sign: clockwise/right
    is negative, counterclockwise/left is positive (standard USD Z-up
    right-hand-rule convention — positive rotateZ turns counterclockwise
    viewed from above); a bare degree number with no sense word is applied
    as-is (so "rotate -90 degrees" still works). Degrees is OPTIONAL —
    "rotate the sofa clockwise" still commits to a rotate (default
    quarter-turn) rather than falling through to add."""
    if not _ROTATE_TRIGGER_RE.search(prompt_text or ""):
        return None

    deg_match = _ROTATE_DEG_RE.search(prompt_text)
    sense_match = _ROTATE_SENSE_RE.search(prompt_text)
    if deg_match:
        degrees = float(deg_match.group(1))
        degrees_defaulted = False
    else:
        degrees = _ROTATE_DEFAULT_DEGREES
        degrees_defaulted = True

    if sense_match:
        sense = sense_match.group(1).lower().replace("-", "")
        if sense in ("clockwise", "right"):
            degrees = -abs(degrees)
        elif sense in ("counterclockwise", "anticlockwise", "left"):
            degrees = abs(degrees)

    room_name, kind = _resolve_room_and_kind_from_text(prompt_text)
    ordinal = _resolve_ordinal_from_text(prompt_text)
    return {
        "room_name": room_name, "kind": kind, "degrees": degrees,
        "degrees_defaulted": degrees_defaulted, "ordinal": ordinal,
    }


def _parse_remove_command(prompt_text):
    """Returns {"room_name", "kind"} if `prompt_text` unambiguously reads
    as "remove/delete X [in/from <room>]", else None. Unlike move/rotate
    there's no magnitude to default — the trigger verb alone is enough to
    commit to the remove interpretation, same "never silently fall
    through to add just because a detail is missing" principle."""
    if not _REMOVE_TRIGGER_RE.search(prompt_text or ""):
        return None
    room_name, kind = _resolve_room_and_kind_from_text(prompt_text)
    ordinal = _resolve_ordinal_from_text(prompt_text)
    return {"room_name": room_name, "kind": kind, "ordinal": ordinal}


# ── "add/furnish X in the current camera view" ──────────────────────────
# A GENERAL override, not tied to one exact phrase — any "add"/"furnish"
# prompt that mentions the active camera instead of (or as well as) a
# labeled room gets positioned in front of whichever camera is currently
# showing in the viewport, built from that camera's own live world
# transform. Checked locally (no LLM involvement) purely to decide WHERE
# to place; the LLM still classifies WHAT to place exactly as before.
_CAMERA_VIEW_TRIGGER_RE = re.compile(
    r"\b(current camera|camera view|active camera|this camera view|current view|this view|"
    r"active view|in front of (?:the )?camera)\b",
    re.IGNORECASE,
)
_CAMERA_VIEW_DISTANCE_M = 3.0     # how far in front of the camera to place things
_CAMERA_VIEW_FLOOR_TOLERANCE_M = 0.5  # small buffer so a camera mounted right at its own floor's base still counts as "on" that floor


def _camview_dbg(msg):
    """File-based logging (not print/carb.log_info, which only go to a
    console a live session has no visibility into afterward) — same
    established pattern floorplan.tool's _camlayout_dbg already proved
    out for the camera Save/Restore bug: real evidence of exactly which
    step camera-view resolution reaches/fails at, instead of another
    guess."""
    try:
        import datetime as _dt_cv, os as _os_cv
        path = "D:/PlanLift-Backend/output/camera_layouts/camview_debug.txt"
        _os_cv.makedirs(_os_cv.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(_dt_cv.datetime.now().strftime("%H:%M:%S") + " " + msg + "\n")
    except Exception:
        pass


def _get_active_camera_prim(stage):
    """Returns the actual UsdGeom.Camera prim currently shown in the
    active viewport, or None. vp.camera_path is an "identity" path that
    for the built-in Perspective camera IS the Camera prim itself, but
    for a placed rig+Camera pair (floorplan.tool's and planlift_floorview's
    own rig/Camera convention — see floorplan.tool's _cam_optical_path) is
    the outer Xform rig. Checked by PRIM TYPE rather than by guessing
    which extension's path convention applies, so this works no matter
    which system created the currently-active camera."""
    try:
        import omni.kit.viewport.utility as vp_util
    except Exception as e:
        _camview_dbg(f"_get_active_camera_prim: vp_util import failed: {e}")
        return None
    vp = vp_util.get_active_viewport()
    if not vp:
        _camview_dbg("_get_active_camera_prim: get_active_viewport() returned None")
        return None
    cam_path = getattr(vp, "camera_path", None)
    _camview_dbg(f"_get_active_camera_prim: vp.camera_path={cam_path!r}")
    if not cam_path:
        return None
    prim = stage.GetPrimAtPath(cam_path)
    if prim and prim.IsValid() and prim.GetTypeName() == "Camera":
        _camview_dbg(f"_get_active_camera_prim: using {cam_path!r} directly (type=Camera)")
        return prim
    child = stage.GetPrimAtPath(f"{cam_path}/Camera")
    if child and child.IsValid() and child.GetTypeName() == "Camera":
        _camview_dbg(f"_get_active_camera_prim: using {cam_path}/Camera (rig child)")
        return child
    _camview_dbg(f"_get_active_camera_prim: neither {cam_path!r} nor {cam_path}/Camera is a valid Camera prim (type at cam_path={prim.GetTypeName() if prim and prim.IsValid() else 'invalid'!r})")
    return None


def _resolve_camera_view_target():
    """Blocking — run via run_in_executor. If a camera is active in the
    viewport, returns a synthetic "room" dict {"name","wx","wy","wz",
    "wallHeightM"} positioned _CAMERA_VIEW_DISTANCE_M directly in front of
    it at real FLOOR height (not a fixed offset below the camera — see
    below), or None if there's no stage/active camera. This is EXACTLY
    the shape _create_light/_create_camera/_create_furniture already
    expect (see their own "wx/wy/wz come straight from editor.html's
    _buildRoomsDataForAgent" docstrings) so the rest of the add/furnish
    flow needs zero other changes to accept it in place of a labeled
    room. Works with ZERO labeled rooms (falls back to ground-floor
    Z=0) — a labeled room only sharpens which floor's height to use when
    more than one exists, it is never required."""
    import omni.usd
    from pxr import Usd, UsdGeom, Gf
    stage = omni.usd.get_context().get_stage()
    if not stage:
        _camview_dbg("_resolve_camera_view_target: no stage open")
        return None
    cam_prim = _get_active_camera_prim(stage)
    if not cam_prim:
        _camview_dbg("_resolve_camera_view_target: _get_active_camera_prim returned None")
        return None
    _camview_dbg(f"_resolve_camera_view_target: cam_prim={cam_prim.GetPath()}")
    mat = UsdGeom.Xformable(cam_prim).ComputeLocalToWorldTransform(Usd.TimeCode.Default())
    # ExtractTranslation() is in this STAGE's own units (confirmed live:
    # 1 unit = 1cm, see _STAGE_UNITS_PER_METER), but every other field this
    # function computes/compares against (_CAMERA_VIEW_DISTANCE_M,
    # _CAMERA_VIEW_FLOOR_TOLERANCE_M, and _agent_rooms' own "wz") is real
    # METERS — matching this function's own documented contract ("wx/wy/wz
    # come straight from editor.html's _buildRoomsDataForAgent", i.e.
    # meters). Converting right here keeps the rest of this function in one
    # consistent unit instead of silently mixing stage units into a
    # meters-only calculation.
    pos = mat.ExtractTranslation() / _STAGE_UNITS_PER_METER
    # USD cameras look down their own local -Z axis. TransformDir (not
    # Transform) applies only the matrix's rotation/scale, not its
    # translation, since this is a direction, not a point.
    forward_world = mat.TransformDir(Gf.Vec3d(0, 0, -1))
    fx, fy = forward_world[0], forward_world[1]
    horiz_len = (fx * fx + fy * fy) ** 0.5
    if horiz_len < 1e-6:
        # Camera is looking almost straight up/down, where "in front of
        # it" on the floor is meaningless — fall back to its own local
        # +X (right) axis projected onto the floor instead.
        right_world = mat.TransformDir(Gf.Vec3d(1, 0, 0))
        fx, fy = right_world[0], right_world[1]
        horiz_len = (fx * fx + fy * fy) ** 0.5 or 1.0
    fx, fy = fx / horiz_len, fy / horiz_len
    gx = pos[0] + fx * _CAMERA_VIEW_DISTANCE_M
    gy = pos[1] + fy * _CAMERA_VIEW_DISTANCE_M

    # Floor height: many cameras in this app are ceiling/wall-mounted
    # surveillance cameras, well above a person's eye level, so a fixed
    # "camera Z minus an eye-height offset" (the previous approach) put
    # placed items floating in mid-air for anything but a low, walkthrough-
    # style camera — confirmed live as a real bug. Instead, use the
    # HIGHEST labeled room's floor-base (wz) that's still at or below the
    # camera's own Z (i.e. the floor the camera is mounted somewhere
    # ABOVE, not one it's below) — that's the floor this camera is
    # actually covering. Zero labeled rooms (or none below the camera,
    # e.g. it's sitting right at ground level) falls back to Z=0, the
    # same "0 for the ground floor" default _create_light already uses.
    floor_candidates = [
        r.get("wz", 0.0) for r in _agent_rooms
        if r.get("wz", 0.0) <= pos[2] + _CAMERA_VIEW_FLOOR_TOLERANCE_M
    ]
    gz = max(floor_candidates) if floor_candidates else 0.0
    _camview_dbg(
        f"_resolve_camera_view_target: pos={tuple(round(v,2) for v in pos)} "
        f"forward_xy=({fx:.2f},{fy:.2f}) -> target=({gx:.2f},{gy:.2f},{gz:.2f}) "
        f"floor_candidates={floor_candidates}")
    return {"name": "the current camera view", "wx": gx, "wy": gy, "wz": gz, "wallHeightM": 0.0}


# Converts a REAL-WORLD METERS value (what editor.html's
# _buildRoomsDataForAgent / WORLD_SCALE compute — see _create_light's own
# docstring) into this stage's own coordinate units. Confirmed live: this
# app's GLB->USD conversion composes the GLB's own meter-authored geometry
# straight into the stage with no compensating scale, and the stage ends
# up at 1 unit = 1 centimetre regardless (a wall's own Translate read back
# as (461.56, 274.32, -386.93) for a floor plan whose real footprint is
# ~15.7x11.8m — a clean ~100x ratio, confirmed against the live scene, not
# guessed) — but nothing in that conversion ever sets the stage's
# metersPerUnit METADATA to reflect it, so that can't be queried back
# reliably; it would just return USD's unauthored-stage default (1.0) and
# silently reintroduce this exact bug. Hardcoded to match the real,
# confirmed-live scene scale instead. If a future conversion pipeline
# change ever makes GLB content land directly in real meters-as-units,
# this is the one constant to update.
_STAGE_UNITS_PER_METER = 100.0


def _resolve_placed_item(kind, room_name, ordinal=None):
    """Finds the item in _agent_placed_items a move/rotate/remove command
    refers to — most-recently-placed match wins by default when more than
    one fits (e.g. two sofas in the same room), since that's the most
    likely thing "the sofa" means right after adding one. Pass `ordinal`
    (a 0-based placement-order index, or -1 for "last" — see
    _resolve_ordinal_from_text) to pick a SPECIFIC one instead, e.g. "the
    first sofa" / "the second sofa in yui".

    kind is matched by CATEGORY FOLDER (via _nvidia_category_for), not the
    literal word, so "shift the couch" finds something that was added as
    "sofa" — both normalize to the same Furniture/Sofas folder, same as
    the add flow already does.

    Returns (item, reason) — item is None on failure, and reason is a
    specific, log-able explanation (no items placed at all vs. wrong kind
    vs. right kind but wrong room vs. ordinal out of range) rather than
    one generic "not found"."""
    kind_folder, _fp = _nvidia_category_for(kind) if kind else (None, None)
    room_needle = (room_name or "").strip().lower()

    if not _agent_placed_items:
        return None, "No items have been placed by the AI agent yet in this session."

    # Newest-first (most-recently-placed match wins when ordinal is None).
    kind_matches = [
        it for it in reversed(_agent_placed_items)
        if not kind_folder or it.get("category") == kind_folder
    ]
    if not kind_matches:
        return None, f"Label \"{kind}\" does not exist - nothing matching that has been placed yet."

    candidates = kind_matches
    scope = f"\"{kind}\""
    if room_needle:
        candidates = [it for it in kind_matches if room_needle in it["room"].lower()]
        if not candidates:
            return None, f"No \"{kind}\" has been placed in \"{room_name}\" yet."
        scope = f"\"{kind}\" in \"{room_name}\""

    if ordinal is not None:
        # candidates is newest-first; ordinal counts in PLACEMENT order
        # (oldest first, matching how a person would say "the first one I
        # added"), and -1 ("last") should mean the newest — so reverse
        # back to chronological order before indexing.
        placement_order = list(reversed(candidates))
        if ordinal != -1 and ordinal >= len(placement_order):
            return None, f"Only {len(placement_order)} {scope} placed - asked for #{ordinal+1}."
        return placement_order[ordinal], None

    return candidates[0], None


def _pick_camera_preset_key(spec):
    """Maps a free-text spec like "4 mp" / "8MP" / "2 megapixel" onto one of
    planlift_floorview's _CAM_PRESETS keys ("2MP  (1920x1080)" etc — note
    the double space, must match exactly). Defaults to 4MP when nothing
    parses, since that's the size the panel's own default button uses."""
    m = re.search(r"(\d+)\s*-?\s*(mp|megapixel)", (spec or "").lower())
    n = int(m.group(1)) if m else 4
    if n <= 2:
        return "2MP  (1920x1080)"
    if n <= 4:
        return "4MP  (2560x1440)"
    return "8MP  (3840x2160)"


# NVIDIA's public Omniverse content bucket — no auth, no Nucleus server
# needed, plain HTTPS. Assets here are authored natively for Omniverse and
# already Z-up, so _create_furniture skips GLB conversion and the Y-up
# axis-fix rotate for these — but NOT scale correction: the library isn't
# consistently meter-scaled (one sofa measured 259 units wide — centimeters,
# not meters), so a category-guessed footprint is applied the same way it
# was for local-catalog items, plus the pivot-centering fix (still can't
# assume every asset's local origin sits at its own base/center).
_NVIDIA_ASSET_ROOT = "https://omniverse-content-production.s3-us-west-2.amazonaws.com"
_NVIDIA_ASSET_PREFIX = "Assets/ArchVis/Residential/"

# Keyword -> (category folder relative to _NVIDIA_ASSET_PREFIX, target
# footprint in meters). Matched by substring against whatever noun Ollama
# extracted. Order matters — more specific keys (checked first) must come
# before generic ones that would otherwise shadow them (e.g. "coffee
# table" before "table").
_NVIDIA_CATEGORY_MAP = [
    ("coffee table",  "Furniture/CoffeeTables", (1.0, 0.6, 0.4)),
    ("end table",     "Furniture/EndTables",    (0.5, 0.5, 0.55)),
    ("side table",    "Furniture/EndTables",    (0.5, 0.5, 0.55)),
    ("sofa table",    "Furniture/SofaTables",   (1.2, 0.35, 0.65)),
    ("console table", "Furniture/SofaTables",   (1.2, 0.35, 0.65)),
    ("dining table",  "Furniture/DiningSets",   (1.6, 0.9, 0.75)),
    ("dining set",    "Furniture/DiningSets",   (1.6, 0.9, 0.75)),
    ("media table",   "Furniture/MediaTables",  (1.4, 0.4, 0.45)),
    ("tv stand",      "Furniture/MediaTables",  (1.4, 0.4, 0.45)),
    ("table",         "Furniture/Tables",       (1.2, 0.75, 0.75)),
    ("sofa",          "Furniture/Sofas",        (2.0, 0.9, 0.85)),
    ("couch",         "Furniture/Sofas",        (2.0, 0.9, 0.85)),
    ("bar stool",     "Furniture/BarStools",    (0.4, 0.4, 0.75)),
    ("stool",         "Furniture/BarStools",    (0.4, 0.4, 0.75)),
    ("armchair",      "Furniture/Chairs",       (0.78, 0.9, 0.85)),
    ("chair",         "Furniture/Chairs",       (0.55, 0.55, 0.85)),
    ("bed",           "Furniture/Beds",         (1.5, 2.0, 0.6)),
    ("bookshelf",     "Furniture/Bookshelves",  (0.9, 0.3, 1.8)),
    ("shelf",         "Decor/Shelves",          (0.9, 0.3, 1.8)),
    ("desk",          "Furniture/Desks",        (1.2, 0.6, 0.75)),
    ("television",    "Electronics/Televisions", (1.2, 0.08, 0.7)),
    ("tv",            "Electronics/Televisions", (1.2, 0.08, 0.7)),
    ("speaker",       "Electronics/Speakers",   (0.25, 0.25, 0.35)),
    ("floor lamp",    "Lighting/Floor Lamps",   (0.35, 0.35, 1.5)),
    ("table lamp",    "Lighting/Table Lamps",   (0.3, 0.3, 0.55)),
    ("lamp",          "Lighting/Table Lamps",   (0.3, 0.3, 0.55)),
    ("chandelier",    "Lighting/Chandeliers",   (0.8, 0.8, 0.6)),
    ("plant",         "Plants",                 (0.5, 0.5, 1.2)),
    ("rug",           "Decor/Rugs",             (2.0, 1.5, 0.02)),
    ("carpet",        "Decor/Rugs",             (2.0, 1.5, 0.02)),
    ("mirror",        "Decor/Mirrors",          (0.6, 0.05, 1.2)),
    ("vase",          "Decor/Vases",            (0.25, 0.25, 0.4)),
    ("clock",         "Decor/Clocks",           (0.3, 0.05, 0.3)),
    ("book",          "Decor/Books",            (0.3, 0.2, 0.15)),
    ("sculpture",     "Decor/Sculptures",       (0.3, 0.3, 0.4)),
]

# Industrial/warehouse items — confirmed (via a direct listing of the
# public bucket) to live in a completely SEPARATE part of it: Isaac Sim's
# own SimReady Industrial/Warehouse asset set, not ArchVis/Residential at
# all — the residential furniture map above has nothing like a pallet or
# a forklift. Each entry here is a FULL "Assets/..." path rather than a
# name relative to _NVIDIA_ASSET_PREFIX (see _nvidia_category_for).
# Several deliberately have NO trailing slash (e.g. ".../Vehicle_Forklift")
# to use S3's plain-string prefix matching to sweep up every same-family
# variant (Vehicle_Forklift_Blue_A01, _Red_B01, …) in one category instead
# of hard-locking to a single specific model — do not "clean up" those by
# adding a trailing slash, it would narrow the match to nothing. Order
# matters here too: more specific keys ("hand pallet truck") before the
# generic ones they'd otherwise be shadowed by ("pallet").
_INDUSTRIAL_BASE = "Assets/Isaac/6.0/Isaac/SimReady/Industrial/"
_WAREHOUSE_BASE = _INDUSTRIAL_BASE + "Warehouse/"
_NVIDIA_CATEGORY_MAP_INDUSTRIAL = [
    ("hand pallet truck", _WAREHOUSE_BASE + "Equipment/Heavy_Duty_Pallet_Jack_A01/", (0.7, 1.5, 1.2)),
    ("pallet jack",       _WAREHOUSE_BASE + "Equipment/Heavy_Duty_Pallet_Jack_A01/", (0.7, 1.5, 1.2)),
    ("wooden pallet",     _WAREHOUSE_BASE + "Pallets/",                              (1.0, 1.2, 0.15)),
    ("pallet",            _WAREHOUSE_BASE + "Pallets/",                              (1.0, 1.2, 0.15)),
    ("cardboard box",     "Assets/simready_content/common_assets/props/cardbox_a1/", (0.4, 0.3, 0.3)),
    ("forklift",          _WAREHOUSE_BASE + "Equipment/Vehicle_Forklift",            (1.2, 2.3, 2.1)),
    ("storage rack",      _WAREHOUSE_BASE + "Racks/",                                (2.7, 0.9, 3.0)),
    ("shelving unit",     _WAREHOUSE_BASE + "Racks/",                                (2.7, 0.9, 3.0)),
    ("conveyor belt",     _WAREHOUSE_BASE + "Equipment/Conveyor_Belt",               (3.0, 0.7, 0.9)),
    ("warehouse cart",    _WAREHOUSE_BASE + "Equipment/Utility_Cart_E01/",           (0.6, 1.0, 1.0)),
    ("delivery trolley",  _WAREHOUSE_BASE + "Equipment/Utility_Cart_E01/",           (0.6, 1.0, 1.0)),
    ("plastic container", _WAREHOUSE_BASE + "Containers/Container_Plastic_Grit_Black_B01/", (0.6, 0.4, 0.4)),
    ("metal drum",        _WAREHOUSE_BASE + "Containers/Drum_Steel_A01/",            (0.6, 0.6, 0.9)),
    ("safety cone",       "Assets/simready_content/common_assets/props/heavydutytrafficcone_a01/", (0.3, 0.3, 0.7)),
    ("traffic cone",      "Assets/simready_content/common_assets/props/heavydutytrafficcone_a01/", (0.3, 0.3, 0.7)),
    ("fire extinguisher", _WAREHOUSE_BASE + "Equipment/Fire_Extinguisher",           (0.15, 0.15, 0.5)),
    ("barcode scanner",   _WAREHOUSE_BASE + "Equipment/Computer_Scanner_Barcode",    (0.15, 0.1, 0.2)),
    ("packing table",     _WAREHOUSE_BASE + "Equipment/Deluxe_Packing_Table_A01/",   (1.5, 0.75, 0.9)),
    ("tool cabinet",      _INDUSTRIAL_BASE + "Tools/Toolbox_Carry_Hard_Case_B01/",   (0.5, 0.35, 0.35)),
    ("safety helmet",     _INDUSTRIAL_BASE + "Safety/Hard_Hat_A01/",                 (0.25, 0.25, 0.2)),
    ("hard hat",          _INDUSTRIAL_BASE + "Safety/Hard_Hat_A01/",                 (0.25, 0.25, 0.2)),
    ("safety gloves",     _INDUSTRIAL_BASE + "Safety/Gloves_Leather_A01/",           (0.15, 0.05, 0.3)),
]
# Checked before the residential map so nothing there (e.g. generic
# "table") can shadow an industrial entry (e.g. "packing table").
_NVIDIA_CATEGORY_MAP = _NVIDIA_CATEGORY_MAP_INDUSTRIAL + _NVIDIA_CATEGORY_MAP


def _nvidia_category_for(kind):
    """Returns (full_bucket_prefix, footprint_dict) or (None, None) if
    nothing matched. A map entry's folder is either relative to
    _NVIDIA_ASSET_PREFIX (short name like "Furniture/Sofas" — the
    original ArchVis/Residential set) or a full "Assets/..." path used
    exactly as given (the Isaac SimReady Industrial set, elsewhere in the
    same bucket) — trailing slash or its absence is significant for the
    latter (see _NVIDIA_CATEGORY_MAP_INDUSTRIAL's own note), so it is
    never appended here."""
    needle = (kind or "").strip().lower()
    for key, folder, dims in _NVIDIA_CATEGORY_MAP:
        if key in needle:
            prefix = folder if folder.startswith("Assets/") else (_NVIDIA_ASSET_PREFIX + folder + "/")
            return prefix, {"widthM": dims[0], "depthM": dims[1], "heightM": dims[2]}
    return None, None


# Curated furniture SETS for the "furnish this room as a <type>" AI Agent
# request (action="furnish" from /api/agent — see route.ts's system
# prompt), distinct from the single-object "add" flow above. Each entry
# maps a room-type keyword to an ordered list of entity kinds, each
# resolved against _NVIDIA_CATEGORY_MAP exactly like a normal "add"
# request would (same _list_nvidia_assets search), just auto-picking the
# first result instead of showing suggestions to choose from — asking the
# user to confirm 5-6 items one at a time would defeat the point of a
# one-shot "furnish" request. First substring match wins, same technique
# _nvidia_category_for already uses.
_ROOM_TYPE_KITS = [
    ("reception",    ["desk", "chair", "chair", "sofa", "coffee table", "plant"]),
    ("waiting area", ["chair", "chair", "coffee table", "plant"]),
    ("lobby",        ["desk", "chair", "chair", "sofa", "plant"]),
    ("living room",  ["sofa", "coffee table", "tv stand", "armchair", "lamp", "rug"]),
    ("lounge",       ["sofa", "coffee table", "tv stand", "armchair", "lamp"]),
    ("office",       ["desk", "chair", "bookshelf", "table lamp"]),
    ("study",        ["desk", "chair", "bookshelf", "table lamp"]),
    ("bedroom",      ["bed", "table lamp", "bookshelf"]),
    ("dining room",  ["dining set", "plant"]),
    # Real industrial props — NVIDIA's public bucket also hosts Isaac
    # Sim's own SimReady Industrial/Warehouse asset set (a completely
    # different part of the bucket from ArchVis/Residential above; see
    # _NVIDIA_CATEGORY_MAP_INDUSTRIAL), confirmed by directly listing it:
    # real pallets, forklifts, storage racks, cardboard boxes, fire
    # extinguishers and traffic cones all exist there. A representative
    # 6-item mix: one big vehicle, one big structure, one mid-size prop,
    # two small safety props, one small clutter item.
    ("warehouse",    ["forklift", "storage rack", "wooden pallet", "cardboard box", "fire extinguisher", "safety cone"]),
    ("storage room", ["storage rack", "storage rack", "cardboard box"]),
    ("stockroom",    ["storage rack", "storage rack", "cardboard box"]),
]


def _room_type_kit_for(room_type):
    """Returns the ordered entity-kind list for a room_type keyword (fuzzy
    substring match, first hit wins), or None if nothing matched."""
    needle = (room_type or "").strip().lower()
    for key, kit in _ROOM_TYPE_KITS:
        if key in needle or needle in key:
            return kit
    return None


# Fixed offsets (metres, same world X/Y convention _create_furniture/
# _apply_move already use) spreading a furnish kit's items out around the
# room's single wx/wy reference point instead of stacking every item on
# top of it — there's no stored room polygon/footprint to place items
# exactly inside the walls (see _handle_furnish_command), so this is a
# best-effort scatter, not a boundary-aware layout: the same positioning
# sophistication _create_light/_create_camera already use (a single
# point, no room-shape awareness anywhere in this file). First entry is
# near-anchor (e.g. a desk close to the room's own reference point); the
# rest ring around it.
_FURNISH_LAYOUT_OFFSETS = [
    (0.0, 0.0),
    (1.3, 1.1),
    (-1.3, 1.1),
    (0.0, 2.1),
    (1.9, -1.0),
    (-1.9, -1.0),
    (0.0, -2.1),
    (2.6, 0.6),
]


def _list_nvidia_assets(kind, limit=12):
    """Blocking — run via run_in_executor, same pattern as _call_agent_api.
    Maps `kind` to a category folder and lists it via S3's public
    ListObjectsV2 API (plain XML over HTTPS, no SDK/auth needed), returning
    up to `limit` items for the AI Agent panel to present as clickable
    suggestions — rather than silently auto-picking one, so "sofa" can
    resolve to a specific sofa the user actually chose.

    No delimiter is passed, so the listing recurses into subfolders too —
    confirmed against the real bucket that e.g. Sofas/ has only 2 models
    directly inside it (Arnold, Moline) but a dozen more live one level
    down in Sofas/Lemay/; without recursing, only those first 2 ever show
    up as suggestions.

    Each item's thumbnail is downloaded to a local temp cache and its path
    returned as "thumb" — every model in this bucket has a matching
    <folder>/.thumbs/256x256/<file>.png (confirmed by listing the bucket),
    and ui.Button's image_url kwarg is documented to take a plain path, so
    this avoids depending on unconfirmed remote-HTTPS image loading inside
    omni.ui."""
    import os as _os
    import tempfile as _tempfile
    import urllib.parse as _up
    import urllib.request as _ur
    import xml.etree.ElementTree as _ET
    import concurrent.futures as _cf

    prefix, footprint = _nvidia_category_for(kind)
    if not prefix:
        return {"error": f"no NVIDIA asset category mapped for \"{kind}\""}

    query = _up.urlencode({"list-type": "2", "prefix": prefix})
    try:
        with _ur.urlopen(f"{_NVIDIA_ASSET_ROOT}/?{query}", timeout=20) as resp:
            xml_bytes = resp.read()
    except Exception as e:
        return {"error": f"NVIDIA asset listing failed: {e}"}

    try:
        ns = {"s3": "http://s3.amazonaws.com/doc/2006-03-01/"}
        root = _ET.fromstring(xml_bytes)
        keys = [
            el.text for el in root.findall(".//s3:Contents/s3:Key", ns)
            if el.text and el.text.lower().endswith((".usd", ".usda", ".usdc"))
            and "/.thumbs/" not in el.text
            # The Isaac SimReady Industrial assets (unlike the older
            # ArchVis/Residential set) each carry several INTERNAL
            # composition-arc files alongside the real top-level asset —
            # payloads/{base,geometries,materials,instances,robot}.usd(a)
            # and a Physics/ subfolder — plus their own Materials-only
            # .usd. None of those are meant to be referenced directly
            # (confirmed by listing several categories: exactly one
            # top-level .usd sits next to them), so both are excluded
            # rather than showing 8-9 near-duplicate, mostly-invisible
            # "results" per real prop.
            and "/payloads/" not in el.text
            and "/materials/" not in el.text.lower()
        ]
    except Exception as e:
        return {"error": f"NVIDIA asset listing parse failed: {e}"}

    if not keys:
        return {"error": f"no .usd files found under {folder}"}

    cache_dir = _os.path.join(_tempfile.gettempdir(), "planlift_nvidia_thumbs")
    _os.makedirs(cache_dir, exist_ok=True)

    def _fetch_one(key):
        basename = key.rsplit("/", 1)[-1]
        dirpart = key.rsplit("/", 1)[0]
        name = basename.rsplit(".", 1)[0].replace("_", " ")
        # Some category folders have spaces in their names (e.g. "Table
        # Lamps"); quote() with safe="/" escapes those without mangling the
        # path separators, since a raw space in the URL isn't guaranteed to
        # survive Kit's own USD/HTTP asset resolver the way it survived
        # urllib here.
        encoded_key = _up.quote(key, safe="/")

        thumb_key = f"{dirpart}/.thumbs/256x256/{basename}.png"
        thumb_local = _os.path.join(cache_dir, thumb_key.replace("/", "_"))
        if not _os.path.exists(thumb_local):
            try:
                thumb_url = f"{_NVIDIA_ASSET_ROOT}/{_up.quote(thumb_key, safe='/')}"
                with _ur.urlopen(thumb_url, timeout=10) as thumb_resp:
                    thumb_bytes = thumb_resp.read()
                with open(thumb_local, "wb") as f:
                    f.write(thumb_bytes)
            except Exception:
                thumb_local = ""

        return {
            "name": name,
            "url": f"{_NVIDIA_ASSET_ROOT}/{encoded_key}",
            "thumb": thumb_local.replace("\\", "/") if thumb_local else "",
        }

    # Thumbnails fetched concurrently, not one network round-trip at a
    # time — with up to `limit` (12) separate HTTPS requests to a US-West-2
    # bucket, doing them serially was the single biggest chunk of wall-clock
    # time a furniture "add" prompt spent waiting, confirmed by this
    # function's own structure (a plain for-loop over blocking urlopen
    # calls). ThreadPoolExecutor.map preserves input order, so suggestion
    # ordering is unaffected. Already-cached thumbnails (the common case on
    # a repeat search) short-circuit per-item regardless.
    wanted_keys = keys[:limit]
    with _cf.ThreadPoolExecutor(max_workers=min(8, len(wanted_keys)) or 1) as pool:
        items = list(pool.map(_fetch_one, wanted_keys))
    return {"items": items, "footprint": footprint}


def set_agent_context(rooms, world_scale, img_w, img_h):
    """Called once per Isaac Sim launch, after furniture injection succeeds.
    Safe to call more than once (e.g. Kit hot-reload) — just refreshes state
    and creates the panel if it isn't already open."""
    global _agent_rooms, _agent_world_scale, _agent_img_w, _agent_img_h, _agent_panel
    _agent_rooms       = rooms or []
    _agent_world_scale = world_scale
    _agent_img_w       = img_w
    _agent_img_h       = img_h
    # Diagnostic: dump the actual per-room name/cx/cy the browser sent, so a
    # "every command lands in the same spot" report can be checked against
    # real data instead of guessed at — e.g. multiple rooms sharing one name,
    # or the same cx/cy, would show up directly here.
    for _r in _agent_rooms:
        carb.log_info(
            f"[PlanLift] AgentContext room: name={_r.get('name')!r} "
            f"floorKey={_r.get('floorKey')} wx={_r.get('wx')} wy={_r.get('wy')} wz={_r.get('wz')}"
        )
    if _agent_panel is None:
        _agent_panel = _AIAgentPanel()
        # Dock into Property's slot using the SAME mechanism floorplan.tool
        # already relies on successfully for its own "Floor Plan Tool"
        # window (floorplan/tool/extension.py, right after that window's
        # ui.Window(...) call):
        #   win.deferred_dock_in("Env Day Cycle Control",
        #                         ui.DockPolicy.CURRENT_WINDOW_IS_ACTIVE)
        # That method's own in-tree comment is "deferred so it works after
        # layout" — it resolves the target by NAME on a later frame, instead
        # of requiring an already-live Workspace.get_window() reference
        # right now the way plain dock_in() does. Two earlier attempts here
        # used plain dock_in() with an immediate reference — first to
        # Stage's slot, then to Property's — and a live test (screenshot
        # evidence) showed both left AI Agent floating, undocked. Switching
        # to the exact proven pattern instead of re-guessing a third
        # variant of the same approach.
        try:
            _agent_panel._win.deferred_dock_in(
                "Property", _agent_panel._ui.DockPolicy.CURRENT_WINDOW_IS_ACTIVE
            )
            carb.log_info("[PlanLift] AI Agent: deferred_dock_in('Property') requested")
        except Exception as e:
            carb.log_error(f"[PlanLift] AI Agent deferred_dock_in failed: {e}")

        # Hide Stage and Property a few frames later — not in this same
        # frame, which would race deferred_dock_in's own later-frame
        # resolution of "Property" by name (hiding/destroying it here would
        # risk pulling the target out from under that pending dock before
        # it runs). Stage has no dock role here; the user wants it gone
        # entirely, same as Property.
        async def _hide_stage_and_property():
            app = omni.kit.app.get_app()
            for _ in range(10):
                await app.next_update_async()
            import omni.ui as _ui_hide
            for _name in ("Stage", "Property", "Properties"):
                try:
                    _w = _ui_hide.Workspace.get_window(_name)
                    if _w and _w.visible:
                        _w.visible = False
                        carb.log_info(f"[PlanLift] Hidden: {_name}")
                except Exception as _e:
                    carb.log_error(f"[PlanLift] hide {_name}: {_e}")
        asyncio.ensure_future(_hide_stage_and_property())
    else:
        _agent_panel.set_status(f"Ready - {len(_agent_rooms)} room(s) labeled.")
        _agent_panel.set_rooms_line(_format_labeled_rooms_line())


def _resolve_room(name):
    """Fuzzy-match a room name against _agent_rooms: exact match first, then
    substring either direction, so "bedroom" matches a labeled
    "Master Bedroom" (mirrors the browser's Rooms-tab room resolution)."""
    if not name:
        return None
    needle = name.strip().lower()
    for r in _agent_rooms:
        if r.get("name", "").strip().lower() == needle:
            return r
    for r in _agent_rooms:
        label = r.get("name", "").strip().lower()
        if label and (label in needle or needle in label):
            return r
    return None


class _AIAgentPanel:
    """Type a request, NVIDIA Nemotron (via the app's own /api/agent proxy
    on localhost:3000 — the same endpoint the browser used to call, reused
    here instead of duplicating the prompt-building logic; that route
    itself calls Nebius Token Factory, not a local Ollama model anymore)
    turns it into {entity_kind, room}, resolved against _agent_rooms, and
    the result is created directly on the live stage. 'light' and 'camera'
    are procedural; anything else searches NVIDIA's public Omniverse asset
    library (see _list_nvidia_assets) and shows the results as clickable
    suggestions - the chosen one is scaled to a category-guessed footprint
    and placed."""

    WINDOW_NAME = "PlanLift AI Agent"

    def __init__(self):
        import omni.ui as ui
        self._ui = ui
        self._win = ui.Window(self.WINDOW_NAME, width=380, height=680)
        self._field  = None
        self._status = None
        self._rooms_line = None
        self._suggestions_frame = None
        self._placed_frame = None
        # Set while a furniture search's results are showing as clickable
        # suggestions, waiting on the user to pick one (or Cancel).
        self._pending_room = None
        # Set instead of _pending_room for an "add X in all rooms" prompt —
        # a list of every labeled room, so the chosen suggestion gets placed
        # once per room instead of once total. Entirely separate from
        # _pending_room so the normal single-room flow above is untouched.
        self._pending_rooms = None
        self._pending_footprint = None
        self._pending_kind = None  # the search kind (e.g. "sofa") — tracked alongside the placed item, for later "move" prompts
        # Set once per _handle_prompt call, right after classification
        # succeeds — read by every placement path below (light/camera/
        # furniture alike, immediate or several seconds later via a
        # suggestion click) so each item placed from this one request
        # gets tagged with the SAME prompt_id/prompt_text, regardless of
        # how long the user takes to pick a suggestion.
        self._pending_prompt_id = None
        self._pending_prompt_text = None
        self._build_ui()
        # Prevent the user from accidentally closing this panel (its own
        # close button, or a middle-click on its tab) — set_agent_context
        # only ever creates ONE _AIAgentPanel per Isaac Sim launch, so a
        # closed panel would otherwise stay gone until relaunch, with
        # every "Ready"/status update silently landing on a window
        # nobody can see. Same guard technique _apply_layout already uses
        # for "Env Day Cycle Control": re-show one frame later rather
        # than fighting the close event itself, which some Kit window
        # implementations ignore a same-frame visible=True during.
        def _agent_win_guard(visible, _w=self._win):
            if not visible:
                async def _r():
                    await omni.kit.app.get_app().next_update_async()
                    try:
                        _w.visible = True
                    except Exception:
                        pass
                asyncio.ensure_future(_r())
        self._win.set_visibility_changed_fn(_agent_win_guard)
        carb.log_info("[PlanLift] AI Agent panel created")

    def _build_ui(self):
        ui = self._ui
        with self._win.frame:
            with ui.VStack(spacing=6, style={"margin": 8}):
                ui.Label(
                    "Ask the AI agent to add something to a labeled room — "
                    "or say \"in the current view\" to place it where the "
                    "camera is looking right now:",
                    style={"font_size": 13, "color": 0xFFDDDDDD},
                    word_wrap=True,
                )
                ui.Label(
                    "Powered by NVIDIA Nemotron - Nebius Token Factory",
                    style={"font_size": 16, "color": 0xFF76B900},
                    word_wrap=True,
                )
                self._field = ui.StringField(height=24)
                self._field.model.set_value("add a light in the kitchen")
                ui.Button("Ask", height=26, clicked_fn=self._on_submit)
                self._status = ui.Label(
                    f"Ready - {len(_agent_rooms)} room(s) labeled.",
                    style={"font_size": 12, "color": 0xFF999999},
                    word_wrap=True,
                )
                self._rooms_line = ui.Label(
                    _format_labeled_rooms_line(),
                    style={"font_size": 11, "color": 0xFF777777},
                    word_wrap=True,
                )
                # Populated by _show_suggestions() when a furniture request
                # matches a category with multiple items — empty otherwise.
                # Kept small since it's usually empty — most of the
                # window's height goes to the persistent placed-items list
                # below, which almost always has real content.
                self._suggestions_frame = ui.ScrollingFrame(
                    height=120,
                    horizontal_scrollbar_policy=ui.ScrollBarPolicy.SCROLLBAR_ALWAYS_OFF,
                    vertical_scrollbar_policy=ui.ScrollBarPolicy.SCROLLBAR_AS_NEEDED,
                )
                ui.Label(
                    "Placed items - < and > swap models, Remove deletes it from the scene:",
                    style={"font_size": 11, "color": 0xFFAAAAAA},
                    word_wrap=True,
                )
                # Every item the AI agent has placed this session (via add,
                # furnish, or "all rooms"), persistent across prompts — NOT
                # cleared with the suggestions frame above, which is only
                # for the transient "pick one" step before something is
                # placed. Populated by _render_placed_items().
                self._placed_frame = ui.ScrollingFrame(
                    height=340,
                    horizontal_scrollbar_policy=ui.ScrollBarPolicy.SCROLLBAR_ALWAYS_OFF,
                    vertical_scrollbar_policy=ui.ScrollBarPolicy.SCROLLBAR_AS_NEEDED,
                )
        self._render_placed_items()

    def set_status(self, text):
        if self._status:
            self._status.text = text

    def set_rooms_line(self, text):
        if self._rooms_line:
            self._rooms_line.text = text

    def _on_submit(self):
        text = self._field.model.get_value_as_string().strip() if self._field else ""
        if not text:
            return
        # Release keyboard focus back to the viewport — otherwise WASD/arrow
        # keys keep typing into this field instead of flying the camera
        # after a prompt is submitted.
        try:
            self._field.focus_keyboard(False)
        except Exception:
            pass
        asyncio.ensure_future(self._handle_prompt(text))

    async def _handle_prompt(self, prompt_text):
        # "Move/rotate an existing item" are handled entirely locally,
        # checked BEFORE the LLM call — only intercept prompts that
        # unambiguously match (trigger verb + the numbers each needs);
        # anything else falls through to the normal flow below completely
        # untouched, so this can't conflict with how a new item's own
        # position gets computed.
        move_cmd = _parse_move_command(prompt_text)
        if move_cmd:
            self._handle_move_command(move_cmd)
            return

        rotate_cmd = _parse_rotate_command(prompt_text)
        if rotate_cmd:
            self._handle_rotate_command(rotate_cmd)
            return

        remove_cmd = _parse_remove_command(prompt_text)
        if remove_cmd:
            self._handle_remove_command(remove_cmd)
            return

        self.set_status("Thinking...")
        loop = asyncio.get_event_loop()
        try:
            intent = await loop.run_in_executor(None, self._call_agent_api, prompt_text)
        except Exception as e:
            self.set_status(f"Error reaching the agent API: {e}")
            return

        if "error" in intent:
            self.set_status(f"Error: {intent['error']}")
            return

        global _agent_prompt_counter
        _agent_prompt_counter += 1
        self._pending_prompt_id = _agent_prompt_counter
        self._pending_prompt_text = prompt_text

        carb.log_info(f"[PlanLift] AI agent: prompt={prompt_text!r} -> intent={intent!r}")

        # "add/furnish X in the current camera view" (or "in front of the
        # camera") — a general override on WHERE to place things, checked
        # against the ORIGINAL prompt text (not anything the LLM decided),
        # so it works no matter what room name the LLM guessed. Computed
        # once here and threaded through both the furnish and single-add
        # branches below — neither the all-rooms branch nor route.ts
        # itself needs to know this exists.
        #
        # REVERTED: an earlier version of this tried to let an "explicit"
        # labeled-room mention in the prompt win over this — but that
        # check (a plain substring match of every labeled room's name
        # against the whole prompt) false-positived constantly in
        # practice, because a room_type word used in a "furnish" prompt
        # routinely IS also a real room's label (e.g. a room literally
        # named "Reception" plus the prompt "furnish ... as a reception
        # in current camera view" — "reception" there names the KIT, not
        # a target location, but the substring check couldn't tell the
        # difference and always concluded a room was named, silently
        # disabling camera-view every time). The trigger phrase alone is
        # trusted again — simpler, and matches how this worked before
        # that regression.
        camera_view_room = None
        _cv_trigger_matched = bool(_CAMERA_VIEW_TRIGGER_RE.search(prompt_text))
        _camview_dbg(f"_handle_prompt: prompt={prompt_text!r} trigger_matched={_cv_trigger_matched}")
        if _cv_trigger_matched:
            camera_view_room = await loop.run_in_executor(None, _resolve_camera_view_target)
            if not camera_view_room:
                _camview_dbg("_handle_prompt: camera_view_room resolution FAILED -> aborting with error status")
                self.set_status("No active camera found in the viewport to place relative to.")
                return
            carb.log_info(f"[PlanLift] AI agent: camera-view target -> {camera_view_room!r}")

        # "Furnish the reception" / "set this up as an office" — /api/agent
        # signals this with action="furnish" and a room_type instead of one
        # entity_kind (see route.ts's system prompt). Entirely separate
        # from the "add" flow below (checked first, unconditionally
        # returns), so it can't interfere with the original single-object
        # or all-rooms paths.
        if (intent.get("action") or "").strip().lower() == "furnish":
            await self._handle_furnish_command(intent, override_room=camera_view_room)
            return

        # "Add X in all rooms" / "every room" — /api/agent signals this with
        # the literal room value "__ALL_ROOMS__" (see route.ts's system
        # prompt). Handled as an entirely separate branch, with its own
        # _pending_rooms suggestion/placement functions below, so the
        # original single-room path underneath is completely untouched.
        #
        # camera_view_room gates this: a "current view" prompt has no
        # labeled room to match, so route.ts's own classifier regularly
        # falls back to guessing "__ALL_ROOMS__" for it — without this
        # check that guess won the race before camera_view_room (set
        # above, from the ORIGINAL prompt text, not the LLM's guess) ever
        # got a chance, so "add the camera in current view" silently
        # fanned out to every room instead of the one the user was
        # actually looking at.
        room_raw = intent.get("room")
        all_rooms_mode = (not camera_view_room) and isinstance(room_raw, str) and room_raw.strip().strip('_').upper().replace(" ", "_") in (
            "ALL_ROOMS", "ALL", "EVERY_ROOM",
        )
        if all_rooms_mode:
            if not _agent_rooms:
                self.set_status(
                    "No rooms are labeled yet in this session. Label rooms in the "
                    "web app's Rooms tab first, then relaunch Isaac Sim so the AI "
                    "agent panel picks up the labels."
                )
                return
            rooms = list(_agent_rooms)
            carb.log_info(f"[PlanLift] AI agent: 'all rooms' mode -> {len(rooms)} room(s)")
            kind = (intent.get("entity_kind") or "").strip().lower()

            if kind == "light":
                placed = 0
                for r in rooms:
                    try:
                        light_path = self._create_light(r["wx"], r["wy"], r.get("wz", 0.0), r.get("wallHeightM", 0.0), room_name=r.get("name"))
                        if light_path:
                            _agent_placed_items.append({
                                "kind": "light", "room": r.get("name"), "prim_path": light_path,
                                "name": "Light", "prompt_id": self._pending_prompt_id, "prompt_text": self._pending_prompt_text,
                            })
                        placed += 1
                    except Exception as e:
                        carb.log_warn(f"[PlanLift] AI agent: light in {r.get('name')!r} failed: {e}")
                self._render_placed_items()
                self.set_status(f"Added a light to {placed}/{len(rooms)} room(s).")
                return

            if kind == "camera":
                self._show_camera_suggestions_all(rooms)
                return

            self.set_status(f"Searching NVIDIA assets for \"{kind}\"...")
            nv_result = await loop.run_in_executor(None, _list_nvidia_assets, kind)
            if "error" in nv_result:
                self.set_status(f"NVIDIA assets: {nv_result['error']}")
                return
            self._show_suggestions_all(rooms, kind, nv_result["items"], nv_result.get("footprint"))
            return

        # ── Everything below is the original single-room flow — the only
        # change is resolving to camera_view_room first when set above.
        room = camera_view_room or _resolve_room(intent.get("room"))
        _camview_dbg(f"_handle_prompt: single-room branch -> using camera_view_room={bool(camera_view_room)}, final room={room!r}")
        if not room:
            self.set_status(f"Couldn't find a room named \"{intent.get('room')}\" - label it first.")
            return

        carb.log_info(f"[PlanLift] AI agent: resolved room {intent.get('room')!r} -> {room!r}")

        kind = (intent.get("entity_kind") or "").strip().lower()

        if kind == "light":
            try:
                light_path = self._create_light(room["wx"], room["wy"], room.get("wz", 0.0), room.get("wallHeightM", 0.0), room_name=room.get("name"))
                if light_path:
                    _agent_placed_items.append({
                        "kind": "light", "room": room["name"], "prim_path": light_path,
                        "name": "Light", "prompt_id": self._pending_prompt_id, "prompt_text": self._pending_prompt_text,
                    })
                    self._render_placed_items()
                self.set_status(f"Added a light to \"{room['name']}\".")
            except Exception as e:
                self.set_status(f"Failed to create light: {e}")
            return

        if kind == "camera":
            self._show_camera_suggestions(room)
            return

        # Anything else: search NVIDIA's public Omniverse asset library and
        # show the results as clickable suggestions, rather than silently
        # auto-picking one — "sofa" can resolve to several sofas, and the
        # user should be the one to choose which one actually gets placed.
        # Neither the local manifest.json catalog nor Sketchfab are used
        # here anymore — furniture comes exclusively from NVIDIA's ArchVis
        # Residential asset set.
        self.set_status(f"Searching NVIDIA assets for \"{kind}\"...")
        nv_result = await loop.run_in_executor(None, _list_nvidia_assets, kind)
        if "error" in nv_result:
            self.set_status(f"NVIDIA assets: {nv_result['error']}")
            return

        self._show_suggestions(room, kind, nv_result["items"], nv_result.get("footprint"))

    async def _handle_furnish_command(self, intent, override_room=None):
        """"furnish the reception" style request — resolves room_type
        against _ROOM_TYPE_KITS (fuzzy substring, same technique
        _nvidia_category_for uses), then places every item in that kit's
        list around the room's single wx/wy reference point using
        _FURNISH_LAYOUT_OFFSETS. Auto-picks the first NVIDIA search result
        per item rather than showing suggestions — the user chose "place
        the whole set at once" over "confirm each item" for this flow, and
        stepping through 5-6 separate suggestion pickers would defeat
        that. Each placed item is still tracked in _agent_placed_items
        exactly like the normal "add" flow, so a later "move the chair in
        the reception" finds it.

        override_room, when given (from a "...in the current camera
        view" prompt — see _resolve_camera_view_target), is used AS-IS
        instead of resolving intent["room"] against the labeled rooms —
        this is how a furnish request gets positioned at the active
        camera instead of a labeled room."""
        room = override_room or _resolve_room(intent.get("room"))
        _camview_dbg(f"_handle_furnish_command: using override_room={bool(override_room)}, final room={room!r}")
        if not room:
            self.set_status(f"Couldn't find a room named \"{intent.get('room')}\" - label it first.")
            return

        room_type = (intent.get("room_type") or "").strip().lower()
        kit = _room_type_kit_for(room_type)
        if not kit:
            known = ", ".join(sorted({key for key, _ in _ROOM_TYPE_KITS}))
            self.set_status(
                f"No starter kit for \"{room_type}\" yet (known: {known}) - "
                f"try naming individual items instead (e.g. \"add a desk in {room['name']}\").")
            return

        carb.log_info(f"[PlanLift] AI agent: furnish {room['name']!r} as {room_type!r} -> kit {kit!r}")
        self.set_status(f"Furnishing \"{room['name']}\" as a {room_type} ({len(kit)} item(s))...")

        loop = asyncio.get_event_loop()
        # Search every kit item's NVIDIA asset category CONCURRENTLY —
        # these are independent S3 listings (different categories), and
        # awaiting them one at a time in a for-loop made a furnish
        # request's total wait scale with kit size: a 6-item reception
        # kit paid 6 sequential network round-trips before anything got
        # placed. Gathering them cuts total wait to roughly the slowest
        # single search instead of their sum — the single biggest source
        # of "furnish/group prompts take a long time" latency.
        search_results = await asyncio.gather(*[
            loop.run_in_executor(None, _list_nvidia_assets, kind) for kind in kit
        ])

        placed, failed = 0, []
        for i, (kind, nv_result) in enumerate(zip(kit, search_results)):
            dx, dy = _FURNISH_LAYOUT_OFFSETS[i % len(_FURNISH_LAYOUT_OFFSETS)]
            if "error" in nv_result or not nv_result.get("items"):
                failed.append(kind)
                carb.log_warn(f"[PlanLift] AI agent: furnish — no NVIDIA asset for \"{kind}\": {nv_result.get('error')}")
                continue
            chosen = nv_result["items"][0]
            item = {"name": chosen["name"], "model": chosen["url"], "footprint": nv_result.get("footprint")}
            try:
                base_path = await self._create_furniture(room["wx"] + dx, room["wy"] + dy, item, room.get("wz", 0.0))
                if base_path:
                    category_folder, _fp = _nvidia_category_for(kind)
                    # Furnish already fetched the full candidate list for
                    # this kind — cache it now so the first Forward/Back
                    # click on this item doesn't need a fresh fetch.
                    if category_folder:
                        _category_alt_cache[category_folder] = nv_result["items"]
                    _agent_placed_items.append({
                        "kind": kind, "category": category_folder,
                        "room": room["name"], "prim_path": base_path,
                        "name": item["name"], "alt_index": 0,
                        "thumb": chosen.get("thumb"),
                        "prompt_id": self._pending_prompt_id, "prompt_text": self._pending_prompt_text,
                    })
                    placed += 1
                else:
                    failed.append(kind)
            except Exception as e:
                failed.append(kind)
                carb.log_warn(f"[PlanLift] AI agent: furnish — placing \"{kind}\" failed: {e}")

        self._render_placed_items()
        if failed:
            self.set_status(f"Furnished \"{room['name']}\" with {placed}/{len(kit)} item(s) - couldn't place: {', '.join(failed)}.")
        else:
            self.set_status(f"Furnished \"{room['name']}\" with {placed} item(s) for a {room_type}.")

    def _handle_move_command(self, move_cmd):
        """Resolves a parsed move command (see _parse_move_command) to a
        previously-placed item and repositions it — entirely separate from
        _create_furniture, so the add flow's own positioning is never
        touched by this."""
        kind = move_cmd["kind"]
        room_name = move_cmd["room_name"]
        room_err = self._check_room_label(room_name)
        if room_err:
            self.set_status(room_err)
            return
        item, reason = _resolve_placed_item(kind, room_name, move_cmd.get("ordinal"))
        if not item:
            self.set_status(reason or "Couldn't find that item.")
            return
        try:
            new_t = self._apply_move(item["prim_path"], move_cmd["direction"], move_cmd["distance_m"])
            default_note = " (default - say e.g. \"2 feet\" to be specific)" if move_cmd["distance_defaulted"] else ""
            self.set_status(
                f"Shifted \"{item['kind']}\" in \"{item['room']}\" {move_cmd['direction']} "
                f"{move_cmd['distance_m']:.2f}m{default_note} - now at ({new_t[0]:.2f}, {new_t[1]:.2f}).")
        except Exception as e:
            self.set_status(f"Failed to move {item['kind']}: {e}")

    @staticmethod
    def _check_room_label(room_name):
        """Returns a log-able error string if `room_name` (from a parsed
        move/rotate command) couldn't be resolved to a labeled room, else
        None. Distinguishes "no rooms labeled at all this session" (fix:
        label rooms in the web app, then relaunch Isaac Sim so the AI
        agent panel picks up the labels — rooms_data is only sent at
        launch time) from "rooms ARE labeled, just didn't name one"."""
        if room_name:
            return None
        if not _agent_rooms:
            return (
                "No rooms are labeled yet in this session. Label rooms in the "
                "web app's Rooms tab first, then relaunch Isaac Sim so the AI "
                "agent panel picks up the labels."
            )
        return (
            "Couldn't tell which labeled room you mean - mention one of: "
            + ", ".join(r.get("name", "?") for r in _agent_rooms) + "."
        )

    def _apply_move(self, prim_path, direction, distance_m):
        """Offsets an already-placed prim's translate by `distance_m`
        along the given compass-style direction, in the same world X/Y
        convention _create_furniture/_create_light/_create_camera already
        use (left/west = -X, right/east = +X, forward/north = +Y,
        back/backward/south = -Y). Reads the CURRENT translate and adds to
        it, rather than recomputing from scratch, so it composes correctly
        with wherever the item actually is now (including any earlier
        moves)."""
        import omni.usd
        from pxr import Usd, UsdGeom, Gf
        stage = omni.usd.get_context().get_stage()
        if not stage:
            raise RuntimeError("no stage open")
        prim = stage.GetPrimAtPath(prim_path)
        if not prim.IsValid():
            raise RuntimeError(f"item no longer exists at {prim_path}")

        # distance_m is real METERS (see _parse_move_command); the prim's
        # existing translate (read below) is in this stage's own units —
        # see _STAGE_UNITS_PER_METER's docstring for why those differ.
        distance_units = distance_m * _STAGE_UNITS_PER_METER
        dx = dy = 0.0
        if direction in ("left", "west"):
            dx = -distance_units
        elif direction in ("right", "east"):
            dx = distance_units
        elif direction in ("forward", "north"):
            dy = distance_units
        elif direction in ("back", "backward", "south"):
            dy = -distance_units

        xf = UsdGeom.XformCommonAPI(prim)
        vecs = xf.GetXformVectors(Usd.TimeCode.Default())
        t = list(vecs[0])
        t[0] += dx
        t[1] += dy
        xf.SetTranslate(Gf.Vec3d(t[0], t[1], t[2]))
        carb.log_info(f"[PlanLift] AI agent: moved {prim_path} by ({dx:.2f},{dy:.2f}) -> ({t[0]:.2f},{t[1]:.2f},{t[2]:.2f})")
        return t

    def _handle_remove_command(self, remove_cmd):
        """Resolves a parsed remove command (see _parse_remove_command) to
        a previously-placed item, then deletes it via _delete_placed_item —
        shared with the per-card Remove button in _render_placed_items,
        which already knows exactly which item was clicked and skips this
        text-resolution step entirely."""
        kind = remove_cmd["kind"]
        room_name = remove_cmd["room_name"]
        room_err = self._check_room_label(room_name)
        if room_err:
            self.set_status(room_err)
            return
        item, reason = _resolve_placed_item(kind, room_name, remove_cmd.get("ordinal"))
        if not item:
            self.set_status(reason or "Couldn't find that item.")
            return
        self._delete_placed_item(item)

    def _delete_placed_item(self, item):
        """Deletes item's prim and drops it from _agent_placed_items,
        refreshing the persistent placed-items list — same DeletePrims
        command Floor Plan Tool's own camera removal uses, and entirely
        separate from _apply_move/_apply_rotate (neither of which ever
        touches _agent_placed_items' membership), so this can't affect how
        a move/rotate command resolves an item that's still there."""
        try:
            import omni.usd
            import omni.kit.commands
            stage = omni.usd.get_context().get_stage()
            prim_path = item["prim_path"]
            if stage and stage.GetPrimAtPath(prim_path):
                omni.kit.commands.execute("DeletePrims", paths=[prim_path])
            if item in _agent_placed_items:
                _agent_placed_items.remove(item)
            self._render_placed_items()
            self.set_status(f"Removed \"{item['kind']}\" from \"{item['room']}\".")
            carb.log_info(f"[PlanLift] AI agent: removed {prim_path} ({item['kind']!r} in {item['room']!r})")
        except Exception as e:
            self.set_status(f"Failed to remove {item['kind']}: {e}")

    def _delete_placed_group(self, prompt_id):
        """"Remove All" button for a multi-item card (see _render_placed_
        items) — deletes every _agent_placed_items entry that shares this
        prompt_id (everything one "furnish ..." or "add X in all rooms"
        request placed), one DeletePrims call per prim so a single failed
        prim can't block the rest. A snapshot of the list is taken first
        since _delete_placed_item mutates _agent_placed_items as it goes
        (iterating the live list while removing from it would skip
        entries)."""
        items = [it for it in _agent_placed_items if it.get("prompt_id") == prompt_id]
        if not items:
            return
        removed = 0
        for it in items:
            try:
                import omni.usd
                import omni.kit.commands
                stage = omni.usd.get_context().get_stage()
                prim_path = it["prim_path"]
                if stage and stage.GetPrimAtPath(prim_path):
                    omni.kit.commands.execute("DeletePrims", paths=[prim_path])
                if it in _agent_placed_items:
                    _agent_placed_items.remove(it)
                removed += 1
            except Exception as e:
                carb.log_warn(f"[PlanLift] AI agent: failed to remove {it.get('kind')!r}: {e}")
        self._render_placed_items()
        self.set_status(f"Removed {removed}/{len(items)} item(s) from that request.")
        carb.log_info(f"[PlanLift] AI agent: removed {removed}/{len(items)} item(s) for prompt_id={prompt_id}")

    def _render_placed_items(self):
        """Rebuilds the persistent placed-items list — one card per
        PROMPT (not per item): _agent_placed_items is grouped by
        prompt_id, so a plain "add a chair" (one item) gets its own card
        showing that prompt's text plus thumbnail/name/room and "< Prev"/
        "Next >" to cycle it through that category's other available
        models, while a "furnish ..." or "add X in all rooms" request
        (several items from the ONE prompt) gets a single card listing
        them all with one "Remove All" button — removing the whole batch
        a furnish/all-rooms request placed is one click, not one per
        item. Called after every place/remove so the list always matches
        the live scene; safe to call before _build_ui has run (e.g.
        never, but defensive) since _placed_frame is checked.

        Button labels are plain ASCII text on purpose, not < / >
        arrow-glyph Unicode characters (◀ ▶) — confirmed live that
        Kit's UI font renders those (and ✓/— elsewhere in this panel)
        as a literal "?" box, making the buttons unreadable. Every
        set_status()/ui.Label() string in this file uses the same
        plain-ASCII substitutions (" - " for an em dash, no checkmark)
        for the same reason."""
        if not self._placed_frame:
            return
        ui = self._ui
        self._placed_frame.clear()
        with self._placed_frame:
            with ui.VStack(spacing=8):
                if not _agent_placed_items:
                    ui.Label(
                        "Nothing placed yet.",
                        style={"font_size": 11, "color": 0xFF777777},
                    )
                # Group in first-seen order (oldest prompt first) — matches
                # _agent_placed_items' own append order, so new cards
                # appear at the bottom, same as before this was grouped.
                groups = {}
                order = []
                for it in _agent_placed_items:
                    pid = it.get("prompt_id")
                    if pid not in groups:
                        groups[pid] = []
                        order.append(pid)
                    groups[pid].append(it)

                for pid in order:
                    items = groups[pid]
                    prompt_text = items[0].get("prompt_text") or items[0].get("name") or items[0]["kind"]

                    if len(items) == 1:
                        it = items[0]
                        label = it.get("name") or it["kind"]
                        with ui.VStack(height=104, spacing=4, style={"background_color": 0xFF2A2A2A, "border_radius": 4, "margin": 2}):
                            ui.Label(
                                f"\"{prompt_text}\"", style={"font_size": 10, "color": 0xFF888888},
                                word_wrap=True,
                            )
                            with ui.HStack(height=52, spacing=8):
                                ui.Image(
                                    it.get("thumb") or "", width=48, height=48,
                                    style={"background_color": 0xFF1A1A1A},
                                )
                                with ui.VStack(spacing=2):
                                    ui.Label(
                                        label, style={"font_size": 12, "color": 0xFFDDDDDD},
                                        word_wrap=False,
                                    )
                                    ui.Label(
                                        it["room"], style={"font_size": 10, "color": 0xFF888888},
                                        word_wrap=False,
                                    )
                            with ui.HStack(height=24, spacing=6):
                                if it.get("category"):
                                    # Swapping to an "alternative" only makes sense for
                                    # NVIDIA-asset furniture (which has a category of
                                    # candidates) — lights/cameras have no such list.
                                    ui.Button(
                                        "< Prev", height=24,
                                        clicked_fn=lambda it=it: asyncio.ensure_future(self._cycle_placed_item(it, -1)))
                                    ui.Button(
                                        "Next >", height=24,
                                        clicked_fn=lambda it=it: asyncio.ensure_future(self._cycle_placed_item(it, 1)))
                                ui.Button(
                                    "Remove", height=24, style={"color": 0xFFDD5555},
                                    clicked_fn=lambda it=it: self._delete_placed_item(it))
                    else:
                        with ui.VStack(height=84, spacing=4, style={"background_color": 0xFF2A2A2A, "border_radius": 4, "margin": 2}):
                            ui.Label(
                                f"\"{prompt_text}\"", style={"font_size": 10, "color": 0xFF888888},
                                word_wrap=True,
                            )
                            names = ", ".join((it.get("name") or it["kind"]) for it in items[:6])
                            ui.Label(
                                f"{len(items)} item(s): {names}", style={"font_size": 11, "color": 0xFFDDDDDD},
                                word_wrap=True,
                            )
                            ui.Button(
                                "Remove All", height=24, style={"color": 0xFFDD5555},
                                clicked_fn=lambda pid=pid: self._delete_placed_group(pid))

    async def _cycle_placed_item(self, item, direction):
        """Forward (+1) / Back (-1) button handler — swaps `item`'s
        referenced model for the next/previous one in its category's
        candidate list (wrapping around at either end), in place, keeping
        its position untouched. The candidate list is whatever
        _get_category_alternatives returns: the exact list already shown
        as suggestions if this category has been searched before in this
        session, otherwise a fresh fetch."""
        if item not in _agent_placed_items:
            self.set_status("That item no longer exists.")
            return
        self.set_status(f"Finding alternatives for \"{item['kind']}\"...")
        loop = asyncio.get_event_loop()
        alts = await loop.run_in_executor(None, _get_category_alternatives, item["kind"], item["category"])
        if not alts:
            self.set_status(f"No alternatives found for \"{item['kind']}\".")
            return
        idx = (item.get("alt_index", 0) + direction) % len(alts)
        chosen = alts[idx]
        self.set_status(f"Swapping to \"{chosen['name']}\"...")
        ok = await self._swap_furniture_model(item["prim_path"], item["kind"], chosen["url"])
        if not ok:
            self.set_status(f"Failed to swap \"{item['kind']}\" - item may no longer exist in the scene.")
            return
        item["alt_index"] = idx
        item["name"] = chosen["name"]
        item["thumb"] = chosen.get("thumb")
        self._render_placed_items()
        self.set_status(f"\"{item['room']}\" - now showing \"{chosen['name']}\".")

    async def _swap_furniture_model(self, prim_path, kind, new_url):
        """Swaps the referenced asset at an already-placed furniture
        item's Scaled/Ref prim for a different URL, keeping its translate
        (position) completely untouched. Mirrors _create_furniture's own
        reference-add -> wait-for-load -> measure -> scale sequence (kept
        as a separate method rather than folded into a shared helper,
        given how much hard-won bug history is baked into that function's
        comments about xformOp ordering — safer not to risk touching it).
        Returns True on success, False if the item no longer exists."""
        import omni.usd
        from pxr import Usd, UsdGeom, Gf
        stage = omni.usd.get_context().get_stage()
        if not stage:
            return False
        xform_prim = stage.GetPrimAtPath(prim_path)
        scaler_prim = stage.GetPrimAtPath(prim_path + "/Scaled")
        ref_prim = stage.GetPrimAtPath(prim_path + "/Scaled/Ref")
        if not (xform_prim.IsValid() and scaler_prim.IsValid() and ref_prim.IsValid()):
            return False

        # Reuse the EXISTING scale op (added once by _create_furniture) —
        # calling AddScaleOp() again on a prim that already has one raises
        # ("xformOp:scale already exists in xformOpOrder"), the exact
        # error _create_furniture's own hierarchy comment describes
        # hitting before settling on this one-op-type-per-prim layout.
        scaler_xformable = UsdGeom.Xformable(scaler_prim)
        scale_op = None
        for op in scaler_xformable.GetOrderedXformOps():
            if op.GetOpType() == UsdGeom.XformOp.TypeScale:
                scale_op = op
                break
        if scale_op is None:
            scale_op = scaler_xformable.AddScaleOp()
        # Reset to identity BEFORE measuring — otherwise the new asset's
        # native bbox would be read back already distorted by whatever
        # scale the OLD asset needed, contaminating the new measurement.
        scale_op.Set(Gf.Vec3f(1.0, 1.0, 1.0))

        ref_prim.GetReferences().ClearReferences()
        ref_prim.GetReferences().AddReference(new_url)

        app = omni.kit.app.get_app()
        ctx = omni.usd.get_context()
        for _ in range(300):   # ~5s+ ceiling, same as _create_furniture
            await app.next_update_async()
            try:
                if not ctx.is_stage_loading():
                    break
            except AttributeError:
                break

        _, footprint = _nvidia_category_for(kind)
        fw = footprint.get("widthM") if footprint else None
        fd = footprint.get("depthM") if footprint else None
        fh = footprint.get("heightM") if footprint else None
        scale = Gf.Vec3f(1.0, 1.0, 1.0)
        bbox_cache = UsdGeom.BBoxCache(
            Usd.TimeCode.Default(), [UsdGeom.Tokens.default_, UsdGeom.Tokens.render], useExtentsHint=False)
        rng = bbox_cache.ComputeWorldBound(xform_prim).ComputeAlignedRange()
        if not rng.IsEmpty():
            mn, mx = rng.GetMin(), rng.GetMax()
            dx = abs(mx[0] - mn[0]) or 1.0
            dy = abs(mx[1] - mn[1]) or 1.0
            dz = abs(mx[2] - mn[2]) or 1.0
            if fw and fd and fh:
                # Same _STAGE_UNITS_PER_METER scaling _create_furniture's
                # own (otherwise-identical) scale computation needs — see
                # its comment for why fw/fd/fh (real metres) can't be
                # divided directly against dx/dy/dz (this stage's units).
                scale = Gf.Vec3f(fw * _STAGE_UNITS_PER_METER / dx,
                                  fd * _STAGE_UNITS_PER_METER / dy,
                                  fh * _STAGE_UNITS_PER_METER / dz)
        scale_op.Set(scale)
        carb.log_info(f"[PlanLift] AI agent: swapped {prim_path} -> {new_url} scale={tuple(scale)}")
        return True

    def _handle_rotate_command(self, rotate_cmd):
        """Resolves a parsed rotate command (see _parse_rotate_command) to
        a previously-placed item and spins it around Z — entirely separate
        from _create_furniture and from _apply_move (which only ever
        touches translate), so neither the add flow nor position-shifting
        is affected by this."""
        kind = rotate_cmd["kind"]
        room_name = rotate_cmd["room_name"]
        room_err = self._check_room_label(room_name)
        if room_err:
            self.set_status(room_err)
            return
        item, reason = _resolve_placed_item(kind, room_name, rotate_cmd.get("ordinal"))
        if not item:
            self.set_status(reason or "Couldn't find that item.")
            return
        try:
            new_z = self._apply_rotate(item["prim_path"], rotate_cmd["degrees"])
            default_note = " (default quarter-turn - say e.g. \"45 degrees\" to be specific)" if rotate_cmd["degrees_defaulted"] else ""
            self.set_status(
                f"Rotated \"{item['kind']}\" in \"{item['room']}\" by {rotate_cmd['degrees']:.1f} deg{default_note} "
                f"- now facing {new_z:.1f} deg.")
        except Exception as e:
            self.set_status(f"Failed to rotate {item['kind']}: {e}")

    def _apply_rotate(self, prim_path, delta_degrees):
        """Adds `delta_degrees` to an already-placed prim's Z rotation
        only — X and Y stay untouched, per explicit "Z axis only" request
        (the only axis that makes sense for furniture sitting on a floor).
        Reads the CURRENT rotation via the same XformCommonAPI
        GetXformVectors()/Set*() pattern _apply_move already uses (and is
        confirmed working) — translate (vecs[0]) is read back and written
        unchanged, so this can't drift the item's position."""
        import omni.usd
        from pxr import Usd, UsdGeom, Gf
        stage = omni.usd.get_context().get_stage()
        if not stage:
            raise RuntimeError("no stage open")
        prim = stage.GetPrimAtPath(prim_path)
        if not prim.IsValid():
            raise RuntimeError(f"item no longer exists at {prim_path}")

        xf = UsdGeom.XformCommonAPI(prim)
        vecs = xf.GetXformVectors(Usd.TimeCode.Default())
        t = vecs[0]
        r = list(vecs[1])
        r[2] = (r[2] + delta_degrees) % 360.0
        xf.SetTranslate(t)
        xf.SetRotate(Gf.Vec3f(r[0], r[1], r[2]), UsdGeom.XformCommonAPI.RotationOrderXYZ)
        carb.log_info(f"[PlanLift] AI agent: rotated {prim_path} by {delta_degrees:.1f} -> z={r[2]:.1f}")
        return r[2]

    def _show_suggestions(self, room, kind, items, footprint):
        """Populates the suggestions frame with one button per candidate
        item; clicking one places it in `room` and clears the list."""
        ui = self._ui
        self._pending_room = room
        self._pending_footprint = footprint
        self._pending_kind = kind
        # Same candidate list the Forward/Back buttons will later cycle
        # through for whichever one gets placed — cache it now so the
        # first click of either button doesn't need a fresh network
        # fetch (see _get_category_alternatives).
        category_folder, _fp = _nvidia_category_for(kind) if kind else (None, None)
        if category_folder:
            _category_alt_cache[category_folder] = items
        self.set_status(
            f"Found {len(items)} option(s) for \"{kind}\" - click one to place it in \"{room['name']}\":")
        self._suggestions_frame.clear()
        with self._suggestions_frame:
            with ui.VStack(spacing=4):
                for it in items:
                    ui.Button(
                        it["name"], height=56,
                        image_url=it.get("thumb") or "",
                        image_width=48, image_height=48,
                        clicked_fn=lambda it=it: asyncio.ensure_future(self._place_suggestion(it)))
                ui.Button(
                    "Cancel", height=24, style={"color": 0xFF999999},
                    clicked_fn=self._clear_suggestions)

    def _clear_suggestions(self):
        self._suggestions_frame.clear()
        self._pending_room = None
        self._pending_rooms = None
        self._pending_footprint = None
        self._pending_kind = None
        self.set_status(f"Ready - {len(_agent_rooms)} room(s) labeled.")

    async def _place_suggestion(self, chosen):
        room = self._pending_room
        footprint = self._pending_footprint
        kind = self._pending_kind
        self._suggestions_frame.clear()
        if not room:
            self.set_status("That room selection expired - ask again.")
            return
        item = {
            "name": chosen["name"],
            "model": chosen["url"],   # already USD — referenced as-is, no conversion
            "footprint": footprint,
        }
        self.set_status(f"Placing \"{item['name']}\"...")
        try:
            base_path = await self._create_furniture(room["wx"], room["wy"], item, room.get("wz", 0.0))
            if kind and base_path:
                # Track by category FOLDER, not the literal search word — so a
                # later "shift the couch" finds something added as "sofa"
                # (both map to Furniture/Sofas via _nvidia_category_for, the
                # same normalization the add flow already applies).
                category_folder, _fp = _nvidia_category_for(kind)
                alts = _category_alt_cache.get(category_folder, [])
                alt_index = next((i for i, a in enumerate(alts) if a.get("url") == chosen.get("url")), 0)
                _agent_placed_items.append({
                    "kind": kind, "category": category_folder,
                    "room": room["name"], "prim_path": base_path,
                    "name": item["name"], "alt_index": alt_index,
                    "thumb": chosen.get("thumb"),
                    "prompt_id": self._pending_prompt_id, "prompt_text": self._pending_prompt_text,
                })
                self._render_placed_items()
            self.set_status(f"Added \"{item['name']}\" to \"{room['name']}\".")
        except Exception as e:
            self.set_status(f"Failed to place {item['name']}: {e}")
        finally:
            self._pending_room = None
            self._pending_footprint = None
            self._pending_kind = None

    def _show_suggestions_all(self, rooms, kind, items, footprint):
        """Same as _show_suggestions, but for "add X in all rooms" — uses
        _pending_rooms (a list) instead of _pending_room, so picking one
        suggestion places that SAME item once in every room, not once
        total. _show_suggestions itself is untouched."""
        ui = self._ui
        self._pending_rooms = rooms
        self._pending_footprint = footprint
        self._pending_kind = kind
        category_folder, _fp = _nvidia_category_for(kind) if kind else (None, None)
        if category_folder:
            _category_alt_cache[category_folder] = items
        self.set_status(
            f"Found {len(items)} option(s) for \"{kind}\" - click one to place it in all {len(rooms)} labeled room(s):")
        self._suggestions_frame.clear()
        with self._suggestions_frame:
            with ui.VStack(spacing=4):
                for it in items:
                    ui.Button(
                        it["name"], height=56,
                        image_url=it.get("thumb") or "",
                        image_width=48, image_height=48,
                        clicked_fn=lambda it=it: asyncio.ensure_future(self._place_suggestion_all(it)))
                ui.Button(
                    "Cancel", height=24, style={"color": 0xFF999999},
                    clicked_fn=self._clear_suggestions)

    async def _place_suggestion_all(self, chosen):
        rooms = self._pending_rooms
        footprint = self._pending_footprint
        kind = self._pending_kind
        self._suggestions_frame.clear()
        if not rooms:
            self.set_status("That room selection expired - ask again.")
            return
        item = {
            "name": chosen["name"],
            "model": chosen["url"],
            "footprint": footprint,
        }
        self.set_status(f"Placing \"{item['name']}\" in {len(rooms)} room(s)...")
        category_folder, _fp = _nvidia_category_for(kind) if kind else (None, None)
        alts = _category_alt_cache.get(category_folder, [])
        alt_index = next((i for i, a in enumerate(alts) if a.get("url") == chosen.get("url")), 0)
        placed = 0
        for room in rooms:
            try:
                base_path = await self._create_furniture(room["wx"], room["wy"], item, room.get("wz", 0.0))
                if kind and base_path:
                    _agent_placed_items.append({
                        "kind": kind, "category": category_folder,
                        "room": room["name"], "prim_path": base_path,
                        "name": item["name"], "alt_index": alt_index,
                        "thumb": chosen.get("thumb"),
                        "prompt_id": self._pending_prompt_id, "prompt_text": self._pending_prompt_text,
                    })
                placed += 1
            except Exception as e:
                carb.log_warn(f"[PlanLift] AI agent: \"{item['name']}\" in {room.get('name')!r} failed: {e}")
        self._render_placed_items()
        self.set_status(f"Added \"{item['name']}\" to {placed}/{len(rooms)} room(s).")
        self._pending_rooms = None
        self._pending_footprint = None
        self._pending_kind = None

    def _show_camera_suggestions(self, room):
        """Shows one button per available camera resolution preset
        (planlift_floorview's _CAM_PRESETS — the exact 2MP/4MP/8MP specs
        that extension already defines, each with its own real resolution,
        focal length, and sensor aperture) instead of silently guessing
        one from the prompt text via _pick_camera_preset_key. Same
        reasoning as the furniture suggestions above: let the user pick
        exactly which camera gets placed, so what's placed always has the
        real properties for that spec, not a default/guessed one."""
        ui = self._ui
        self._pending_room = room
        self._pending_footprint = None
        self.set_status(f"Choose a camera for \"{room['name']}\":")
        self._suggestions_frame.clear()
        try:
            from planlift_floorview.extension import _CAM_PRESETS
        except Exception as e:
            self.set_status(f"Camera presets unavailable: {e}")
            return
        with self._suggestions_frame:
            with ui.VStack(spacing=4):
                for key, preset in _CAM_PRESETS.items():
                    row_label = f"{preset['label']} - {preset['res_w']}x{preset['res_h']}"
                    ui.Button(
                        row_label, height=32,
                        clicked_fn=lambda k=key: asyncio.ensure_future(self._place_camera_suggestion(k)))
                ui.Button(
                    "Cancel", height=24, style={"color": 0xFF999999},
                    clicked_fn=self._clear_suggestions)

    async def _place_camera_suggestion(self, preset_key):
        room = self._pending_room
        self._suggestions_frame.clear()
        if not room:
            self.set_status("That room selection expired - ask again.")
            return
        try:
            if room.get("name") == "the current camera view":
                # camera_view_room's sentinel name (see _resolve_camera_view_target) —
                # matches the viewport camera's own pose instead of mounting at a
                # generic floor-relative point.
                cam_label, cam_path = self._create_camera_from_view(preset_key=preset_key)
                self.set_status(f"Added a {cam_label} camera matching the current view - see the Camera List.")
            else:
                cam_label, cam_path = self._create_camera(room["wx"], room["wy"], room.get("wz", 0.0), room.get("wallHeightM", 0.0), preset_key=preset_key, room_name=room.get("name"))
                self.set_status(f"Added a {cam_label} camera to \"{room['name']}\" - see the Camera List.")
            if cam_path:
                _agent_placed_items.append({
                    "kind": "camera", "room": room["name"], "prim_path": cam_path,
                    "name": f"{cam_label} camera", "prompt_id": self._pending_prompt_id, "prompt_text": self._pending_prompt_text,
                })
                self._render_placed_items()
        except Exception as e:
            self.set_status(f"Failed to create camera: {e}")
        finally:
            self._pending_room = None

    def _show_camera_suggestions_all(self, rooms):
        """Same as _show_camera_suggestions, but for "add a camera in all
        rooms" — uses _pending_rooms (a list) so the SAME chosen preset gets
        placed once in every room. _show_camera_suggestions itself is
        untouched."""
        ui = self._ui
        self._pending_rooms = rooms
        self._pending_footprint = None
        self.set_status(f"Choose a camera for all {len(rooms)} labeled room(s):")
        self._suggestions_frame.clear()
        try:
            from planlift_floorview.extension import _CAM_PRESETS
        except Exception as e:
            self.set_status(f"Camera presets unavailable: {e}")
            return
        with self._suggestions_frame:
            with ui.VStack(spacing=4):
                for key, preset in _CAM_PRESETS.items():
                    row_label = f"{preset['label']} - {preset['res_w']}x{preset['res_h']}"
                    ui.Button(
                        row_label, height=32,
                        clicked_fn=lambda k=key: asyncio.ensure_future(self._place_camera_suggestion_all(k)))
                ui.Button(
                    "Cancel", height=24, style={"color": 0xFF999999},
                    clicked_fn=self._clear_suggestions)

    async def _place_camera_suggestion_all(self, preset_key):
        rooms = self._pending_rooms
        self._suggestions_frame.clear()
        if not rooms:
            self.set_status("That room selection expired - ask again.")
            return
        cam_label = preset_key
        placed = 0
        for room in rooms:
            try:
                cam_label, cam_path = self._create_camera(room["wx"], room["wy"], room.get("wz", 0.0), room.get("wallHeightM", 0.0), preset_key=preset_key, room_name=room.get("name"))
                if cam_path:
                    _agent_placed_items.append({
                        "kind": "camera", "room": room.get("name"), "prim_path": cam_path,
                        "name": f"{cam_label} camera", "prompt_id": self._pending_prompt_id, "prompt_text": self._pending_prompt_text,
                    })
                placed += 1
            except Exception as e:
                carb.log_warn(f"[PlanLift] AI agent: camera in {room.get('name')!r} failed: {e}")
        self._render_placed_items()
        self.set_status(f"Added a {cam_label} camera to {placed}/{len(rooms)} room(s) - see the Camera List.")
        self._pending_rooms = None

    @staticmethod
    def _call_agent_api(prompt_text):
        """Runs off the main thread via run_in_executor above (a blocking
        call here would freeze Kit's whole UI for the duration of the
        Ollama round trip). urllib is stdlib, so this works regardless of
        what's pip-installed in Kit's embedded Python, unlike 'requests'."""
        import json as _json
        import urllib.request as _ur
        body = _json.dumps({
            "prompt": prompt_text,
            "rooms": [{"name": r["name"]} for r in _agent_rooms],
        }).encode("utf-8")
        req = _ur.Request(
            "http://localhost:3000/api/agent", data=body,
            headers={"Content-Type": "application/json"}, method="POST",
        )
        with _ur.urlopen(req, timeout=120) as resp:
            return _json.loads(resp.read().decode("utf-8"))

    def _create_light(self, wx, wy, wz=0.0, wall_height_m=0.0, room_name=None):
        """Prefers planlift_floorview's own _spawn_light — same rig/height
        conventions, and (crucially) it registers into Floor Plan Tool's
        light list and floor-plan markers the same way a manually-dropped
        light does. A bare-prim fallback covers the case where that
        extension isn't loaded for some reason.

        wx/wy/wz come straight from editor.html's _buildRoomsDataForAgent —
        already-computed world coordinates (wz is the room's OWN floor's
        base height: 0 for the ground floor, stacking up by that floor's
        real wall+slab height for each floor above it), not raw pixel cx/cy.
        Kit no longer re-derives wx/wy from cx/cy + a single global
        img_w/img_h/world_scale, which only ever matched the ground floor —
        every floor above it silently collapsed onto the same Z.

        These three are still real-world METERS at this point — scaled to
        this stage's own units (see _STAGE_UNITS_PER_METER) before anything
        below touches them, so a dragged/placed light actually lands where
        its marker is instead of barely moving relative to the (correctly
        stage-unit-scaled) walls around it."""
        wx, wy, wz = wx * _STAGE_UNITS_PER_METER, wy * _STAGE_UNITS_PER_METER, wz * _STAGE_UNITS_PER_METER
        try:
            from planlift_floorview.extension import _INSTANCE as _fpv
            if _fpv is not None:
                light_path = _fpv._spawn_light(wx, wy, floor_base_z=wz, wall_height_m=wall_height_m or None, room_name=room_name)
                carb.log_info(f"[PlanLift] AI agent: light spawned via Floor Plan View at ({wx:.2f}, {wy:.2f}, base_z={wz:.2f})")
                return light_path
        except Exception as e:
            carb.log_warn(f"[PlanLift] AI agent: floorview light spawn failed ({e}), using fallback light")

        import omni.usd
        from pxr import UsdLux, UsdGeom, Gf
        global _agent_light_counter
        stage = omni.usd.get_context().get_stage()
        if not stage:
            raise RuntimeError("no stage open")
        abs_wz = wz + 2.3 * _STAGE_UNITS_PER_METER  # roughly ceiling height above this room's own floor, not wall-height-aware
        _agent_light_counter += 1
        light_path = f"/PlanLift_AgentLight_{_agent_light_counter}"
        light = UsdLux.SphereLight.Define(stage, light_path)
        light.CreateRadiusAttr(0.08)
        light.CreateIntensityAttr(20000.0)
        light.CreateColorAttr(Gf.Vec3f(1.0, 0.95, 0.85))
        UsdGeom.XformCommonAPI(light).SetTranslate((wx, wy, abs_wz))
        if room_name:
            light.GetPrim().SetCustomDataByKey("planlift:room", room_name)
        carb.log_info(f"[PlanLift] AI agent: light created (fallback) at ({wx:.2f}, {wy:.2f}, {abs_wz:.2f})")
        return light_path

    def _create_camera(self, wx, wy, wz=0.0, wall_height_m=0.0, preset_key=None, spec=None, room_name=None):
        """Prefers planlift_floorview's own _spawn_camera — same rig+optical
        prim structure the "Add Camera" button and drag-drop use, which is
        what the Camera List panel reads from and what the floor-plan
        marker's drag manipulator expects. A camera created any other way
        (e.g. a bare UsdGeom.Camera.Define, which is what this used to do)
        doesn't show up in that list and confuses the marker's drag
        controller, since it isn't under FloorPlanRig/Cameras with a
        registered rig path. Falls back to a bare camera only if that
        extension isn't loaded.

        wx/wy/wz come straight from editor.html's _buildRoomsDataForAgent —
        see _create_light's docstring for why this replaced deriving wx/wy
        from cx/cy + a single global img_w/img_h/world_scale (it silently
        collapsed every floor above the ground floor onto one Z). Still
        real-world METERS at this point — see _create_light's docstring
        for why these get scaled to stage units immediately below.

        preset_key, when given (from the user's own pick in the
        suggestions list — see _show_camera_suggestions), is used exactly
        as-is so the placed camera gets that preset's real resolution/
        focal length/aperture. Only falls back to guessing one from the
        free-text spec via _pick_camera_preset_key when no explicit pick
        was made. Returns (label, prim_path) — e.g. ("4MP", "/World/
        FloorPlanRig/Cameras/Cam_FPV_01_4MP") — so the caller can track
        the created prim in _agent_placed_items for the panel's placed-
        items list; prim_path is None if creation failed silently."""
        wx, wy, wz = wx * _STAGE_UNITS_PER_METER, wy * _STAGE_UNITS_PER_METER, wz * _STAGE_UNITS_PER_METER
        try:
            from planlift_floorview.extension import _INSTANCE as _fpv, _CAM_PRESETS
            if _fpv is not None:
                key = preset_key if preset_key else _pick_camera_preset_key(spec)
                preset = _CAM_PRESETS[key]
                cam_path = _fpv._spawn_camera(preset, wx, wy, floor_base_z=wz, wall_height_m=wall_height_m or None, room_name=room_name)
                carb.log_info(
                    f"[PlanLift] AI agent: camera ({preset['label']}) spawned via "
                    f"Floor Plan View at ({wx:.2f}, {wy:.2f}, base_z={wz:.2f})")
                return preset["label"], cam_path
        except Exception as e:
            carb.log_warn(f"[PlanLift] AI agent: floorview camera spawn failed ({e}), using fallback camera")

        import omni.usd
        from pxr import UsdGeom, Gf
        global _agent_camera_counter
        stage = omni.usd.get_context().get_stage()
        if not stage:
            raise RuntimeError("no stage open")
        abs_wz = wz + 2.4 * _STAGE_UNITS_PER_METER  # roughly ceiling height above this room's own floor, same first-pass assumption as _create_light
        _agent_camera_counter += 1
        cam_path = f"/PlanLift_AgentCamera_{_agent_camera_counter}"
        cam = UsdGeom.Camera.Define(stage, cam_path)
        cam.CreateFocalLengthAttr(18.0)   # wide-ish FOV, typical for a fixed security camera
        cam.CreateClippingRangeAttr(Gf.Vec2f(0.05, 1000.0))
        xf = UsdGeom.XformCommonAPI(cam)
        xf.SetTranslate((wx, wy, abs_wz))
        xf.SetRotate((-35.0, 0.0, 0.0))   # tilt down into the room (USD cameras look down -Z)
        if room_name:
            cam.GetPrim().SetCustomDataByKey("planlift:room", room_name)
        carb.log_info(f"[PlanLift] AI agent: camera created (fallback) at ({wx:.2f}, {wy:.2f}, {wz:.2f})")
        return "camera", cam_path

    def _create_camera_from_view(self, preset_key=None, spec=None):
        """"Add the camera in current view" (see the camera_view_room
        override in _handle_prompt) — unlike _create_camera, which mounts
        a camera at a floor-relative point with a generic ceiling height
        and a fixed downward tilt, this one matches Isaac Sim's own
        "Create Camera From View": position AND rotation are copied
        EXACTLY from whatever camera is active in the viewport right
        now. Fixes a reported bug where "add the camera in current view"
        landed roughly at the right spot but with the wrong height/tilt
        — the generic room-mount logic has no idea what the viewport
        camera's actual pose is, since it only ever gets a floor-level
        target point (x,y,z), never a rotation.

        Still goes through planlift_floorview's _spawn_camera first (not
        a bare prim) so this camera registers in the Camera List and the
        floor-plan marker system exactly like any other — only its
        transform is overridden afterward, straight to the viewport
        camera's full local-to-world matrix. The rig's generic mount
        translate/yaw and the optical child's fixed baseline tilt (both
        meaningless here) are cleared first so nothing double-applies on
        top of the copied matrix."""
        import omni.usd
        from pxr import Usd, UsdGeom

        stage = omni.usd.get_context().get_stage()
        if not stage:
            raise RuntimeError("no stage open")
        src_prim = _get_active_camera_prim(stage)
        if not src_prim:
            raise RuntimeError("no active camera in the viewport")
        mat = UsdGeom.Xformable(src_prim).ComputeLocalToWorldTransform(Usd.TimeCode.Default())
        pos = mat.ExtractTranslation()

        try:
            from planlift_floorview.extension import _INSTANCE as _fpv, _CAM_PRESETS
        except Exception as e:
            raise RuntimeError(f"camera presets unavailable: {e}")
        if _fpv is None:
            raise RuntimeError("Floor Plan View extension not loaded")
        key = preset_key if preset_key else _pick_camera_preset_key(spec)
        preset = _CAM_PRESETS[key]

        cam_path = _fpv._spawn_camera(preset, pos[0], pos[1], floor_base_z=0.0, wall_height_m=None, room_name="current view")
        if not cam_path:
            raise RuntimeError("camera spawn failed")

        rig_prim = stage.GetPrimAtPath(cam_path)
        optical_prim = stage.GetPrimAtPath(cam_path + "/Camera")
        UsdGeom.Xformable(rig_prim).ClearXformOpOrder()
        UsdGeom.Xformable(rig_prim).AddTransformOp().Set(mat)
        if optical_prim and optical_prim.IsValid():
            UsdGeom.Xformable(optical_prim).ClearXformOpOrder()

        carb.log_info(f"[PlanLift] AI agent: camera ({preset['label']}) created matching current view at {tuple(round(v,2) for v in pos)}")
        return preset["label"], cam_path

    async def _create_furniture(self, wx, wy, item, wz=0.0):
        """References an NVIDIA Omniverse asset (already USD and already
        Z-up) directly into the live stage by URL — no GLB conversion and
        no Y-up axis-fix rotate needed, unlike the local-catalog/Sketchfab
        GLB path this replaced. Scale is NOT trusted, though: the library
        isn't consistently meter-scaled (one sofa measured 259 units wide —
        centimeters, not meters), so a category-guessed footprint
        (item["footprint"]) is applied. Position is set directly to
        (wx, wy, wz), same as light/camera — no bbox-based pivot-centering,
        which measured size correctly but position unreliably for these
        assets (see the comment above the bbox measurement below).

        wx/wy/wz come straight from editor.html's _buildRoomsDataForAgent —
        see _create_light's docstring for why (wz is this item's own
        floor's base height, 0 for the ground floor). Still real-world
        METERS at this point — scaled to stage units below, same as
        _create_light/_create_camera/_apply_move (see
        _STAGE_UNITS_PER_METER's own docstring for why)."""
        import omni.usd
        from pxr import Usd, UsdGeom, Gf

        wx, wy, wz = wx * _STAGE_UNITS_PER_METER, wy * _STAGE_UNITS_PER_METER, wz * _STAGE_UNITS_PER_METER
        global _agent_furniture_counter

        model_url = item.get("model", "")
        if not model_url:
            raise RuntimeError("no model URL given")

        stage = omni.usd.get_context().get_stage()
        if not stage:
            raise RuntimeError("no stage open")

        _agent_furniture_counter += 1
        base_path = f"/PlanLift_AgentFurniture_{_agent_furniture_counter}"
        xform = UsdGeom.Xform.Define(stage, base_path)

        # Three-level hierarchy, one op type per prim, no exceptions:
        #   base_path            (xform)       — translate only
        #   base_path/Scaled     (scaler_prim) — scale only
        #   base_path/Scaled/Ref (ref_prim)    — the reference only, no ops
        #
        # This used to be two levels (scale AND translate both on `xform`,
        # reference on a child `Ref`) and it silently miscomposed: the
        # translate value was coming out multiplied by the scale factor
        # (verified — a translate of -9.58 landed at -0.07, and -9.58 x the
        # 0.0077 scale factor = -0.074, an exact match), meaning scale was
        # somehow being applied on top of translate instead of the other way
        # around. Rather than fight USD's xformOpOrder semantics again
        # (already gotten backwards once), parent-wraps-child composition is
        # unambiguous — no op-order guessing possible. The reference still
        # needs to be isolated on its own leaf prim, same reason as before:
        # the referenced asset may carry its own baked xformOps, and adding
        # an Add*Op() call on the same prim that already composes a
        # same-typed op raises ("xformOp:scale already exists in
        # xformOpOrder").
        scaler_prim = stage.DefinePrim(base_path + "/Scaled", "Xform")
        ref_prim = stage.DefinePrim(base_path + "/Scaled/Ref", "Xform")
        ref_prim.GetReferences().AddReference(model_url)

        # A reference just added to the live stage isn't guaranteed to be
        # fully composed/queryable in the same frame. These are remote HTTPS
        # references (one measured 21MB) that can take real wall-clock
        # seconds to download — a fixed few-frame yield was catching the
        # asset mid-download (an empty/near-empty bbox), computing scale/
        # pivot against that garbage, and the self-correction pass below was
        # then chasing a moving target as more geometry streamed in after.
        # Poll is_stage_loading() instead of guessing a frame count — same
        # mechanism this extension's own startup already uses to wait out
        # the main stage load.
        app = omni.kit.app.get_app()
        ctx = omni.usd.get_context()
        for _ in range(300):   # ~5s+ ceiling so a stuck download can't hang forever
            await app.next_update_async()
            try:
                if not ctx.is_stage_loading():
                    break
            except AttributeError:
                break   # older Kit API without this — fall back to the frames already spent

        # Measure native size (for scale only — NOT position). The bbox-based
        # "measure center, subtract it from translate" pivot-centering this
        # used to do was unreliable for these assets: size always came out
        # exactly right (a mathematical tautology — scale = target/measured,
        # so measured*scale = target regardless of whether the underlying
        # measurement was trustworthy), but position kept landing near world
        # origin no matter what room was targeted, because that guarantee
        # doesn't extend to the *center* the same way. Light/camera never
        # had this problem because they just set translate directly with no
        # bbox math involved — so furniture now does the same: scale from
        # the measured size, then translate straight to (wx, wy, 0), no
        # pivot-offset correction, no self-correction pass chasing a
        # measurement that wasn't reliable to begin with.
        footprint = item.get("footprint") or {}
        fw, fd, fh = footprint.get("widthM"), footprint.get("depthM"), footprint.get("heightM")
        scale = Gf.Vec3f(1.0, 1.0, 1.0)
        bbox_cache = UsdGeom.BBoxCache(
            Usd.TimeCode.Default(), [UsdGeom.Tokens.default_, UsdGeom.Tokens.render], useExtentsHint=False)
        rng = bbox_cache.ComputeWorldBound(xform.GetPrim()).ComputeAlignedRange()
        if not rng.IsEmpty():
            mn, mx = rng.GetMin(), rng.GetMax()
            dx = abs(mx[0] - mn[0]) or 1.0
            dy = abs(mx[1] - mn[1]) or 1.0
            dz = abs(mx[2] - mn[2]) or 1.0
            if fw and fd and fh:
                # fw/fd/fh are the target footprint in real METERS
                # (item["footprint"], a category guess in meters — see this
                # function's own docstring); dx/dy/dz are the asset's
                # CURRENT measured size in this STAGE's own units (see
                # _STAGE_UNITS_PER_METER's docstring for why those differ).
                # Without this, the asset would size to fw stage units
                # (~fw cm-equivalent) instead of fw real metres — comically
                # tiny next to the actual room.
                scale = Gf.Vec3f(fw * _STAGE_UNITS_PER_METER / dx,
                                  fd * _STAGE_UNITS_PER_METER / dy,
                                  fh * _STAGE_UNITS_PER_METER / dz)
            carb.log_info(
                f"[PlanLift] AI agent: NVIDIA asset bbox dx={dx:.3f} dy={dy:.3f} dz={dz:.3f} -> scale={tuple(scale)}"
                + (f" (target footprint {fw}x{fd}x{fh}m)" if fw else " (no footprint — native scale kept)"))
        else:
            carb.log_warn(f"[PlanLift] AI agent: bbox empty for {model_url} — no scale correction possible")
        # Scale on scaler_prim (the middle level), NOT on xform — see the
        # hierarchy comment above for why.
        UsdGeom.Xformable(scaler_prim).AddScaleOp().Set(scale)

        xform.AddTranslateOp().Set(Gf.Vec3d(wx, wy, wz))

        carb.log_info(f"[PlanLift] AI agent: furniture '{item['name']}' placed at ({wx:.2f}, {wy:.2f}, {wz:.2f})")

        # Diagnostic only — does NOT modify the placement (no more chasing/
        # correcting, that's what caused problems before). Just reports
        # where the object actually ended up so a "still not landing right"
        # report can be checked against real numbers instead of guessed at.
        for _ in range(300):
            await app.next_update_async()
            try:
                if not ctx.is_stage_loading():
                    break
            except AttributeError:
                break
        try:
            verify_cache = UsdGeom.BBoxCache(
                Usd.TimeCode.Default(), [UsdGeom.Tokens.default_, UsdGeom.Tokens.render], useExtentsHint=False)
            verify_rng = verify_cache.ComputeWorldBound(xform.GetPrim()).ComputeAlignedRange()
            if verify_rng.IsEmpty():
                carb.log_warn(f"[PlanLift] AI agent: VERIFY bbox for {base_path} is EMPTY")
            else:
                vmn, vmx = verify_rng.GetMin(), verify_rng.GetMax()
                actual_cx = (vmn[0] + vmx[0]) / 2.0
                actual_cy = (vmn[1] + vmx[1]) / 2.0
                carb.log_info(
                    f"[PlanLift] AI agent: VERIFY {base_path}: actual center=({actual_cx:.2f},{actual_cy:.2f}) "
                    f"vs intended ({wx:.2f},{wy:.2f}) — diff=({wx-actual_cx:.2f},{wy-actual_cy:.2f}) "
                    f"size=({vmx[0]-vmn[0]:.3f},{vmx[1]-vmn[1]:.3f},{vmx[2]-vmn[2]:.3f})")
        except Exception as e:
            carb.log_warn(f"[PlanLift] AI agent: verify bbox failed: {e}")

        return base_path


class PlanLiftUISetup(omni.ext.IExt):

    def on_startup(self, ext_id):
        carb.log_info("[PlanLift] UI setup started — extension.py v4")
        # Write a version marker so we can confirm which code Kit loaded.
        try:
            import os as _os, datetime as _dt
            _debug_dir = r"D:/PlanLift-Backend\output\isaac_tmp"
            _os.makedirs(_debug_dir, exist_ok=True)
            _marker = _os.path.join(_debug_dir, "extension_version.txt")
            with open(_marker, "a", encoding="utf-8") as _mf:
                _mf.write(_dt.datetime.now().strftime("%H:%M:%S")
                          + " extension.py v4 on_startup\n")
        except Exception as _me:
            carb.log_warn(f"[PlanLift] version marker write failed: {_me}")
        self._stage_opened = False
        self._stage_sub    = None
        self._sel_sub      = None
        # Path tracing is deferred to AFTER the scene + furniture are
        # fully loaded — enabling it during startup triggers heavy shader
        # compilation that can exceed the hang detector timeout and crash Kit.
        self._task = asyncio.ensure_future(self._setup())

    def on_shutdown(self):
        if self._task and not self._task.done():
            self._task.cancel()
        if self._stage_sub:
            self._stage_sub.unsubscribe()
            self._stage_sub = None
        if getattr(self, "_sel_sub", None):
            self._sel_sub.unsubscribe()
            self._sel_sub = None

    @staticmethod
    def _cp(label):
        """Write a checkpoint to isaac_tmp for debugging _setup() flow."""
        try:
            import datetime as _d, os as _o
            _dir = r"D:/PlanLift-Backend\output\isaac_tmp"
            _o.makedirs(_dir, exist_ok=True)
            with open(_o.path.join(_dir, "setup_checkpoints.txt"), "a",
                      encoding="utf-8") as _f:
                _f.write(_d.datetime.now().strftime("%H:%M:%S") + " " + label + "\n")
        except Exception:
            pass

    async def _setup(self):
        PlanLiftUISetup._cp("_setup START")
        try:
            app = omni.kit.app.get_app()
            PlanLiftUISetup._cp("got app ref")

            # Wait for all extensions and their UI to initialize.
            # Wrapped in BaseException to catch asyncio.CancelledError
            # (a BaseException, not Exception) if Kit hot-reloads us.
            for _fi in range(30):
                await app.next_update_async()
                if _fi == 0:
                    PlanLiftUISetup._cp("first await done")
        except BaseException as _e:
            PlanLiftUISetup._cp(f"CRASHED in 30-frame wait: {type(_e).__name__}: {_e}")
            return

        PlanLiftUISetup._cp("after 30-frame wait")
        try:
            import carb.settings as _cs_diag
            _rm_val = _cs_diag.get_settings().get("/rtx/rendermode")
            PlanLiftUISetup._cp(f"DIAG /rtx/rendermode (early) = {_rm_val!r}")
        except Exception as _rm_e:
            PlanLiftUISetup._cp(f"DIAG /rtx/rendermode (early) read FAILED: {_rm_e}")
        try:

            # Apply camera gizmo scale before anything else so saved user prefs
            # from previous sessions don't leave the camera invisibly small.
            self._set_camera_gizmo_scale()

            # Open USD first so the stage is live when we apply layout
            self._open_stage()
            PlanLiftUISetup._cp("after _open_stage")

            # Click-anywhere-on-assembly selection redirect (see method
            # docstring) — event-driven, safe to arm before furniture
            # exists since it just checks whatever gets clicked later.
            self._setup_selection_redirect()
            PlanLiftUISetup._cp("after _setup_selection_redirect")

            # First layout pass
            self._apply_layout()
            PlanLiftUISetup._cp("after _apply_layout #1")

            # Wait until the stage and all payloads finish loading
            # Must use "as" alias — bare "import omni.usd" creates a local
            # binding for "omni", shadowing the module-level import and causing
            # UnboundLocalError at the earlier omni.kit.app.get_app() call.
            import omni.usd as _omni_usd
            ctx = _omni_usd.get_context()
            for _fi in range(600):
                await app.next_update_async()
                _stage = ctx.get_stage()
                if not _stage:
                    continue
                try:
                    _loading = ctx.is_stage_loading()
                except AttributeError:
                    _loading = False  # API renamed in this Kit version
                if not _loading:
                    PlanLiftUISetup._cp(f"stage loaded after {_fi} frames")
                    break
            else:
                PlanLiftUISetup._cp("stage wait timed out at 600 frames")

            # Second layout pass catches anything that appeared after stage load
            self._apply_layout()
            PlanLiftUISetup._cp("after _apply_layout #2")

            # Make the dome light / sky show up through glass and reflections
            self._fix_dome_lights()
            PlanLiftUISetup._cp("after _fix_dome_lights")

            # Replace transparent/glass materials with OmniGlass (timelapse scene)
            PlanLiftUISetup._cp("CALLING _apply_glass_materials")
            self._apply_glass_materials()
            PlanLiftUISetup._cp("after _apply_glass_materials")

            # Replace steel-marked materials (pillars/trusses/tie-beams) with OmniPBR
            PlanLiftUISetup._cp("CALLING _apply_steel_materials")
            self._apply_steel_materials()
            PlanLiftUISetup._cp("after _apply_steel_materials")

            # Replace Pond water material with a real OmniGlass pool/sea shader
            PlanLiftUISetup._cp("CALLING _apply_water_materials")
            self._apply_water_materials()
            PlanLiftUISetup._cp("after _apply_water_materials")

            # Final layout pass
            for _ in range(10):
                await app.next_update_async()
            self._apply_layout()

            # Poll for /PlanLift_Furniture — the inject script adds this AFTER Kit
            # starts. Wait up to 10 minutes (36000 frames at 60fps) and apply
            # OmniGlass as soon as it appears and finishes loading.
            carb.log_info("[PlanLift] waiting for /PlanLift_Furniture injection...")
            PlanLiftUISetup._cp("starting /PlanLift_Furniture poll")
            for _pi in range(36000):
                await app.next_update_async()
                stage = ctx.get_stage()
                if not stage:
                    continue
                prim = stage.GetPrimAtPath("/PlanLift_Furniture")
                if prim and prim.IsValid():
                    carb.log_info("[PlanLift] /PlanLift_Furniture found — applying OmniGlass to web-app model")
                    PlanLiftUISetup._cp(f"/PlanLift_Furniture found after {_pi} poll frames")
                    # Extra ticks so the reference fully resolves before we scan
                    for _ in range(60):
                        await app.next_update_async()
                    self._apply_glass_materials()
                    self._apply_steel_materials()
                    self._apply_water_materials()
                    # Re-check rather than re-create: the early call at
                    # startup already creates the DomeLight if the preloaded
                    # scene didn't ship with one, so this is just a safety
                    # net in case something in furniture injection touched it.
                    self._fix_dome_lights(allow_create=False)
                    PlanLiftUISetup._cp("glass applied to /PlanLift_Furniture")
                    # Grace period before letting the stream pipeline treat
                    # the scene as ready: _apply_glass_materials/_steel/_water
                    # just bound dozens of new materials in rapid succession,
                    # each needing its shader compiled for the first time.
                    # That synchronous compile burst, happening right as the
                    # streaming pipeline expects its next frame, lines up with
                    # the NVST_R_FRAME_DROPPED/NVST_R_BUSY cascade + client
                    # disconnect seen immediately after this point — the same
                    # "heavy shader compilation triggers the hang detector"
                    # scenario the old deferred-path-tracing code was built
                    # to avoid, just triggered by this material-swap burst
                    # instead of a path-tracing switch. Yielding here gives
                    # compilation a chance to catch up before that happens.
                    for _ in range(180):
                        await app.next_update_async()
                    PlanLiftUISetup._cp("post-material grace period done")
                    try:
                        _rm_val2 = _cs_diag.get_settings().get("/rtx/rendermode")
                        PlanLiftUISetup._cp(f"DIAG /rtx/rendermode (post-grace) = {_rm_val2!r}")
                    except Exception as _rm_e2:
                        PlanLiftUISetup._cp(f"DIAG /rtx/rendermode (post-grace) read FAILED: {_rm_e2}")
                    break
            else:
                PlanLiftUISetup._cp("poll timed out — /PlanLift_Furniture never appeared")

            # Path Tracing used to auto-enable here (right after scene +
            # furniture + glass are set up, to avoid heavy shader compilation
            # during startup triggering the Kit hang detector) because
            # RTX Real-Time doesn't trace true refraction/reflection through
            # glass. Disabled per explicit request: Real-Time 2.0 is wanted
            # as the actual default now, not silently overridden back to
            # Path Tracing at the end of every session.
            # _enable_path_tracing() is left defined below for manual/future
            # use (e.g. a final-quality render step) if ever wanted again.
            #
            # A RendererSettingsFactory.set_current_renderer("Real-Time 2.0")
            # call was tried here and reverted: a RUNTIME renderer switch
            # tears down and rebuilds the whole rendering pipeline (shader
            # recompilation etc.) while the app is already under load from
            # furniture processing -- exactly the kind of heavy mid-session
            # work the comment above already identifies as triggering the
            # Kit hang detector. Confirmed live: adding that call lined up
            # with the same severe hang (NVST_R_FRAME_DROPPED/NVST_R_BUSY
            # cascade, client disconnect) this whole deferred-enable
            # mechanism was originally built to avoid. The CLI's own
            # --/rtx/rendermode=RealTimePathTracing (set once, before Kit's
            # renderer ever initializes, not as a runtime switch) is the
            # only thing touching render mode now.
            PlanLiftUISetup._cp("renderer selection left to the CLI launch arg only")

        except BaseException as _e:
            PlanLiftUISetup._cp(f"CRASHED in main _setup body: {type(_e).__name__}: {_e}")
            raise

    def _set_camera_gizmo_scale(self):
        """Force camera gizmo scale to Isaac Sim defaults regardless of saved user prefs."""
        try:
            import carb.settings
            s = carb.settings.get_settings()
            s.set("/app/viewport/show/camera", True)
            s.set("/persistent/app/viewport/show/camera", True)
            # Cameras use world-space gizmo scale (constantScaleCamera is
            # False below), so this value directly controls how big the
            # built-in camera icon renders relative to the room — raised
            # from 0.01 per explicit request ("the camera is tiny, make it
            # bigger"). Adjust this one number up/down to retune size.
            s.set("/persistent/app/viewport/gizmo/scale", 0.5)
            s.set("/persistent/app/viewport/gizmo/constantScaleEnabled", True)
            s.set("/persistent/app/viewport/gizmo/constantScaleCamera", False)
            s.set("/persistent/app/viewport/gizmo/constantScale", 10.0)
            carb.log_info("[PlanLift] Camera gizmo scale set to Isaac Sim defaults")
        except Exception as e:
            carb.log_error(f"[PlanLift] set_camera_gizmo_scale: {e}")

    def _setup_selection_redirect(self):
        """Clicking any ONE part of a multi-mesh assembly (e.g. a window's
        glass pane vs its frame vs its handle — separate meshes/GeomSubsets
        after api_server.py's glass/wood split) should select the whole
        assembly, not just the part under the cursor. api_server.py tags
        the root of each such assembly with a custom data key
        ("planlift:selectRoot") while injecting furniture; this listens for
        every selection change and, if the newly selected prim sits under
        one of those tagged roots, replaces the selection with the root
        instead. Guarded against feeding its own re-selection back into
        itself (set_selected_prim_paths below fires another SELECTION_CHANGED
        event synchronously)."""
        import omni.usd

        self._redirecting_selection = False

        def _on_stage_event(event):
            if self._redirecting_selection:
                return
            if event.type != int(omni.usd.StageEventType.SELECTION_CHANGED):
                return
            ctx = omni.usd.get_context()
            stage = ctx.get_stage()
            if not stage:
                return
            sel = ctx.get_selection()
            paths = sel.get_selected_prim_paths()
            if not paths:
                return
            new_paths = []
            changed = False
            for p in paths:
                prim = stage.GetPrimAtPath(p)
                target_path = p
                walker = prim
                while walker and walker.IsValid():
                    if walker.HasCustomDataKey("planlift:selectRoot"):
                        target_path = str(walker.GetPath())
                        break
                    walker = walker.GetParent()
                if target_path != p:
                    changed = True
                new_paths.append(target_path)
            if not changed:
                return
            deduped = list(dict.fromkeys(new_paths))
            self._redirecting_selection = True
            try:
                sel.set_selected_prim_paths(deduped, True)
            finally:
                self._redirecting_selection = False

        self._sel_sub = (
            omni.usd.get_context()
            .get_stage_event_stream()
            .create_subscription_to_pop(
                _on_stage_event, name="PlanLift component-select redirect"
            )
        )
        carb.log_info("[PlanLift] component-select redirect subscription active")

    def _enable_path_tracing(self):
        """RTX Real-Time approximates transparency and does not trace true
        refraction/reflection through glass. Path Tracing does."""
        try:
            import carb.settings
            settings = carb.settings.get_settings()
            settings.set("/rtx/rendermode", "PathTracing")
            settings.set("/rtx/pathtracing/maxBounces", 8)
            settings.set("/rtx/pathtracing/maxSpecularAndTransmissionBounces", 8)
            # Hide viewport grid
            settings.set("/app/viewport/grid/enabled", False)
            settings.set("/persistent/app/viewport/grid/enabled", False)
            settings.set("/app/viewport/grid/visible", False)
            carb.log_info("[PlanLift] Render mode -> PathTracing, grid hidden")
        except Exception as e:
            carb.log_error(f"[PlanLift] enable path tracing: {e}")

    # Local HDRI swapped in for every DomeLight found in the preloaded scene
    # (startup.py's updated_timelapse.usd) — same "hardcode the machine-
    # local asset path" convention startup.py itself already uses for that
    # USD file. Change this path (or set it to None to leave whatever
    # texture the DomeLight already has) to use a different environment.
    _HDRI_PATH = r"C:\Users\Piyush\Downloads\aarfontein_pan_4k.exr"

    def _fix_dome_lights(self, allow_create=True):
        """Ensure the dome light / sky is rendered as a visible background
        (so glass has something to reflect/transmit instead of black void),
        and pointed at _HDRI_PATH rather than whatever texture the preloaded
        stage happened to ship with. Confirmed live: the preloaded
        updated_timelapse.usd scene this runs against has NO DomeLight prim
        at all when this fires (zero hits, zero log lines, every launch) —
        the old version silently no-opped for that reason, which is exactly
        why the HDRI never showed. allow_create=True creates one from
        scratch in that case; pass False for a later re-check call that
        shouldn't spawn a second light if one now exists."""
        try:
            import omni.usd
            from pxr import Sdf, Usd, UsdLux

            stage = omni.usd.get_context().get_stage()
            if not stage:
                return

            hdri_path = None
            if self._HDRI_PATH:
                import os as _os3
                if _os3.path.exists(self._HDRI_PATH):
                    hdri_path = self._HDRI_PATH.replace("\\", "/")
                else:
                    carb.log_warn(f"[PlanLift] HDRI not found, leaving existing texture: {self._HDRI_PATH}")

            found = 0
            for prim in Usd.PrimRange(stage.GetPseudoRoot()):
                if prim.GetTypeName() != "DomeLight":
                    continue
                found += 1
                for attr_name in ("inputs:visibleInPrimaryRay", "visibleInPrimaryRay"):
                    try:
                        attr = prim.GetAttribute(attr_name)
                        if not attr.IsValid():
                            attr = prim.CreateAttribute(attr_name, Sdf.ValueTypeNames.Bool)
                        attr.Set(True)
                    except Exception:
                        pass
                if hdri_path:
                    try:
                        tex_attr = prim.GetAttribute("inputs:texture:file")
                        if not tex_attr.IsValid():
                            tex_attr = prim.CreateAttribute("inputs:texture:file", Sdf.ValueTypeNames.Asset)
                        tex_attr.Set(Sdf.AssetPath(hdri_path))
                        carb.log_info(f"[PlanLift] DomeLight texture -> {hdri_path}: {prim.GetPath()}")
                    except Exception as _te:
                        carb.log_error(f"[PlanLift] DomeLight texture set failed: {_te}")
                carb.log_info(f"[PlanLift] DomeLight made visible: {prim.GetPath()}")

            if found == 0:
                carb.log_warn("[PlanLift] fix dome lights: no DomeLight prim found in stage")
                if allow_create:
                    path = Sdf.Path("/World/PlanLift_HDRI_DomeLight")
                    if not stage.GetPrimAtPath(path).IsValid():
                        dome = UsdLux.DomeLight.Define(stage, path)
                        dome.CreateIntensityAttr(1000.0)
                        if hdri_path:
                            dome.CreateTextureFileAttr(Sdf.AssetPath(hdri_path))
                        prim = dome.GetPrim()
                        for attr_name in ("inputs:visibleInPrimaryRay", "visibleInPrimaryRay"):
                            try:
                                attr = prim.CreateAttribute(attr_name, Sdf.ValueTypeNames.Bool)
                                attr.Set(True)
                            except Exception:
                                pass
                        carb.log_info(f"[PlanLift] created DomeLight at {path} (texture={hdri_path})")
        except Exception as e:
            carb.log_error(f"[PlanLift] fix dome lights: {e}")

    def _open_stage(self):
        if self._stage_opened:
            return
        try:
            import omni.usd
            ctx = omni.usd.get_context()
            stage = ctx.get_stage()
            if stage:
                # startup.py already opened/created a stage (the real timelapse
                # USD, or a fresh empty one if that file wasn't found) before
                # this extension runs. Re-calling open_stage() here regardless
                # of whether it matches USD_FILE used to be destructive: on
                # failure it clobbers the current stage rather than leaving it
                # alone, which is what broke furniture injection — the working
                # empty stage from startup.py's fallback got wiped out by this
                # extension's own redundant (and failing) open_stage(USD_FILE).
                carb.log_info("[PlanLift] Stage already present — skipping open_stage")
                self._stage_opened = True
                return
            result = ctx.open_stage(USD_FILE)
            carb.log_info(f"[PlanLift] open_stage -> {result}")
            if not result:
                carb.log_warn(f"[PlanLift] {USD_FILE} not found — creating empty stage instead")
                ctx.new_stage()
            self._stage_opened = True
        except Exception as e:
            carb.log_error(f"[PlanLift] open_stage: {e}")

    def _apply_layout(self):
        import omni.ui

        # ── 0. Re-enable extensions Kit disabled during its own boot ───────
        # Root cause of Stage/Property never working, found by reading the
        # kit log: omni.kit.window.stage and omni.kit.property.bundle both
        # start up fine at app boot (a few ms each) but are shut down again
        # about 3 seconds later, in the same batch as
        # omni.kit.hotkeys.core/manipulator.camera/manipulator.selection/
        # manipulator.prim/widget.toolbar — an extension-manager
        # reconciliation pass this app's boot sequence triggers, nothing
        # planlift.ui_setup does. Neither restarts on its own. Once shut
        # down, Stage's on_shutdown() calls
        # ui.Workspace.set_show_window_fn("Stage", None), so every
        # Workspace.show_window("Stage")/get_window("Stage") call we tried
        # was hitting an unregistered callback the whole time — that's why
        # nothing ever worked, not a docking or visibility-flag problem.
        # property.bundle is what registers all the actual property-editor
        # widgets (camera/transform/usd/material/light/render/geometry/
        # audio) into the (still-alive) Property window — without it the
        # window shows up but stays permanently blank, which is exactly
        # what re-enabling it fixes. Re-enabling bundle auto-enables those
        # dependencies too (same as widget.stage did for window.stage), so
        # they don't need to be listed individually.
        #
        # manipulator.camera drives WASD/mouse-fly viewport navigation and
        # mouse-drag camera orbit/pan. manipulator.selection/manipulator.prim
        # drive the move/rotate/scale drag gizmo for any selected prim.
        # widget.toolbar (the Move/Rotate/Scale/Select tool buttons) comes
        # along as manipulator.prim.core's optional dependency.
        #
        # omni.kit.widget.toolbar's own hotkey.py is explicit about hotkeys.core
        # NOT being a hard dependency of it: "If omni.kit.hotkeys.core is not
        # enabled, hotkey will not be in effect" (it subscribes to that
        # extension's enable/disable events and only registers the W/E/R
        # bindings while it happens to be on). Nothing here formally requires
        # it, so it was only ever enabled incidentally via some other
        # extension's optional-dependency chain — not guaranteed. Listed
        # explicitly now so W/E/R are deterministic instead of "on if we got
        # lucky with pull-in order."
        #
        # omni.kit.widget.toolbar also has NO window of its own — it's just
        # the button-group logic. omni.kit.window.toolbar is the extension
        # that actually creates the "Main ToolBar" ui.Window and calls
        # rebuild_toolbar(root_frame=...), the ONLY code path that runs
        # TransformButtonGroup.create() and sets self._move_button. That gap
        # (not _HIDE_WINDOWS, which was fixed first and wasn't the real
        # cause) is what crashed "W" with AttributeError before this.
        for _ext_id in (
            "omni.kit.window.stage", "omni.kit.property.bundle",
            "omni.kit.manipulator.camera", "omni.kit.manipulator.selection",
            "omni.kit.manipulator.prim", "omni.kit.hotkeys.core",
            "omni.kit.window.toolbar",
        ):
            try:
                ext_mgr = omni.kit.app.get_app().get_extension_manager()
                if not ext_mgr.is_extension_enabled(_ext_id):
                    ext_mgr.set_extension_enabled_immediate(_ext_id, True)
                    carb.log_info(f"[PlanLift] Re-enabled {_ext_id}")
            except Exception as e:
                carb.log_error(f"[PlanLift] re-enable {_ext_id}: {e}")

        # ── 0b. Undo floorplan.tool's own viewport navigation lock ─────────
        # FOUND THE ACTUAL ROOT CAUSE of every "W does nothing / scroll does
        # nothing" report: it isn't Kit's boot process at all. floorplan.tool
        # (a sibling extension, floorplan/tool/extension.py) deliberately
        # calls _disable_viewport_manipulators() from its own
        # _setup_drop_listener(), specifically so dragging a camera/light
        # preset into the viewport can't be disrupted by ordinary WASD/mouse
        # navigation. That method does two things, both needed to undo it:
        #  1. Disables the 5 extensions above via set_extension_enabled_immediate
        #     (which is why re-enabling them here, alone, looked like it should
        #     work but didn't).
        #  2. Sets .visible = False on the viewport's own "Camera"/"Selection"/
        #     "ContextMenu"/"ObjectClick" manipulator LAYERS — a separate
        #     mechanism this file never touched. Per its own comment, these
        #     layers register gesture handling straight on the viewport's
        #     scene view rather than through omni.ui widgets, so merely
        #     re-enabling the owning extension doesn't reattach them — that's
        #     the specific reason "Adding ui.scenes dependent on
        #     omni.kit.manipulator.camera" never re-fires after step 0's
        #     re-enable, while the extension itself shows as enabled again.
        # floorplan.tool already has the correct, symmetric undo for both
        # halves — _restore_viewport_manipulators() — so call that instead of
        # trying to re-derive it here.
        try:
            from floorplan.tool import extension as _fpt_mod
            _fpt = getattr(_fpt_mod, "_INSTANCE", None)
            if _fpt is not None and getattr(_fpt, "_vp_manipulators_disabled", False):
                _fpt._restore_viewport_manipulators()
                carb.log_info("[PlanLift] Restored floorplan.tool's viewport manipulators/layers")
        except Exception as e:
            carb.log_error(f"[PlanLift] restore fpt viewport manipulators: {e}")

        # ── 1. Menu bar ──────────────────────────────────────────────────
        # Forced visible (not hidden) so Window > Stage can be used directly.
        # This explicitly sets visible = True (not just "doesn't hide it")
        # because the app has been running continuously through many dev
        # hot-reloads — the menu bar was already latched hidden by earlier
        # runs of this same code, and merely no longer calling
        # `.visible = False` doesn't undo a state that's already set.
        try:
            from omni.kit.mainwindow import get_main_window
            mw = get_main_window()
            if mw:
                mw.get_main_menu_bar().visible = True
                carb.log_info("[PlanLift] Menu bar forced visible")
        except Exception as e:
            carb.log_error(f"[PlanLift] show menu: {e}")

        # ── 2. Close unwanted panels ──────────────────────────────────────
        for name in _HIDE_WINDOWS:
            try:
                w = omni.ui.Workspace.get_window(name)
                if w and w.visible:
                    w.visible = False
                    carb.log_info(f"[PlanLift] Hidden: {name}")
            except Exception as e:
                carb.log_error(f"[PlanLift] hide {name}: {e}")

        # TEMP (debugging): force these panels open via ui.Workspace.show_window().
        # Confirmed from omni.kit.window.stage's own source
        # (stage_extension.py): these standard panels don't behave like
        # plain persistent windows with a simple .visible flag — each one
        # registers a creation callback via
        # ui.Workspace.set_show_window_fn(name, self.show_window), and
        # hiding one (.visible = False) actually DESTROYS the underlying
        # window object a frame later (self._window.destroy(); self._window
        # = None). That's exactly what years of this extension setting
        # .visible = False on them did. Directly flipping .visible = True
        # on a stale/destroyed reference (what the previous version of this
        # code did, via Workspace.get_windows()) does nothing — it has to
        # go through show_window(), which is what actually re-invokes the
        # registered callback and rebuilds the window, the same call the
        # menu item itself would trigger. Stage is intentionally excluded
        # here — use Window > Stage from the menu bar for it (see note above).
        # (Stage itself gets hidden later, in set_agent_context() once the
        # PlanLift AI Agent panel is created — see that function's comment.
        # Window > Stage still works manually
        # mid-session if needed; it just won't be the default view.)
        # Toolbar included here too — removing it from _HIDE_WINDOWS only
        # stops this code from hiding it; it doesn't force it to have been
        # shown in the first place (this app's saved layout starts it
        # closed). Without an actual show_window() call, TransformButtonGroup
        # (Move/Rotate/Scale) never runs its create() — which is the only
        # place that sets self._move_button — so the "W" hotkey kept
        # crashing on a never-created attribute even after unhiding.
        for name in ("Property", "Properties", "Console", "Toolbar", "Main ToolBar"):
            try:
                omni.ui.Workspace.show_window(name)
                carb.log_info(f"[PlanLift] show_window({name!r}) called")
            except Exception as e:
                carb.log_warn(f"[PlanLift] show_window({name!r}) failed: {e}")

        # ── 2b. Re-hide Property/Stage after the forced show above ─────────
        # The force-show two steps up is a one-time-per-_apply_layout()-call
        # necessity (rebuilds property.bundle's widgets so the window isn't
        # permanently blank — see the long comment above), but _apply_layout
        # itself re-runs on every extension reload, while the AI Agent
        # panel's own hide (set_agent_context's _hide_stage_and_property) is
        # gated behind `if _agent_panel is None` and so only ever fires
        # once, the first time that panel is created. Net effect without
        # this: every reload after the panel already exists force-shows
        # Property again with nothing left to hide it back — exactly the
        # "Property tab is back" report this fixes. Scoped to Property/
        # Properties/Stage only; Console/Toolbar/Main ToolBar are meant to
        # stay visible per the comment above and are intentionally excluded.
        async def _rehide_property_after_layout():
            app = omni.kit.app.get_app()
            for _ in range(15):
                await app.next_update_async()
            for _name in ("Property", "Properties", "Stage"):
                try:
                    w = omni.ui.Workspace.get_window(_name)
                    if w and w.visible:
                        w.visible = False
                        carb.log_info(f"[PlanLift] Re-hidden after layout: {_name}")
                except Exception as e:
                    carb.log_error(f"[PlanLift] re-hide {_name}: {e}")
        asyncio.ensure_future(_rehide_property_after_layout())

        # ── 3. Dock Env Day Cycle Control to the right of the Viewport ──
        # Floor Plan Tool uses deferred_dock_in("Env Day Cycle Control") to tab
        # alongside it, so we only need to place Env Day Cycle Control itself.
        # Do NOT use Stage/Property as anchors — they occupy a different dock area
        # and would pull Env Day Cycle Control out of the right-sidebar layout.
        try:
            env_w = omni.ui.Workspace.get_window("Env Day Cycle Control")
            if env_w:
                docked = False
                for anchor in ("Viewport", "Viewport 0", "DockSpace"):
                    anchor_w = omni.ui.Workspace.get_window(anchor)
                    if anchor_w:
                        env_w.dock_in(anchor_w, omni.ui.DockPosition.RIGHT, 0.25)
                        carb.log_info(f"[PlanLift] Docked Env Day Cycle Control right of {anchor}")
                        docked = True
                        break
                if not docked:
                    carb.log_warn("[PlanLift] No anchor window found for env_day_cycle dock")
                # Prevent the user from closing Env Day Cycle Control
                def _env_guard(visible, _w=env_w):
                    if not visible:
                        async def _r():
                            await omni.kit.app.get_app().next_update_async()
                            try:
                                _w.visible = True
                            except Exception:
                                pass
                        asyncio.ensure_future(_r())
                env_w.set_visibility_changed_fn(_env_guard)
        except Exception as e:
            carb.log_error(f"[PlanLift] dock env: {e}")

        # ── 4. Dock Floor Plan View below Env Day Cycle Control ───────────────
        try:
            fp_w  = omni.ui.Workspace.get_window("Floor Plan View")
            env_w2 = omni.ui.Workspace.get_window("Env Day Cycle Control")
            if fp_w and env_w2:
                fp_w.dock_in(env_w2, omni.ui.DockPosition.BOTTOM, 0.4)
                carb.log_info("[PlanLift] Docked Floor Plan View below Env Day Cycle Control")
        except Exception as e:
            carb.log_error(f"[PlanLift] dock fp: {e}")


    def _apply_glass_materials(self):
        """
        Detect glass/window panes four ways:
          1. UsdPreviewSurface with opacity < 1.0 or transmission > 0
          2. OmniPBR MDL with enable_opacity=True and opacity_constant < 1.0
          3. Any Material prim whose name contains a glass/window keyword
          4. Any Mesh prim whose name contains a glass-pane keyword
          5. Thin flat-plane Mesh siblings of any pane found above
             (catches the "other" pane in a window that kept a generic
             opaque material with no naming hint)

        All detected prims get rebound to a single 20%-opacity glass material.
        """
        try:
            import datetime as _dt2, os as _os2
            _debug_dir2 = r"D:/PlanLift-Backend\output\isaac_tmp"
            _os2.makedirs(_debug_dir2, exist_ok=True)
            with open(_os2.path.join(_debug_dir2, "glass_debug.txt"), "a",
                      encoding="utf-8") as _gdf:
                _gdf.write(_dt2.datetime.now().strftime("%H:%M:%S")
                           + " _apply_glass_materials v4 called\n")
        except Exception as _gde:
            carb.log_warn(f"[PlanLift] glass debug write failed: {_gde}")

        try:
            from pxr import UsdShade, Sdf, Usd
            import omni.usd

            stage = omni.usd.get_context().get_stage()
            if not stage:
                carb.log_warn("[PlanLift] glass: stage not available")
                return

            glass_mat_paths = set()

            # ── Pass -1: exclude TEXTURED alpha-blend materials up front ───
            # (leaf cards on a real dropped tree asset, or any other
            # imported GLB using alpha-cutout foliage/fabric) so Pass 1 and
            # Pass 6 below don't mistake them for architectural glass. Real
            # glass panes exported by this app are always a flat/untextured
            # tint (see sharedGlassPaneMat / the trusted "PlanLift_Glass"
            # name in Pass 0 — no baseColorTexture at all); a BLEND-mode
            # material that DOES carry a real baseColorTexture is foliage/
            # fabric/etc, not glass. Confirmed live: a dropped tree asset's
            # leaf material (alphaMode:"BLEND", a real leaf PNG as its
            # baseColorTexture, default/opaque baseColorFactor so Pass 5's
            # own alpha-factor check correctly leaves it alone) was still
            # getting swapped to the transparent glass material by Pass 1's
            # bare alpha_mode check and Pass 6's flat-plane check, turning
            # the whole tree washed-out white a few seconds after it first
            # appeared correctly (the delay is furniture injection + the
            # settle wait before this function's second call — see _setup).
            # Reads the same newest-GLB-in-isaac_tmp Pass 5 reads lower down
            # (kept as a separate lightweight parse here so this exclusion
            # set exists before Pass 1 runs, rather than restructuring the
            # whole pass order).
            _textured_blend_mat_names = set()
            try:
                import struct as _es, json as _ej, glob as _eg, os as _eo
                _early_glb_dir = "D:/PlanLift-Backend/output/isaac_tmp"
                _early_glb_files = sorted(
                    _eg.glob(_early_glb_dir + "/*.glb"),
                    key=lambda f: _eo.path.getmtime(f),
                    reverse=True,
                )
                if _early_glb_files:
                    _early_data = open(_early_glb_files[0], "rb").read()
                    if _early_data[:4] == b'glTF':
                        _early_clen = _es.unpack_from('<I', _early_data, 12)[0]
                        _early_gltf = _ej.loads(_early_data[20:20 + _early_clen])
                        for _ei, _emat in enumerate(_early_gltf.get('materials', [])):
                            if _emat.get('alphaMode') != 'BLEND':
                                continue
                            if _emat.get('pbrMetallicRoughness', {}).get('baseColorTexture'):
                                # Kit's glTF importer does NOT always rename
                                # every material to "materialN" — confirmed
                                # live (glass_debug.txt DEBUG-MAT dump) that
                                # a uniquely-named source material (e.g. a
                                # tree's "leaf003_basecolor") keeps its
                                # ORIGINAL name in the converted stage, only
                                # colliding/generic ones fall back to
                                # "materialN". Add both forms so whichever
                                # one Kit actually used still matches.
                                _textured_blend_mat_names.add(f"material{_ei}")
                                if _emat.get('name'):
                                    _textured_blend_mat_names.add(_emat['name'])
                        # USD prim names can't contain a "." (or other
                        # punctuation glTF material names freely use, e.g.
                        # Blender's own duplicate-name suffixing style
                        # "leaf003_basecolor.004") — Kit's importer sanitizes
                        # those into a valid identifier, exact spelling
                        # unconfirmed. Comparing on an alphanumeric-only,
                        # lowercased form of BOTH sides (below, at the actual
                        # Pass 1/Pass 6 checks) sidesteps needing to guess
                        # that exact sanitization rule — confirmed live: the
                        # very leaf material this whole fix targets
                        # ("leaf003_basecolor.004") was STILL reaching glass
                        # conversion despite being in this raw-name set,
                        # while the dot-free "leaf003_basecolor" (same GLB)
                        # was correctly excluded — the punctuation was
                        # exactly what broke the exact-string match.
                        # Always logged (not just on a match) — written
                        # directly to glass_debug.txt via _dbg_write since
                        # _glog() isn't defined yet at this point in the
                        # function, and Pass 0-4's own carb.log_info calls
                        # below are console-only, invisible in that file too.
                        def _dbg_write(msg):
                            carb.log_info(msg)
                            try:
                                import datetime as _dbgdt
                                with open("D:/PlanLift-Backend/output/isaac_tmp/glass_debug.txt",
                                          "a", encoding="utf-8") as _dbgf:
                                    _dbgf.write(_dbgdt.datetime.now().strftime("%H:%M:%S") + " " + msg + "\n")
                            except Exception:
                                pass
                        _dbg_write(
                            f"[PlanLift] glass: Pass-1 textured-blend exclusion scan of "
                            f"{_early_glb_files[0]} -> excluding {_textured_blend_mat_names or '(none found)'}"
                        )
            except Exception as _ee:
                carb.log_warn(f"[PlanLift] glass: textured-blend exclusion scan failed: {_ee}")
                try:
                    import datetime as _dbgdt2
                    with open("D:/PlanLift-Backend/output/isaac_tmp/glass_debug.txt",
                              "a", encoding="utf-8") as _dbgf2:
                        _dbgf2.write(_dbgdt2.datetime.now().strftime("%H:%M:%S")
                                     + f" [PlanLift] glass: textured-blend exclusion scan FAILED: {_ee}\n")
                except Exception:
                    pass

            def _norm_mat_name(name):
                """Alphanumeric-only, lowercased form — used to compare a
                USD prim name against _textured_blend_mat_names without
                needing to know Kit's exact punctuation-sanitization rule
                for characters glTF material names allow but USD prim names
                don't (e.g. "."). See Pass -1 above for why."""
                return re.sub(r'[^a-zA-Z0-9]', '', name or '').lower()

            _textured_blend_mat_names_norm = {_norm_mat_name(_n) for _n in _textured_blend_mat_names}

            # ── Pass 0: trusted exact name from the web app ────────────────
            # The web editor names every window-glass material "PlanLift_Glass"
            # before export. Blender/USD round-tripping may suffix duplicates
            # ("PlanLift_Glass_001", "PlanLift_Glass.001" -> "_001"), so match
            # by prefix. This is the authoritative source — no heuristics needed.
            for prim in Usd.PrimRange(stage.GetPseudoRoot()):
                if prim.GetTypeName() != "Material":
                    continue
                if prim.GetName().startswith("PlanLift_Glass"):
                    glass_mat_paths.add(prim.GetPath())
                    carb.log_info(f"[PlanLift] glass: web-app-name-detect -> {prim.GetPath()}")

            # ── Pass 1: scan shaders for transparency indicators ───────────
            for prim in Usd.PrimRange(stage.GetPseudoRoot()):
                if prim.GetTypeName() != "Shader":
                    continue

                shader = UsdShade.Shader(prim)
                mat_path = prim.GetPath().GetParentPath()

                # UsdPreviewSurface: opacity < 1 OR transmission > 0
                id_attr = shader.GetIdAttr()
                if id_attr and id_attr.IsValid() and id_attr.Get() == "UsdPreviewSurface":
                    op_in = shader.GetInput("opacity")
                    tr_in = shader.GetInput("transmission")
                    op_v = op_in.Get() if (op_in and op_in.IsValid()) else 1.0
                    tr_v = tr_in.Get() if (tr_in and tr_in.IsValid()) else 0.0
                    if (op_v is not None and op_v < 0.99) or (tr_v is not None and tr_v > 0.01):
                        glass_mat_paths.add(mat_path)
                        carb.log_info(
                            f"[PlanLift] glass: opacity-detect opacity={op_v} "
                            f"transmission={tr_v} -> {mat_path}"
                        )
                    continue

                # MDL material (OmniPBR or GLTF-converter output)
                src_attr = prim.GetAttribute("info:mdl:sourceAsset")
                if not (src_attr and src_attr.IsValid()):
                    continue
                src_val = src_attr.Get()
                src_str = (src_val.path if src_val else "") or ""
                if "OmniGlass" in src_str:
                    continue  # already glass, skip

                # GLTF converter uses inputs:alpha_mode (int): 2 = BLEND —
                # excluding known textured-blend (foliage/fabric) materials,
                # see Pass -1 above.
                am_attr = prim.GetAttribute("inputs:alpha_mode")
                if am_attr and am_attr.IsValid():
                    am_v = am_attr.Get()
                    if am_v == 2 and _norm_mat_name(mat_path.name) not in _textured_blend_mat_names_norm:  # BLEND
                        glass_mat_paths.add(mat_path)
                        carb.log_info(f"[PlanLift] glass: alpha_mode=BLEND -> {mat_path}")
                        continue

                # OmniPBR: enable_opacity=true + opacity_constant < 1
                en = prim.GetAttribute("inputs:enable_opacity")
                op_c = prim.GetAttribute("inputs:opacity_constant")
                if en and en.IsValid() and en.Get():
                    v = op_c.Get() if (op_c and op_c.IsValid()) else None
                    if v is not None and v < 0.99:
                        glass_mat_paths.add(mat_path)
                        carb.log_info(f"[PlanLift] glass: OmniPBR-detect opacity={v} -> {mat_path}")

            # ── Pass 2: keyword match on Material prim names ──────────────
            all_mat_names = []
            for prim in Usd.PrimRange(stage.GetPseudoRoot()):
                if prim.GetTypeName() != "Material":
                    continue
                all_mat_names.append(prim.GetName())
                name_lower = prim.GetName().lower()
                if any(k in name_lower for k in _GLASS_KEYWORDS):
                    glass_mat_paths.add(prim.GetPath())
                    carb.log_info(f"[PlanLift] glass: keyword-detect -> {prim.GetPath()}")

            # ── Pass 3: keyword match on mesh prim names directly ──────────
            # Catches glass panes that kept a generic opaque material whose
            # name gave no hint (common with Blender exports).
            glass_mesh_prims = []
            for prim in Usd.PrimRange(stage.GetPseudoRoot()):
                if prim.GetTypeName() != "Mesh":
                    continue
                name_lower = prim.GetName().lower()
                if any(k in name_lower for k in _GLASS_MESH_KEYWORDS):
                    glass_mesh_prims.append(prim)
                    carb.log_info(f"[PlanLift] glass: mesh-name-detect -> {prim.GetPath()}")

            # ── Pass 4: sibling thin-plane propagation ─────────────────────
            # Window assemblies often have two panes (e.g. Glass_L / Plane.004)
            # where only one matched by name/material. If a confirmed glass
            # mesh is a thin flat plane, convert sibling meshes under the same
            # parent that are also thin flat planes — they're almost certainly
            # the other pane(s).
            from pxr import UsdGeom, Usd

            bbox_cache = UsdGeom.BBoxCache(
                Usd.TimeCode.Default(),
                [UsdGeom.Tokens.default_, UsdGeom.Tokens.render],
            )

            def is_thin_plane(p, ratio=0.08):
                try:
                    size = bbox_cache.ComputeWorldBound(p).GetBox().GetSize()
                    dims = sorted(abs(d) for d in size)
                    if dims[2] <= 1e-6:
                        return False
                    return dims[0] / dims[2] < ratio
                except Exception:
                    return False

            confirmed_paths = {p.GetPath() for p in glass_mesh_prims}
            confirmed_prims = list(glass_mesh_prims)
            for prim in Usd.PrimRange(stage.GetPseudoRoot()):
                if prim.GetTypeName() != "Mesh":
                    continue
                bound_path = UsdShade.MaterialBindingAPI(prim).GetDirectBinding().GetMaterialPath()
                if bound_path in glass_mat_paths and prim.GetPath() not in confirmed_paths:
                    confirmed_paths.add(prim.GetPath())
                    confirmed_prims.append(prim)

            extra = []
            for prim in confirmed_prims:
                if not is_thin_plane(prim):
                    continue
                parent = prim.GetParent()
                if not parent or not parent.IsValid():
                    continue
                for sib in parent.GetChildren():
                    if sib.GetTypeName() != "Mesh":
                        continue
                    sib_path = sib.GetPath()
                    if sib_path in confirmed_paths or sib_path in (e.GetPath() for e in extra):
                        continue
                    if is_thin_plane(sib):
                        extra.append(sib)
                        carb.log_info(f"[PlanLift] glass: sibling-thin-plane-detect -> {sib_path}")

            glass_mesh_prims.extend(extra)

            # ── Pass 5: direct GLB parse (no dependency on api_server.py) ───────
            # Read the newest GLB in isaac_tmp, find all materials with
            # alphaMode=BLEND + baseColorFactor[3] < 0.35 (window glass ≈ 0.2,
            # semi-transparent floors ≈ 0.5 are excluded). The GLTF→USD converter
            # names USD materials "materialN" (0-indexed), so index 3 → material3.
            import struct as _struct, json as _json_ext, glob as _gl_ext, os as _os_ext
            _glb_dir = "D:/PlanLift-Backend/output/isaac_tmp"
            _glb_files = sorted(
                _gl_ext.glob(_glb_dir + "/*.glb"),
                key=lambda f: _os_ext.path.getmtime(f),
                reverse=True,
            )
            def _glog(msg):
                """Write to glass_debug.txt AND carb log."""
                carb.log_info(msg)
                try:
                    import datetime
                    with open("D:/PlanLift-Backend/output/isaac_tmp/glass_debug.txt",
                              "a", encoding="utf-8") as _gf:
                        _gf.write(datetime.datetime.now().strftime("%H:%M:%S") + " " + msg + "\n")
                except Exception:
                    pass

            _glog("[PlanLift] glass-v3: _apply_glass_materials started")

            if _glb_files:
                try:
                    _glb_data = open(_glb_files[0], "rb").read()
                    if _glb_data[:4] == b'glTF':
                        _clen = _struct.unpack_from('<I', _glb_data, 12)[0]
                        _gltf = _json_ext.loads(_glb_data[20:20 + _clen])
                        _gindices = []
                        for _gi, _mat in enumerate(_gltf.get('materials', [])):
                            _am   = _mat.get('alphaMode', 'OPAQUE')
                            _pbr  = _mat.get('pbrMetallicRoughness', {})
                            _bc   = _pbr.get('baseColorFactor', [1, 1, 1, 1])
                            _alph = _bc[3] if len(_bc) == 4 else 1.0
                            if _am == 'BLEND' and _alph < 0.35:
                                _gindices.append(_gi)
                        _glog(
                            f"[PlanLift] glass: GLB parse -> {len(_gindices)} glass "
                            f"indices={_gindices} from {_glb_files[0]}"
                        )
                        for _gi in _gindices:
                            _tname = f"material{_gi}"
                            for _p in Usd.PrimRange(stage.GetPseudoRoot()):
                                if _p.GetTypeName() == "Material" and _p.GetName() == _tname:
                                    glass_mat_paths.add(_p.GetPath())
                                    _glog(f"[PlanLift] glass: GLB-idx-{_gi} -> {_p.GetPath()}")
                    else:
                        _glog(f"[PlanLift] glass: not a GLB file: {_glb_files[0]}")
                except Exception as _exc:
                    _glog(f"[PlanLift] glass: GLB parse error: {_exc}")
            else:
                _glog("[PlanLift] glass: no GLB files in isaac_tmp")

            # Fallback: read sidecar JSON written by api_server.py (if present)
            import json as _json_sc, glob as _gl_sc, os as _os_sc
            _sidecars = sorted(
                _gl_sc.glob(_glb_dir + "/*.glass.json"),
                key=lambda f: _os_sc.path.getmtime(f),
                reverse=True,
            )
            if _sidecars and not glass_mat_paths:
                try:
                    _ann = _json_sc.loads(open(_sidecars[0], encoding="utf-8").read())
                    _sc_indices = _ann.get("glass_indices", [])
                    _glog(f"[PlanLift] glass: sidecar fallback indices={_sc_indices}")
                    for _gi in _sc_indices:
                        _tname = f"material{_gi}"
                        for _p in Usd.PrimRange(stage.GetPseudoRoot()):
                            if _p.GetTypeName() == "Material" and _p.GetName() == _tname:
                                glass_mat_paths.add(_p.GetPath())
                                _glog(f"[PlanLift] glass: sidecar-idx-{_gi} -> {_p.GetPath()}")
                except Exception as _exc:
                    _glog(f"[PlanLift] glass: sidecar error: {_exc}")

            # ── Pass 6: flat-plane geometry for /PlanLift_Furniture ───────────
            # Web app exports glass panes as planeGeometry (truly zero depth).
            # Walls use boxGeometry (thickness ≈ 0.1 m). Any Mesh under
            # /PlanLift_Furniture whose smallest bounding-box dimension < 5 mm
            # must be a glass pane — not a wall or floor.
            _furniture = stage.GetPrimAtPath("/PlanLift_Furniture")
            if _furniture and _furniture.IsValid():
                for _fp in Usd.PrimRange(_furniture):
                    if _fp.GetTypeName() != "Mesh":
                        continue
                    try:
                        _sz = bbox_cache.ComputeWorldBound(_fp).GetBox().GetSize()
                        _dims = sorted(abs(_sz[i]) for i in range(3))
                        _glog(
                            f"[PlanLift] furniture-mesh {_fp.GetPath().name} "
                            f"dims=[{_dims[0]:.4f},{_dims[1]:.4f},{_dims[2]:.4f}]"
                        )
                        # Truly flat plane: ratio of thinnest to longest dim < 0.1%.
                        # Scale-independent — works whether model is in m, cm, or mm.
                        # Excludes degenerate/infinite meshes (Mesh277) via the < 1e15 guard.
                        #
                        # Excludes anything nested under a "Frame"-named group
                        # (e.g. ".../Window_with_sliding_door.../Frame1/Cube001_2/
                        # Mesh3") — this whole pass assumes /PlanLift_Furniture
                        # only holds the web app's own simple box/plane
                        # primitives (where thin == glass pane), which breaks
                        # down for a resolved catalog/downloaded asset's real
                        # internal geometry: a window/door frame's thin trim or
                        # backing piece is legitimately thin without being
                        # glass. Confirmed live: a zero-depth, unbound Mesh3
                        # under Frame1 was being converted to glass, making the
                        # whole frame render transparent alongside the actual
                        # pane.
                        _under_frame = any(
                            seg.lower().startswith("frame")
                            for seg in str(_fp.GetPath()).split("/")[:-1]
                        )
                        _is_flat = (_dims[2] > 1e-6
                                    and _dims[0] / _dims[2] < 0.001
                                    and _dims[2] < 1e15
                                    and not _under_frame)
                        if _is_flat:
                            # Only treat as glass if the bound material is transparent
                            # or doesn't exist in the stage (missing material = door glass).
                            # Opaque flat planes (floors, roofs) are bound to existing
                            # opaque materials (alpha_mode=0) and must be skipped.
                            _bound_path = UsdShade.MaterialBindingAPI(_fp).GetDirectBinding().GetMaterialPath()
                            _skip = False
                            if _bound_path and str(_bound_path) != "":
                                if _norm_mat_name(_bound_path.name) in _textured_blend_mat_names_norm:
                                    # Known textured-blend (foliage/fabric) material,
                                    # not architectural glass — see Pass -1 above.
                                    _skip = True
                                else:
                                    _mat_p = stage.GetPrimAtPath(_bound_path)
                                    if _mat_p and _mat_p.IsValid():
                                        for _sh in _mat_p.GetChildren():
                                            _am = _sh.GetAttribute("inputs:alpha_mode")
                                            if _am and _am.IsValid() and _am.Get() is not None:
                                                _skip = (_am.Get() != 2)  # skip if not BLEND
                                                break
                            if not _skip:
                                if _fp not in glass_mesh_prims:
                                    glass_mesh_prims.append(_fp)
                                    _glog(f"[PlanLift] glass: flat-plane -> {_fp.GetPath()}")
                    except Exception as _exc:
                        _glog(f"[PlanLift] glass: furniture bbox {_fp.GetPath()}: {_exc}")

                # Debug: dump attributes of every Shader/Material under /PlanLift_Furniture
                # (up to 15) so we know what the GLTF converter actually created.
                _dumped = 0
                for _dp in Usd.PrimRange(_furniture):
                    if _dp.GetTypeName() not in ("Shader", "Material"):
                        continue
                    if _dumped >= 15:
                        break
                    _attrs = {}
                    for _a in _dp.GetAttributes():
                        try:
                            _attrs[_a.GetName()] = str(_a.Get())
                        except Exception:
                            pass
                    _glog(
                        f"[PlanLift] DEBUG-MAT {_dp.GetTypeName()} "
                        f"{_dp.GetPath()} attrs={_attrs}"
                    )
                    _dumped += 1

            if not glass_mat_paths and not glass_mesh_prims:
                _glog(
                    f"[PlanLift] glass: 0 matches. "
                    f"All materials: {all_mat_names[:40]}"
                )
                return

            _glog(
                f"[PlanLift] glass: {len(glass_mat_paths)} material(s), "
                f"{len(glass_mesh_prims)} mesh(es) to convert"
            )

            # ── Create shared PlanLift_Glass (OmniGlass) material ────────────
            gm_path = Sdf.Path("/World/Looks/PlanLift_Glass")
            gm = UsdShade.Material.Define(stage, gm_path)
            gs = UsdShade.Shader.Define(stage, str(gm_path) + "/Shader")
            gs.SetSourceAsset(Sdf.AssetPath("OmniGlass.mdl"), "mdl")
            gs.SetSourceAssetSubIdentifier("OmniGlass", "mdl")
            gs.CreateInput("glass_color",        Sdf.ValueTypeNames.Color3f).Set((0.9, 0.95, 1.0))
            gs.CreateInput("glass_ior",          Sdf.ValueTypeNames.Float).Set(1.491)
            gs.CreateInput("frosting_roughness", Sdf.ValueTypeNames.Float).Set(0.0)
            gs.CreateInput("thin_walled",        Sdf.ValueTypeNames.Bool).Set(True)
            gs.CreateInput("enable_opacity",     Sdf.ValueTypeNames.Bool).Set(True)
            gs.CreateInput("cutout_opacity",     Sdf.ValueTypeNames.Float).Set(0.5)
            gm.CreateSurfaceOutput("mdl").ConnectToSource(gs.ConnectableAPI(), "out")
            gm.CreateDisplacementOutput("mdl").ConnectToSource(gs.ConnectableAPI(), "out")
            gm.CreateVolumeOutput("mdl").ConnectToSource(gs.ConnectableAPI(), "out")

            _sl = stage.GetSessionLayer()
            glass_mat_names = {_p.name for _p in glass_mat_paths}
            count = 0

            # ── Step 1: Flatten door-glass Xform hierarchy ───────────────────
            # The GLTF→USD converter wraps shared/instanced meshes in an Xform
            # with an embedded Looks/ scope and a same-named inner Mesh child.
            # We detect this pattern structurally (no material heuristics) and
            # flatten every matching Xform under /PlanLift_Furniture:
            #   1. Copy geometry attrs from inner Mesh → outer Xform
            #   2. Promote outer Xform → Mesh, clear instanceable
            #   3. Bind PlanLift_Glass
            #   4. Deactivate inner Mesh + Looks
            # Result: node → MeshN (Mesh, not instanceable) with PlanLift_Glass
            # — identical to the window hierarchy.

            _MESH_COPY_ATTRS = [
                "points", "faceVertexCounts", "faceVertexIndices",
                "normals", "doubleSided", "subdivisionScheme",
                "orientation", "extent",
            ]
            # Collect candidates first (read-only pass) so Traverse isn't
            # invalidated by the edits we make in the second pass.
            _candidates = []
            for _xp in Usd.PrimRange(stage.GetPseudoRoot()):
                if _xp.GetTypeName() != "Xform":
                    continue
                if not str(_xp.GetPath()).startswith("/PlanLift_Furniture"):
                    continue
                # GetChildren() returns proxy prims for instanceable Xforms —
                # that's fine here: we only need names + type, which are correct
                # even on prototypes.
                _inner_name = next(
                    (_c.GetName() for _c in _xp.GetChildren()
                     if _c.GetTypeName() == "Mesh"),
                    None,
                )
                if _inner_name is None:
                    continue
                if not any(_c.GetName() == "Looks" for _c in _xp.GetChildren()):
                    continue
                # "Xform > Looks + one Mesh child" is the web app's own export
                # shape for a glass pane, but it's also just glTF's generic
                # one-node-per-mesh convention — a downloaded/catalog asset's
                # internal single-mesh group (a window frame piece, a door
                # handle sub-mesh) matches the same structural pattern
                # without being glass at all. Confirmed live: this step was
                # converting door handles and door/window frame pieces to
                # glass purely from the structural match, with no check on
                # actual geometry. Require it to also be a truly flat plane
                # (same < 0.1% thinnest/longest ratio Pass 6 below uses)
                # before treating it as a pane.
                _inner_for_bbox = stage.GetPrimAtPath(_xp.GetPath().AppendChild(_inner_name))
                if not (_inner_for_bbox and _inner_for_bbox.IsValid()):
                    continue
                try:
                    _step1_sz = bbox_cache.ComputeWorldBound(_inner_for_bbox).GetBox().GetSize()
                    _step1_dims = sorted(abs(d) for d in _step1_sz)
                    _step1_is_thin = _step1_dims[2] > 1e-6 and _step1_dims[0] / _step1_dims[2] < 0.001
                except Exception:
                    _step1_is_thin = False
                if not _step1_is_thin:
                    continue
                _candidates.append((_xp.GetPath(), _inner_name))

            for (_xp_path, _inner_name) in _candidates:
                _xp = stage.GetPrimAtPath(_xp_path)
                if not (_xp and _xp.IsValid()):
                    continue
                with Usd.EditContext(stage, _sl):
                    # ① Clear instanceable FIRST so USD stops treating the
                    #   children as prototype proxies — after this,
                    #   GetPrimAtPath on the authored child paths is reliable.
                    _xp.SetInstanceable(False)

                    # ② Re-fetch children via authored paths (not proxies)
                    _inner = stage.GetPrimAtPath(_xp_path.AppendChild(_inner_name))
                    _looks  = stage.GetPrimAtPath(_xp_path.AppendChild("Looks"))

                    # ③ Copy geometry from inner Mesh → outer Xform
                    if _inner and _inner.IsValid():
                        for _an in _MESH_COPY_ATTRS:
                            _sa = _inner.GetAttribute(_an)
                            if _sa and _sa.IsValid():
                                _sv = _sa.Get()
                                if _sv is not None:
                                    try:
                                        _xp.CreateAttribute(
                                            _an, _sa.GetTypeName(), custom=False
                                        ).Set(_sv)
                                    except Exception:
                                        pass
                        for _pv in UsdGeom.PrimvarsAPI(_inner).GetPrimvars():
                            try:
                                _pval = _pv.Get()
                                if _pval is None:
                                    continue
                                _npv = UsdGeom.PrimvarsAPI(_xp).CreatePrimvar(
                                    _pv.GetPrimvarName(),
                                    _pv.GetTypeName(),
                                    _pv.GetInterpolation(),
                                )
                                _npv.Set(_pval)
                                if _pv.IsIndexed():
                                    _npv.SetIndices(_pv.GetIndices())
                            except Exception:
                                pass

                    # ④ Promote type, bind PlanLift_Glass, deactivate children
                    _xp.SetTypeName("Mesh")
                    UsdShade.MaterialBindingAPI(_xp).Bind(gm)
                    if _inner and _inner.IsValid():
                        _inner.SetActive(False)
                    if _looks and _looks.IsValid():
                        _looks.SetActive(False)

                count += 1
                _glog(f"[PlanLift] glass: flat Mesh+PlanLift_Glass: {_xp_path}")

            # ── Step 2: Bind PlanLift_Glass to flat glass Mesh prims ─────────
            # Handles windows: flat Mesh prims whose direct binding is a glass material.
            for _p in Usd.PrimRange(stage.GetPseudoRoot()):
                _bp = UsdShade.MaterialBindingAPI(_p).GetDirectBinding().GetMaterialPath()
                if not _bp or str(_bp) == "":
                    continue
                if _bp in glass_mat_paths or _bp.name in glass_mat_names:
                    with Usd.EditContext(stage, _sl):
                        UsdShade.MaterialBindingAPI(_p).Bind(gm)
                    count += 1

            # ── Step 3: Bind PlanLift_Glass to Passes 3/4/6 mesh prims ───────
            for prim in glass_mesh_prims:
                with Usd.EditContext(stage, _sl):
                    UsdShade.MaterialBindingAPI(prim).Bind(gm)
                count += 1

            _glog(f"[PlanLift] glass: PlanLift_Glass on {count} target(s) — DONE")

        except Exception as e:
            carb.log_error(f"[PlanLift] glass: {e}")
            try:
                import datetime
                with open("D:/PlanLift-Backend/output/isaac_tmp/glass_debug.txt",
                          "a", encoding="utf-8") as _ef:
                    _ef.write(datetime.datetime.now().strftime("%H:%M:%S")
                              + f" EXCEPTION: {e}\n")
            except Exception:
                pass

    def _apply_steel_materials(self):
        """
        Detect steel meshes (pillars / roof trusses / pillar tie-beams —
        editor.html marks each with material.name='PlanLift_Steel' and a
        matching mesh.name) two ways:
          1. Direct re-parse of the raw GLB in isaac_tmp: the GLTF->USD
             converter renames every material to material0/material1/...,
             so the trusted name only survives in the GLB's own JSON, not
             the converted stage (same reason _apply_glass_materials'
             Pass 5 re-parses the GLB directly instead of trusting names on
             the imported prims).
          2. Mesh prim name containing "steel" — catches the mesh directly
             even if the material-index match above misses.

        All detected prims get rebound to a single OmniPBR steel material.
        No texture (flat color) — Isaac Sim has a real dome-light
        environment (see _fix_dome_lights), so metallic+roughness alone
        reads convincingly here, unlike the web preview.
        """
        try:
            from pxr import UsdShade, Sdf, Usd
            import omni.usd

            stage = omni.usd.get_context().get_stage()
            if not stage:
                carb.log_warn("[PlanLift] steel: stage not available")
                return

            def _slog(msg):
                carb.log_info(msg)
                try:
                    import datetime
                    with open("D:/PlanLift-Backend/output/isaac_tmp/steel_debug.txt",
                              "a", encoding="utf-8") as _sf:
                        _sf.write(datetime.datetime.now().strftime("%H:%M:%S") + " " + msg + "\n")
                except Exception:
                    pass

            _slog("[PlanLift] _apply_steel_materials called")

            steel_mat_paths = set()
            steel_mesh_prims = []

            # ── Pass A: direct GLB re-parse ─────────────────────────────────
            # Read the newest GLB in isaac_tmp, find every material whose
            # name starts with "PlanLift_Steel", map each GLB material index
            # to the USD material the converter named "material{index}".
            import struct as _struct, json as _json_ext, glob as _gl_ext, os as _os_ext
            _glb_dir = "D:/PlanLift-Backend/output/isaac_tmp"
            _glb_files = sorted(
                _gl_ext.glob(_glb_dir + "/*.glb"),
                key=lambda f: _os_ext.path.getmtime(f),
                reverse=True,
            )
            if _glb_files:
                try:
                    _glb_data = open(_glb_files[0], "rb").read()
                    if _glb_data[:4] == b'glTF':
                        _clen = _struct.unpack_from('<I', _glb_data, 12)[0]
                        _gltf = _json_ext.loads(_glb_data[20:20 + _clen])
                        _sindices = [
                            _gi for _gi, _mat in enumerate(_gltf.get('materials', []))
                            if str(_mat.get('name', '')).startswith('PlanLift_Steel')
                        ]
                        _slog(f"[PlanLift] steel: GLB parse -> {len(_sindices)} steel "
                              f"indices={_sindices} from {_glb_files[0]}")
                        for _si in _sindices:
                            _tname = f"material{_si}"
                            for _p in Usd.PrimRange(stage.GetPseudoRoot()):
                                if _p.GetTypeName() == "Material" and _p.GetName() == _tname:
                                    steel_mat_paths.add(_p.GetPath())
                                    _slog(f"[PlanLift] steel: GLB-idx-{_si} -> {_p.GetPath()}")
                    else:
                        _slog(f"[PlanLift] steel: not a GLB file: {_glb_files[0]}")
                except Exception as _exc:
                    _slog(f"[PlanLift] steel: GLB parse error: {_exc}")
            else:
                _slog("[PlanLift] steel: no GLB files in isaac_tmp")

            # ── Pass B: mesh-name keyword match ──────────────────────────────
            for prim in Usd.PrimRange(stage.GetPseudoRoot()):
                if prim.GetTypeName() != "Mesh":
                    continue
                if "steel" in prim.GetName().lower():
                    steel_mesh_prims.append(prim)
                    _slog(f"[PlanLift] steel: mesh-name-detect -> {prim.GetPath()}")

            if not steel_mat_paths and not steel_mesh_prims:
                _slog("[PlanLift] steel: 0 matches")
                return

            # ── Shared OmniPBR steel material ────────────────────────────────
            sm_path = Sdf.Path("/World/Looks/PlanLift_Steel")
            sm = UsdShade.Material.Define(stage, sm_path)
            ss = UsdShade.Shader.Define(stage, str(sm_path) + "/Shader")
            ss.SetSourceAsset(Sdf.AssetPath("OmniPBR.mdl"), "mdl")
            ss.SetSourceAssetSubIdentifier("OmniPBR", "mdl")
            ss.CreateInput("diffuse_color_constant",      Sdf.ValueTypeNames.Color3f).Set((0.32, 0.37, 0.44))
            ss.CreateInput("metallic_constant",            Sdf.ValueTypeNames.Float).Set(0.7)
            ss.CreateInput("reflection_roughness_constant", Sdf.ValueTypeNames.Float).Set(0.45)
            sm.CreateSurfaceOutput("mdl").ConnectToSource(ss.ConnectableAPI(), "out")
            sm.CreateDisplacementOutput("mdl").ConnectToSource(ss.ConnectableAPI(), "out")
            sm.CreateVolumeOutput("mdl").ConnectToSource(ss.ConnectableAPI(), "out")

            _sl = stage.GetSessionLayer()
            steel_mat_names = {p.name for p in steel_mat_paths}
            count = 0

            for prim in Usd.PrimRange(stage.GetPseudoRoot()):
                bp = UsdShade.MaterialBindingAPI(prim).GetDirectBinding().GetMaterialPath()
                if bp and str(bp) != "" and (bp in steel_mat_paths or bp.name in steel_mat_names):
                    with Usd.EditContext(stage, _sl):
                        UsdShade.MaterialBindingAPI(prim).Bind(sm)
                    count += 1

            for prim in steel_mesh_prims:
                with Usd.EditContext(stage, _sl):
                    UsdShade.MaterialBindingAPI(prim).Bind(sm)
                count += 1

            _slog(f"[PlanLift] steel: PlanLift_Steel on {count} target(s) — DONE")

        except Exception as e:
            carb.log_error(f"[PlanLift] steel: {e}")
            try:
                import datetime
                with open("D:/PlanLift-Backend/output/isaac_tmp/steel_debug.txt",
                          "a", encoding="utf-8") as _ef:
                    _ef.write(datetime.datetime.now().strftime("%H:%M:%S")
                              + f" EXCEPTION: {e}\n")
            except Exception:
                pass

    def _apply_water_materials(self):
        """
        Detect Pond water meshes (editor.html marks the water surface with
        material.name='PlanLift_Water' and mesh.name='PlanLift_Water_Pond')
        the same two ways _apply_steel_materials uses — GLB-index re-parse
        (the GLTF->USD converter renames every material to material0/
        material1/..., so the trusted name only survives in the GLB's own
        JSON) plus a mesh-name keyword fallback.

        Rebinds every match to a single shared OmniGlass material (the same
        shader window/wall glass already uses via _apply_glass_materials,
        just re-tinted and re-tuned) instead of OmniPBR: water is a real
        refractive/transparent surface, and the web preview's Three.js
        MeshPhysicalMaterial approximation of that doesn't survive the
        GLB→USD conversion — confirmed live: without this pass the pond
        rendered as flat khaki/olive instead of any kind of water. glass_ior
        uses water's real 1.33 (not glass's 1.5) so refraction still looks
        physically right even though the shader is OmniGlass.
        """
        try:
            from pxr import UsdShade, Sdf, Usd
            import omni.usd

            stage = omni.usd.get_context().get_stage()
            if not stage:
                carb.log_warn("[PlanLift] water: stage not available")
                return

            def _wlog(msg):
                carb.log_info(msg)
                try:
                    import datetime
                    with open("D:/PlanLift-Backend/output/isaac_tmp/water_debug.txt",
                              "a", encoding="utf-8") as _wf:
                        _wf.write(datetime.datetime.now().strftime("%H:%M:%S") + " " + msg + "\n")
                except Exception:
                    pass

            _wlog("[PlanLift] _apply_water_materials called")

            water_mat_paths = set()
            water_mesh_prims = []

            # ── Pass 0: trusted exact name from the web app ─────────────────
            # Confirmed live via glass_debug.txt's DEBUG-MAT dump: for THIS
            # material the GLTF->USD converter did NOT rename it to
            # "materialN" (unlike its neighbours material0/material2/...) —
            # it kept the original glTF name and the USD Material prim is
            # literally named "PlanLift_Water". Pass A's "material{index}"
            # guess below only covers the OTHER naming behaviour, so on its
            # own it silently found 0 matches for exactly this reason (the
            # pond kept showing raw ground/soil instead of any water look at
            # all). Same pattern _apply_glass_materials' own Pass 0 already
            # relies on for PlanLift_Glass.
            for _p in Usd.PrimRange(stage.GetPseudoRoot()):
                if _p.GetTypeName() == "Material" and _p.GetName().startswith("PlanLift_Water"):
                    water_mat_paths.add(_p.GetPath())
                    _wlog(f"[PlanLift] water: Pass0 exact-name -> {_p.GetPath()}")

            # ── Pass A: direct GLB re-parse ─────────────────────────────────
            import struct as _struct, json as _json_ext, glob as _gl_ext, os as _os_ext
            _glb_dir = "D:/PlanLift-Backend/output/isaac_tmp"
            _glb_files = sorted(
                _gl_ext.glob(_glb_dir + "/*.glb"),
                key=lambda f: _os_ext.path.getmtime(f),
                reverse=True,
            )
            if _glb_files:
                try:
                    _glb_data = open(_glb_files[0], "rb").read()
                    if _glb_data[:4] == b'glTF':
                        _clen = _struct.unpack_from('<I', _glb_data, 12)[0]
                        _gltf = _json_ext.loads(_glb_data[20:20 + _clen])
                        _windices = [
                            _gi for _gi, _mat in enumerate(_gltf.get('materials', []))
                            if str(_mat.get('name', '')).startswith('PlanLift_Water')
                        ]
                        _wlog(f"[PlanLift] water: GLB parse -> {len(_windices)} water "
                              f"indices={_windices} from {_glb_files[0]}")
                        for _wi in _windices:
                            _tname = f"material{_wi}"
                            for _p in Usd.PrimRange(stage.GetPseudoRoot()):
                                if _p.GetTypeName() == "Material" and _p.GetName() == _tname:
                                    water_mat_paths.add(_p.GetPath())
                                    _wlog(f"[PlanLift] water: GLB-idx-{_wi} -> {_p.GetPath()}")
                    else:
                        _wlog(f"[PlanLift] water: not a GLB file: {_glb_files[0]}")
                except Exception as _exc:
                    _wlog(f"[PlanLift] water: GLB parse error: {_exc}")
            else:
                _wlog("[PlanLift] water: no GLB files in isaac_tmp")

            # ── Pass B: mesh PATH keyword match (not just its own name) ──────
            # editor.html names the pond mesh 'PlanLift_Water_Pond', but the
            # GLTF->USD converter wraps a flat single-plane mesh (confirmed
            # live: glass's own "flat-plane" pass logged
            # ".../PlanLift_Water_Pond/Mesh1") in an Xform carrying that
            # name, with the actual typeName=="Mesh" prim underneath
            # genericly named "MeshN" — so checking prim.GetName() alone
            # (only the leaf) never matches the real Mesh prim. Checking the
            # full path instead catches the name wherever it sits among the
            # prim's ancestors.
            #
            # This pass runs unconditionally (no current-binding check) and
            # LAST in the pipeline (after _apply_glass_materials), which
            # matters: that same flat-plane heuristic in glass isn't scoped
            # to material name, so it can — and, confirmed live, does — grab
            # this exact mesh first and bind it to PlanLift_Glass. Finding
            # it by path and rebinding here overrides that regardless of
            # whatever it's currently bound to.
            for prim in Usd.PrimRange(stage.GetPseudoRoot()):
                if prim.GetTypeName() != "Mesh":
                    continue
                if "water" in str(prim.GetPath()).lower():
                    water_mesh_prims.append(prim)
                    _wlog(f"[PlanLift] water: mesh-path-detect -> {prim.GetPath()}")

            if not water_mat_paths and not water_mesh_prims:
                _wlog("[PlanLift] water: 0 matches")
                return

            # ── Shared OmniGlass pool/sea water material ─────────────────────
            # Real NVIDIA vMaterials2 "Water_Ocean_Blue_Reef_01" is not
            # reachable from this install (no Nucleus server configured, and
            # it's not in the public omniverse-content-production "Base"
            # pack the built-in Material Browser points at — confirmed live
            # via direct requests) — so this tunes the same OmniGlass.mdl
            # already bundled locally (kit/mdl/core/Base/OmniGlass.mdl,
            # confirmed by reading it) to approximate that tropical-reef
            # turquoise look using every relevant exposed parameter, not
            # just glass_color/opacity.
            wm_path = Sdf.Path("/World/Looks/PlanLift_Water")
            wm = UsdShade.Material.Define(stage, wm_path)
            ws = UsdShade.Shader.Define(stage, str(wm_path) + "/Shader")
            ws.SetSourceAsset(Sdf.AssetPath("OmniGlass.mdl"), "mdl")
            ws.SetSourceAssetSubIdentifier("OmniGlass", "mdl")
            ws.CreateInput("glass_color",        Sdf.ValueTypeNames.Color3f).Set((0.02, 0.42, 0.5))   # richer reef turquoise
            ws.CreateInput("glass_ior",          Sdf.ValueTypeNames.Float).Set(1.33)    # water's real IOR, not glass's 1.491
            ws.CreateInput("frosting_roughness", Sdf.ValueTypeNames.Float).Set(0.035)   # near-still water, not a perfect mirror
            ws.CreateInput("thin_walled",        Sdf.ValueTypeNames.Bool).Set(True)     # matches the flat single-surface pond mesh
            ws.CreateInput("depth",              Sdf.ValueTypeNames.Float).Set(0.25)    # Volume Absorption Scale — richer color falloff
            ws.CreateInput("reflection_color",   Sdf.ValueTypeNames.Color3f).Set((0.72, 0.9, 0.94))  # cool sky-blue tint on highlights, not neutral-white
            ws.CreateInput("enable_opacity",     Sdf.ValueTypeNames.Bool).Set(True)
            ws.CreateInput("cutout_opacity",     Sdf.ValueTypeNames.Float).Set(0.85)    # denser/more tinted than clear window glass
            wm.CreateSurfaceOutput("mdl").ConnectToSource(ws.ConnectableAPI(), "out")
            wm.CreateDisplacementOutput("mdl").ConnectToSource(ws.ConnectableAPI(), "out")
            wm.CreateVolumeOutput("mdl").ConnectToSource(ws.ConnectableAPI(), "out")

            _sl = stage.GetSessionLayer()
            water_mat_names = {p.name for p in water_mat_paths}
            count = 0

            for prim in Usd.PrimRange(stage.GetPseudoRoot()):
                bp = UsdShade.MaterialBindingAPI(prim).GetDirectBinding().GetMaterialPath()
                if bp and str(bp) != "" and (bp in water_mat_paths or bp.name in water_mat_names):
                    with Usd.EditContext(stage, _sl):
                        UsdShade.MaterialBindingAPI(prim).Bind(wm)
                    count += 1

            for prim in water_mesh_prims:
                with Usd.EditContext(stage, _sl):
                    UsdShade.MaterialBindingAPI(prim).Bind(wm)
                count += 1

            _wlog(f"[PlanLift] water: PlanLift_Water on {count} target(s) — DONE")

        except Exception as e:
            carb.log_error(f"[PlanLift] water: {e}")
            try:
                import datetime
                with open("D:/PlanLift-Backend/output/isaac_tmp/water_debug.txt",
                          "a", encoding="utf-8") as _ef:
                    _ef.write(datetime.datetime.now().strftime("%H:%M:%S")
                              + f" EXCEPTION: {e}\n")
            except Exception:
                pass
