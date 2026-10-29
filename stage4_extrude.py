"""
Stage 4 — 3D Extrusion & Mesh Generation
Reads the scene graph (JSON) from Stage 3 (optionally enriched by Stage LLM or
Stage 2b) and produces a watertight 3D mesh:
  - Walls extruded to ceiling height
  - Floor slab + per-room colour patches
  - Ceiling slab
  - Door cutouts (floor-to-door-height) with door leaf panels
  - Window cutouts (sill-to-sill+window-height) with glass panes
  - Stairs (cumulative stepped boxes)
  - Fixtures: toilet, shower, bathtub, sink, kitchen_sink,
              refrigerator, oven, range, dishwasher, washer, dryer, water_heater
  - Furniture: bed, double_bed, sofa, chair, armchair,
               dining_table, desk, bookshelf, wardrobe, table

Outputs (saved to output/models/):
  <stem>.glb   — GLTF Binary (drag into any web viewer or Blender)
  <stem>.obj   — Wavefront OBJ (Blender, MeshLab, etc.)
"""

import argparse
import json
import sys
from pathlib import Path
from typing import List, Optional

import numpy as np
import trimesh
from shapely.geometry import Polygon
from shapely.validation import make_valid

# ── Architectural constants ───────────────────────────────────────────────────
DEFAULT_WALL_HEIGHT    = 2.5    # metres
DEFAULT_FLOOR_THICK    = 0.15
DEFAULT_CEIL_THICK     = 0.10
DEFAULT_DOOR_HEIGHT    = 2.1
DEFAULT_WINDOW_HEIGHT  = 1.2
DEFAULT_WINDOW_SILL    = 0.9
DOOR_PANEL_THICK       = 0.04   # thickness of the 3D door leaf

# ── Materials (RGBA 0-255) ────────────────────────────────────────────────────
MAT_WALL          = [220, 220, 220, 255]
MAT_FLOOR         = [180, 160, 130, 255]
MAT_CEILING       = [245, 245, 245, 255]
MAT_DOOR          = [160, 110,  60, 255]   # warm wood brown
MAT_WINDOW_GLASS  = [173, 216, 230, 220]   # light blue
MAT_STAIR         = [200, 190, 170, 255]   # concrete / pale wood
MAT_CERAMIC       = [245, 245, 245, 255]   # toilet / sink / tub
MAT_SHOWER        = [200, 240, 255, 255]   # shower tray / stall
MAT_APPLIANCE     = [190, 190, 190, 255]   # fridge, oven, dishwasher
MAT_LAUNDRY       = [ 60,  80, 120, 255]   # washer / dryer (dark blue-grey)
MAT_WATER_HEATER  = [160, 160, 160, 255]
MAT_FURNITURE_SOFT= [160, 120, 100, 255]   # bed / sofa / chair
MAT_FURNITURE_HARD= [150, 110,  55, 255]   # table / desk / bookshelf

ROOM_COLORS_3D = [
    [200, 230, 255], [255, 230, 180], [200, 255, 200],
    [255, 200, 220], [220, 220, 255], [255, 255, 200],
]

# ── Standard symbol heights (metres) ─────────────────────────────────────────
FIXTURE_HEIGHT = {
    "toilet":        0.40,
    "shower":        2.10,
    "bathtub":       0.55,
    "sink":          0.85,
    "kitchen_sink":  0.90,
    "refrigerator":  1.80,
    "oven":          0.90,
    "range":         0.90,
    "dishwasher":    0.85,
    "washer":        0.90,
    "dryer":         0.90,
    "water_heater":  1.20,
}

FURNITURE_HEIGHT = {
    "bed":           0.50,
    "double_bed":    0.55,
    "sofa":          0.80,
    "chair":         0.85,
    "armchair":      0.85,
    "dining_table":  0.75,
    "desk":          0.75,
    "bookshelf":     2.00,
    "wardrobe":      2.20,
    "table":         0.75,
}

FIXTURE_COLOR = {
    "toilet":       MAT_CERAMIC,
    "shower":       MAT_SHOWER,
    "bathtub":      MAT_CERAMIC,
    "sink":         MAT_CERAMIC,
    "kitchen_sink": MAT_CERAMIC,
    "refrigerator": MAT_APPLIANCE,
    "oven":         MAT_APPLIANCE,
    "range":        MAT_APPLIANCE,
    "dishwasher":   MAT_APPLIANCE,
    "washer":       MAT_LAUNDRY,
    "dryer":        MAT_LAUNDRY,
    "water_heater": MAT_WATER_HEATER,
}

FURNITURE_COLOR = {
    "bed":          MAT_FURNITURE_SOFT,
    "double_bed":   MAT_FURNITURE_SOFT,
    "sofa":         MAT_FURNITURE_SOFT,
    "chair":        MAT_FURNITURE_SOFT,
    "armchair":     MAT_FURNITURE_SOFT,
    "dining_table": MAT_FURNITURE_HARD,
    "desk":         MAT_FURNITURE_HARD,
    "bookshelf":    MAT_FURNITURE_HARD,
    "wardrobe":     MAT_FURNITURE_HARD,
    "table":        MAT_FURNITURE_HARD,
}


# ── Geometry helpers ──────────────────────────────────────────────────────────

def polygon_to_trimesh(shapely_poly: Polygon, height: float,
                       z_base: float = 0.0) -> Optional[trimesh.Trimesh]:
    shapely_poly = make_valid(shapely_poly)
    if shapely_poly.is_empty or shapely_poly.area < 1e-6:
        return None
    try:
        mesh = trimesh.creation.extrude_polygon(shapely_poly, height)
        mesh.apply_translation([0, 0, z_base])
        return mesh
    except Exception:
        return None


def box_cutout(x: float, y: float, w: float, h: float,
               z_base: float, z_top: float) -> trimesh.Trimesh:
    box = trimesh.creation.box(extents=[w, h, z_top - z_base])
    box.apply_translation([x + w / 2, y + h / 2, z_base + (z_top - z_base) / 2])
    return box


def px_to_m(coords, scale: float, img_height: float):
    return [(c[0] * scale, (img_height - c[1]) * scale) for c in coords]


def _bbox_world(bbox: list, scale: float, img_h: float):
    """Pixel [x,y,w,h] → world (x_m, y_m, w_m, d_m) with Y-axis flip."""
    x_m = bbox[0] * scale
    y_m = (img_h - bbox[1] - bbox[3]) * scale
    w_m = bbox[2] * scale
    d_m = bbox[3] * scale
    return x_m, y_m, w_m, d_m


def _symbol_box(bbox: list, height: float, color: list,
                scale: float, img_h: float,
                z_base: float = 0.0) -> Optional[trimesh.Trimesh]:
    """Build a coloured box mesh from a pixel bbox."""
    x_m, y_m, w_m, d_m = _bbox_world(bbox, scale, img_h)
    if w_m < 0.04 or d_m < 0.04:
        return None
    box = trimesh.creation.box(extents=[w_m, d_m, height])
    box.apply_translation([x_m + w_m / 2, y_m + d_m / 2, z_base + height / 2])
    box.visual.face_colors = color
    return box


# ── Floor / ceiling ───────────────────────────────────────────────────────────

def _wall_bounds_m(scene: dict) -> tuple:
    meta  = scene["meta"]
    scale = meta["scale_m_per_px"]
    img_h = meta["height_px"]
    xs, ys = [], []
    for wall in scene.get("walls", []):
        for px_coord, py_coord in wall["polygon"]:
            xs.append(px_coord * scale)
            ys.append((img_h - py_coord) * scale)
    if not xs:
        return None, None, None, None
    return min(xs), min(ys), max(xs), max(ys)


def _floor_rect(scene: dict) -> tuple:
    meta       = scene["meta"]
    scale      = meta["scale_m_per_px"]
    half_thick = meta.get("wall_thickness_px", 6) * scale / 2
    x0, y0, x1, y1 = _wall_bounds_m(scene)
    bw = meta.get("building_width_m")
    bh = meta.get("building_height_m")
    if x0 is not None and bw and bh:
        cx = (x0 + x1) / 2;  cy = (y0 + y1) / 2
        return cx - bw/2, cy - bh/2, cx + bw/2, cy + bh/2
    elif x0 is not None:
        return x0 - half_thick, y0 - half_thick, x1 + half_thick, y1 + half_thick
    else:
        return 0, 0, meta["width_px"] * scale, meta["height_px"] * scale


def build_floor(scene: dict) -> trimesh.Trimesh:
    x0, y0, x1, y1 = _floor_rect(scene)
    mesh = trimesh.creation.extrude_polygon(
        Polygon([(x0,y0),(x1,y0),(x1,y1),(x0,y1)]), DEFAULT_FLOOR_THICK)
    mesh.apply_translation([0, 0, -DEFAULT_FLOOR_THICK])
    mesh.visual.face_colors = MAT_FLOOR
    return mesh


def build_ceiling(scene: dict) -> trimesh.Trimesh:
    x0, y0, x1, y1 = _floor_rect(scene)
    mesh = trimesh.creation.extrude_polygon(
        Polygon([(x0,y0),(x1,y0),(x1,y1),(x0,y1)]), DEFAULT_CEIL_THICK)
    mesh.apply_translation([0, 0, DEFAULT_WALL_HEIGHT])
    mesh.visual.face_colors = MAT_CEILING
    return mesh


# ── Walls ─────────────────────────────────────────────────────────────────────

def build_walls(scene: dict, wall_height: float) -> List[trimesh.Trimesh]:
    meta  = scene["meta"]
    scale = meta["scale_m_per_px"]
    img_h = meta["height_px"]
    meshes = []
    for wall in scene.get("walls", []):
        pts_m   = px_to_m(wall["polygon"], scale, img_h)
        holes_m = [px_to_m(h, scale, img_h) for h in wall.get("holes", [])]
        if len(pts_m) < 4:
            continue
        try:
            poly = make_valid(Polygon(pts_m, holes_m))
        except Exception:
            continue
        if poly.is_empty or poly.area < 1e-4:
            continue
        mesh = polygon_to_trimesh(poly, wall_height)
        if mesh is not None:
            mesh.visual.face_colors = MAT_WALL
            meshes.append(mesh)
    return meshes


# ── Opening cutouts (door and window heights differ) ─────────────────────────

def apply_opening_cutouts(wall_meshes: List[trimesh.Trimesh], scene: dict,
                          wall_height: float) -> List[trimesh.Trimesh]:
    """
    Door openings: cut from floor (z=0) to DEFAULT_DOOR_HEIGHT.
    Window openings: cut from DEFAULT_WINDOW_SILL to sill + DEFAULT_WINDOW_HEIGHT.
    Falls back to door height when type is unknown.
    """
    meta  = scene["meta"]
    scale = meta["scale_m_per_px"]
    img_h = meta["height_px"]

    cutters = []
    for op in scene.get("openings", []):
        x_m = op["x"] * scale
        y_m = (img_h - op["y"] - op["h"]) * scale
        w_m = op["w"] * scale
        h_m = op["h"] * scale

        if op.get("type") == "window":
            cutter = box_cutout(x_m, y_m, w_m, h_m,
                                DEFAULT_WINDOW_SILL,
                                DEFAULT_WINDOW_SILL + DEFAULT_WINDOW_HEIGHT)
        else:
            cutter = box_cutout(x_m, y_m, w_m, h_m, 0.0, DEFAULT_DOOR_HEIGHT)
        cutters.append(cutter)

    if not cutters or not wall_meshes:
        return wall_meshes

    result_walls = []
    for wm in wall_meshes:
        try:
            merged_cutter = trimesh.boolean.union(cutters)
            cut = trimesh.boolean.difference([wm, merged_cutter])
            if isinstance(cut, trimesh.Trimesh) and not cut.is_empty:
                cut.visual.face_colors = MAT_WALL
                result_walls.append(cut)
            else:
                result_walls.append(wm)
        except Exception:
            result_walls.append(wm)
    return result_walls


# ── Room floor patches ────────────────────────────────────────────────────────

def build_room_floors(scene: dict) -> List[trimesh.Trimesh]:
    meta  = scene["meta"]
    scale = meta["scale_m_per_px"]
    img_h = meta["height_px"]
    meshes = []
    for i, room in enumerate(scene.get("rooms", [])):
        color = ROOM_COLORS_3D[i % len(ROOM_COLORS_3D)] + [255]
        x, y, w, h = room["bbox"]
        x_m, y_m = x * scale, (img_h - y - h) * scale
        w_m, h_m = w * scale, h * scale
        poly = Polygon([(x_m,y_m),(x_m+w_m,y_m),(x_m+w_m,y_m+h_m),(x_m,y_m+h_m)])
        if poly.is_empty or poly.area < 1e-4:
            continue
        mesh = polygon_to_trimesh(poly, 0.01)
        if mesh is not None:
            mesh.visual.face_colors = color
            meshes.append(mesh)
    return meshes


# ── Door leaf panels ──────────────────────────────────────────────────────────

def build_door_panels(scene: dict) -> List[trimesh.Trimesh]:
    """
    Place a thin door-leaf box at each door opening.
    For swing doors the panel is centred in the opening.
    For sliding doors two half-width panels are placed side by side.
    """
    meta  = scene["meta"]
    scale = meta["scale_m_per_px"]
    img_h = meta["height_px"]
    meshes = []

    for op in scene.get("openings", []):
        if op.get("type") == "window":
            continue
        x_m = op["x"] * scale
        y_m = (img_h - op["y"] - op["h"]) * scale
        w_m = op["w"] * scale
        d_m = op["h"] * scale   # wall depth
        door_w = max(w_m, d_m)  # panel spans the wider dimension
        door_d = min(w_m, d_m)  # thin axis = wall depth

        door_type = op.get("door_type", "swing")

        if door_type == "sliding":
            # Two half-width panels
            hw = door_w / 2
            for offset in (0.0, hw / 2):
                if w_m >= d_m:
                    panel = trimesh.creation.box([hw, DOOR_PANEL_THICK, DEFAULT_DOOR_HEIGHT])
                    panel.apply_translation([x_m + offset + hw/2,
                                             y_m + door_d/2,
                                             DEFAULT_DOOR_HEIGHT / 2])
                else:
                    panel = trimesh.creation.box([DOOR_PANEL_THICK, hw, DEFAULT_DOOR_HEIGHT])
                    panel.apply_translation([x_m + door_d/2,
                                             y_m + offset + hw/2,
                                             DEFAULT_DOOR_HEIGHT / 2])
                panel.visual.face_colors = MAT_DOOR
                meshes.append(panel)
        else:
            # Single panel centred in opening
            if w_m >= d_m:
                panel = trimesh.creation.box([door_w, DOOR_PANEL_THICK, DEFAULT_DOOR_HEIGHT])
                panel.apply_translation([x_m + door_w/2, y_m + door_d/2, DEFAULT_DOOR_HEIGHT/2])
            else:
                panel = trimesh.creation.box([DOOR_PANEL_THICK, door_w, DEFAULT_DOOR_HEIGHT])
                panel.apply_translation([x_m + door_d/2, y_m + door_w/2, DEFAULT_DOOR_HEIGHT/2])
            panel.visual.face_colors = MAT_DOOR
            meshes.append(panel)

    return meshes


# ── Window glass panes ────────────────────────────────────────────────────────

def build_window_panes(scene: dict) -> List[trimesh.Trimesh]:
    """Thin glass pane centred at each window opening."""
    meta  = scene["meta"]
    scale = meta["scale_m_per_px"]
    img_h = meta["height_px"]
    meshes = []

    for op in scene.get("openings", []):
        if op.get("type") != "window":
            continue
        x_m = op["x"] * scale
        y_m = (img_h - op["y"] - op["h"]) * scale
        w_m = op["w"] * scale
        d_m = op["h"] * scale
        glass_thick = 0.02

        if w_m >= d_m:
            glass = trimesh.creation.box([w_m, glass_thick, DEFAULT_WINDOW_HEIGHT])
            glass.apply_translation([x_m + w_m/2, y_m + d_m/2,
                                      DEFAULT_WINDOW_SILL + DEFAULT_WINDOW_HEIGHT/2])
        else:
            glass = trimesh.creation.box([glass_thick, d_m, DEFAULT_WINDOW_HEIGHT])
            glass.apply_translation([x_m + w_m/2, y_m + d_m/2,
                                      DEFAULT_WINDOW_SILL + DEFAULT_WINDOW_HEIGHT/2])
        glass.visual.face_colors = MAT_WINDOW_GLASS
        meshes.append(glass)

    return meshes


# ── Stairs ────────────────────────────────────────────────────────────────────

def build_stairs(scene: dict, wall_height: float) -> List[trimesh.Trimesh]:
    """
    Stacked-box staircase: each tread is a box from z=0 to its cumulative height.
    Running along the stair's longer dimension.
    """
    symbols = scene.get("symbols", {})
    stairs_list = symbols.get("stairs", [])
    if not stairs_list:
        return []

    meta  = scene["meta"]
    scale = meta["scale_m_per_px"]
    img_h = meta["height_px"]
    meshes = []

    for stair in stairs_list:
        bbox = stair.get("bbox")
        if not bbox:
            continue
        x_m, y_m, w_m, d_m = _bbox_world(bbox, scale, img_h)
        if w_m < 0.1 or d_m < 0.1:
            continue

        tread_count = max(int(stair.get("tread_count", 4)), 2)
        step_h      = wall_height / tread_count
        running_y   = d_m >= w_m
        tread_dim   = (d_m if running_y else w_m) / tread_count

        for i in range(tread_count):
            cum_h = (i + 1) * step_h
            if running_y:
                box = trimesh.creation.box([w_m, tread_dim, cum_h])
                box.apply_translation([x_m + w_m/2,
                                       y_m + tread_dim * (i + 0.5),
                                       cum_h / 2])
            else:
                box = trimesh.creation.box([tread_dim, d_m, cum_h])
                box.apply_translation([x_m + tread_dim * (i + 0.5),
                                       y_m + d_m / 2,
                                       cum_h / 2])
            box.visual.face_colors = MAT_STAIR
            meshes.append(box)

    return meshes


# ── Fixtures ──────────────────────────────────────────────────────────────────

def build_fixtures(scene: dict) -> List[trimesh.Trimesh]:
    symbols  = scene.get("symbols", {})
    fixtures = symbols.get("fixtures", [])
    if not fixtures:
        return []

    meta  = scene["meta"]
    scale = meta["scale_m_per_px"]
    img_h = meta["height_px"]
    meshes = []

    for sym in fixtures:
        ftype = sym.get("type", "")
        bbox  = sym.get("bbox")
        if not bbox:
            continue
        height = FIXTURE_HEIGHT.get(ftype, 0.80)
        color  = FIXTURE_COLOR.get(ftype, MAT_CERAMIC)
        mesh   = _symbol_box(bbox, height, color, scale, img_h)
        if mesh is not None:
            meshes.append(mesh)

    return meshes


# ── Furniture ─────────────────────────────────────────────────────────────────

def build_furniture(scene: dict) -> List[trimesh.Trimesh]:
    symbols   = scene.get("symbols", {})
    furniture = symbols.get("furniture", [])
    if not furniture:
        return []

    meta  = scene["meta"]
    scale = meta["scale_m_per_px"]
    img_h = meta["height_px"]
    meshes = []

    for sym in furniture:
        ftype = sym.get("type", "")
        bbox  = sym.get("bbox")
        if not bbox:
            continue
        height = FURNITURE_HEIGHT.get(ftype, 0.75)
        color  = FURNITURE_COLOR.get(ftype, MAT_FURNITURE_HARD)
        mesh   = _symbol_box(bbox, height, color, scale, img_h)
        if mesh is not None:
            meshes.append(mesh)

    return meshes


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Stage 4: Extrude floor plan to 3D mesh")
    parser.add_argument("input", help="Path to <stem>_scene.json from Stage 3")
    parser.add_argument("--output-dir", default="output/models")
    parser.add_argument("--wall-height", type=float, default=DEFAULT_WALL_HEIGHT)
    parser.add_argument("--no-cutouts",  action="store_true",
                        help="Skip door/window boolean cutouts (faster)")
    parser.add_argument("--no-symbols",  action="store_true",
                        help="Skip 3D symbol placement (stairs, fixtures, furniture)")
    args = parser.parse_args()

    json_path = Path(args.input)
    if not json_path.exists():
        print(f"[ERROR] File not found: {json_path}")
        sys.exit(1)

    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = json_path.stem.replace("_scene", "")

    with open(json_path) as f:
        scene = json.load(f)

    print(f"[Stage 4] Building 3D model from: {json_path}")

    all_meshes: List[trimesh.Trimesh] = []

    # ── Structural ────────────────────────────────────────────────────────────
    print("  Extruding floor slab...")
    all_meshes.append(build_floor(scene))

    print(f"  Adding {len(scene.get('rooms', []))} room floor patches...")
    all_meshes.extend(build_room_floors(scene))

    print(f"  Extruding {len(scene.get('walls', []))} wall polygon(s) "
          f"to {args.wall_height} m...")
    wall_meshes = build_walls(scene, args.wall_height)

    openings = scene.get("openings", [])
    if not args.no_cutouts and openings:
        n_doors   = sum(1 for o in openings if o.get("type") != "window")
        n_windows = sum(1 for o in openings if o.get("type") == "window")
        print(f"  Applying cutouts: {n_doors} door(s), {n_windows} window(s)...")
        wall_meshes = apply_opening_cutouts(wall_meshes, scene, args.wall_height)

    all_meshes.extend(wall_meshes)

    print("  Adding ceiling slab...")
    all_meshes.append(build_ceiling(scene))

    # ── Openings: leaf panels + glass ─────────────────────────────────────────
    if not args.no_cutouts:
        door_panels = build_door_panels(scene)
        win_panes   = build_window_panes(scene)
        if door_panels:
            print(f"  Door leaf panels:  {len(door_panels)}")
            all_meshes.extend(door_panels)
        if win_panes:
            print(f"  Window glass panes: {len(win_panes)}")
            all_meshes.extend(win_panes)

    # ── Symbols: stairs / fixtures / furniture ────────────────────────────────
    if not args.no_symbols:
        symbols = scene.get("symbols", {})
        if symbols:
            stair_meshes = build_stairs(scene, args.wall_height)
            fix_meshes   = build_fixtures(scene)
            furn_meshes  = build_furniture(scene)
            if stair_meshes:
                print(f"  Stair geometry:    {len(stair_meshes)} tread(s)")
                all_meshes.extend(stair_meshes)
            if fix_meshes:
                print(f"  Fixtures:          {len(fix_meshes)}")
                all_meshes.extend(fix_meshes)
            if furn_meshes:
                print(f"  Furniture:         {len(furn_meshes)}")
                all_meshes.extend(furn_meshes)
        else:
            print("  No symbol data — run Stage LLM or Stage 2b to add symbols.")

    # ── Merge & export ────────────────────────────────────────────────────────
    print("  Merging geometry...")
    scene3d = trimesh.Scene()
    for i, mesh in enumerate(all_meshes):
        if mesh is not None and not mesh.is_empty:
            scene3d.add_geometry(mesh, node_name=f"mesh_{i}")

    # Z-up → Y-up for GLTF / Blender standard
    rot = np.array([
        [1,  0,  0,  0],
        [0,  0,  1,  0],
        [0, -1,  0,  0],
        [0,  0,  0,  1],
    ], dtype=float)
    scene3d.apply_transform(rot)

    glb_path = out_dir / f"{stem}.glb"
    obj_path = out_dir / f"{stem}.obj"

    print(f"  Exporting GLB  -> {glb_path}")
    with open(glb_path, "wb") as f:
        f.write(scene3d.export(file_type="glb"))

    print(f"  Exporting OBJ  -> {obj_path}")
    obj_data = scene3d.export(file_type="obj")
    if isinstance(obj_data, dict):
        for fname, data in obj_data.items():
            out = out_dir / fname
            mode = "w" if isinstance(data, str) else "wb"
            with open(out, mode) as f:
                f.write(data)
    else:
        mode = "w" if isinstance(obj_data, str) else "wb"
        with open(obj_path, mode) as f:
            f.write(obj_data)

    print(f"\n[Stage 4] Done! 3D model ready:")
    print(f"  GLB (web/Blender)  -> {glb_path}")
    print(f"  OBJ (universal)    -> {obj_path}")
    print(f"\n  Preview GLB at: https://gltf-viewer.donmccurdy.com/")
    return str(glb_path)


if __name__ == "__main__":
    main()
