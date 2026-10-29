"""
Stage 3 — Vectorization
Converts pixel masks (from Stage 2) into a clean geometric scene graph:
  - Wall polygons (with thickness)
  - Room polygons (labeled)
  - Door/window bounding boxes

Outputs (saved to output/vectors/):
  <stem>_scene.json   — full scene graph (used by Stage 4)
  <stem>_preview.png  — vectorized overlay for visual inspection
"""

import argparse
import json
import sys
from pathlib import Path

import cv2
import numpy as np
from shapely.geometry import Polygon, MultiPolygon
from shapely.ops import unary_union
from shapely.validation import make_valid


# ── Tuning constants ──────────────────────────────────────────────────────────
WALL_CLOSE_KERNEL  = 5    # morphological closing kernel size for walls
WALL_MIN_AREA      = 200  # ignore wall contours smaller than this (px²)
ROOM_MIN_AREA      = 1000 # ignore room regions smaller than this (px²)
SCALE_M_PER_PX     = 0.01 # default: 1 px = 1 cm (adjust after scale calibration)
# SIMPLIFY_TOL is computed adaptively in vectorize() as ~0.5% of shorter image dimension

ROOM_LABELS = [
    "room", "living room", "bedroom", "kitchen", "bathroom",
    "hallway", "dining room", "office", "storage", "balcony",
    "garage", "laundry", "closet", "utility", "staircase",
    "corridor", "terrace", "entrance", "library", "gym",
]


# ── Helpers ───────────────────────────────────────────────────────────────────

def morph_clean(mask: np.ndarray, kernel_size: int) -> np.ndarray:
    k = cv2.getStructuringElement(cv2.MORPH_RECT, (kernel_size, kernel_size))
    closed = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, k)
    opened = cv2.morphologyEx(closed, cv2.MORPH_OPEN, k)
    return opened


def smooth_mask(mask: np.ndarray) -> np.ndarray:
    """
    Remove bilinear-upsampling jaggedness from an ML-generated mask.
    Gaussian blur softens the irregular edges; re-thresholding at 0.5
    snaps them back to a clean binary boundary.
    """
    h, w = mask.shape[:2]
    # Blur radius scales with image size so it covers ~1% of the shorter dimension
    blur_r = max(3, int(min(h, w) * 0.008))
    if blur_r % 2 == 0:
        blur_r += 1
    blurred = cv2.GaussianBlur(mask.astype(np.float32), (blur_r, blur_r), 0)
    _, clean = cv2.threshold(blurred, 127, 255, cv2.THRESH_BINARY)
    return clean.astype(np.uint8)


def contours_to_polygons(mask: np.ndarray, min_area: float, tol: float):
    """Extract simplified polygons from a binary mask."""
    cnts, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    polys = []
    for c in cnts:
        if cv2.contourArea(c) < min_area:
            continue
        pts = c.squeeze()
        if pts.ndim != 2 or len(pts) < 4:
            continue
        try:
            p = make_valid(Polygon(pts).simplify(tol, preserve_topology=True))
            if not p.is_empty:
                polys.append(p)
        except Exception:
            pass
    return polys


def _merge_collinear(segs: list, is_horiz: bool, gap_px: float) -> list:
    """
    Merge nearby collinear segments along the same axis.
    Groups by the fixed axis value (y for H, x for V) within gap_px,
    then merges overlapping or nearby spans on the variable axis.
    The fixed-axis coordinate is length-weighted-averaged so merged
    segments stay on the true wall centerline rather than drifting to
    whichever individual detection happened to be first.
    """
    if not segs:
        return []
    # segs: H = (lx, y, rx, y)   V = (x, ty, x, by)
    axis_idx = 1 if is_horiz else 0   # fixed axis coordinate
    span_lo  = 0 if is_horiz else 1   # variable axis start
    span_hi  = 2 if is_horiz else 3   # variable axis end

    segs = sorted(segs, key=lambda s: (s[axis_idx], s[span_lo]))
    result = []
    cur       = list(segs[0])
    cur_len   = cur[span_hi] - cur[span_lo]
    for s in segs[1:]:
        if abs(s[axis_idx] - cur[axis_idx]) <= gap_px and s[span_lo] <= cur[span_hi] + gap_px:
            s_len = s[span_hi] - s[span_lo]
            total = cur_len + s_len
            if total > 0:
                # weighted average keeps the merged segment on the true centerline
                cur[axis_idx] = (cur[axis_idx] * cur_len + s[axis_idx] * s_len) / total
            cur[span_hi] = max(cur[span_hi], s[span_hi])
            cur_len      = total
        else:
            result.append(tuple(cur))
            cur     = list(s)
            cur_len = s[span_hi] - s[span_lo]
    result.append(tuple(cur))
    return result


def _resolve_junctions(h_segs: list, v_segs: list, snap: float) -> tuple:
    """
    Extend H/V segment endpoints so they meet nearby perpendicular segments,
    creating clean L, T, and X junctions with no gaps.
    """
    result_h = []
    for lx, my, rx, _ in h_segs:
        new_lx, new_rx = lx, rx
        for vx, vy1, _, vy2 in v_segs:
            # V-seg must span (or nearly span) this y-level
            if vy1 - snap <= my <= vy2 + snap:
                if lx - snap <= vx < lx:    # V-seg is near the left endpoint
                    new_lx = min(new_lx, vx)
                if rx < vx <= rx + snap:    # V-seg is near the right endpoint
                    new_rx = max(new_rx, vx)
        result_h.append((new_lx, my, new_rx, my))

    result_v = []
    for vx, ty, _, by in v_segs:
        new_ty, new_by = ty, by
        for lx, hy, rx, _ in h_segs:
            # H-seg must span (or nearly span) this x-level
            if lx - snap <= vx <= rx + snap:
                if ty - snap <= hy < ty:    # H-seg is near the top endpoint
                    new_ty = min(new_ty, hy)
                if by < hy <= by + snap:    # H-seg is near the bottom endpoint
                    new_by = max(new_by, hy)
        result_v.append((vx, new_ty, vx, new_by))

    return result_h, result_v


def _rectify_hough(filled: np.ndarray, input_thick: float,
                   output_thick: float = None) -> list:
    """
    Hough-based wall rectangles for thin CV walls (≤ 12 px).

    `filled`      — thin wall mask (close-cleaned, ~4-12 px wide, acts as centerlines)
    `input_thick` — actual pixel width of walls in `filled`
    `output_thick`— desired visual thickness of output rectangles (default = input_thick)

    Key design: run Hough on the THIN mask so detections stay on the wall centerline,
    then expand to `output_thick` when drawing rectangles.  This gives correct coverage
    without hallucinating walls in adjacent empty space.
    """
    if output_thick is None:
        output_thick = input_thick

    h, w   = filled.shape[:2]
    half   = output_thick / 2.0                           # rectangle half-width

    # Thin the mask to approximate 1-px centerlines using distance transform.
    # For a wall of width W, the distance transform peaks at W/2 at the center.
    # Keeping only pixels where dist > 0.35*W retains the central ~30% of each
    # wall cross-section, steering Hough detections to the true centerline.
    dist = cv2.distanceTransform(filled, cv2.DIST_L2, 3)
    center_thresh = max(1.0, input_thick * 0.35)
    _, thin = cv2.threshold(dist, center_thresh, 255, cv2.THRESH_BINARY)
    thin = thin.astype(np.uint8)

    # Hough parameters scaled to image and wall geometry
    min_len   = max(int(input_thick * 2), int(min(h, w) * 0.015), 12)
    # gap must bridge typical door openings (~5% of shorter image dimension)
    gap       = max(int(min(h, w) * 0.06), 40)
    threshold = max(min_len // 2, 8)

    lines = cv2.HoughLinesP(thin, 1, np.pi / 180, threshold=threshold,
                            minLineLength=min_len, maxLineGap=gap)
    if lines is None:
        return []

    h_segs, v_segs = [], []
    for seg in lines[:, 0]:
        x1, y1, x2, y2 = map(float, seg)
        dx, dy = abs(x2 - x1), abs(y2 - y1)
        if dx >= dy:
            h_segs.append((min(x1, x2), (y1 + y2) / 2, max(x1, x2), (y1 + y2) / 2))
        else:
            v_segs.append(((x1 + x2) / 2, min(y1, y2), (x1 + x2) / 2, max(y1, y2)))

    # merge_gap: collapse parallel lines across wall width + small misalignments
    merge_gap = max(input_thick * 4, 20)
    h_segs = _merge_collinear(h_segs, True,  merge_gap)
    v_segs = _merge_collinear(v_segs, False, merge_gap)
    h_segs, v_segs = _resolve_junctions(h_segs, v_segs, half * 4)

    rects = []
    for lx, my, rx, _ in h_segs:
        rects.append(make_valid(Polygon([
            (lx - half, my - half), (rx + half, my - half),
            (rx + half, my + half), (lx - half, my + half),
        ])))
    for vx, ty, _, by in v_segs:
        rects.append(make_valid(Polygon([
            (vx - half, ty - half), (vx + half, ty - half),
            (vx + half, by + half), (vx - half, by + half),
        ])))

    if not rects:
        return []
    merged = make_valid(unary_union([r for r in rects if not r.is_empty and r.area > 0]))
    return list(merged.geoms) if isinstance(merged, MultiPolygon) else [merged]


def _rectify_flood(filled: np.ndarray, thick: float,
                   opening_mask: np.ndarray = None) -> list:
    """
    Flood-fill based wall polygon.  Best for thick ML walls (≥ 12 px) that form
    complete room enclosures.  Returns Polygon(building, room_holes).
    """
    h, w = filled.shape[:2]

    # Patch door gaps: dilate opening detections to bridge wall gaps
    if opening_mask is not None and np.count_nonzero(opening_mask) > 0:
        op_r = max(int(thick * 2), int(min(h, w) * 0.015), 10)
        k_op = cv2.getStructuringElement(cv2.MORPH_RECT, (op_r * 2 + 1, op_r * 2 + 1))
        patched = cv2.bitwise_or(filled, cv2.dilate(opening_mask.astype(np.uint8), k_op))
    else:
        patched = filled

    kh = max(int(thick * 2), int(min(h, w) * 0.06), 20)
    sealed = cv2.morphologyEx(patched, cv2.MORPH_CLOSE,
                              cv2.getStructuringElement(cv2.MORPH_RECT, (kh * 2 + 1, kh * 2 + 1)))

    inv     = cv2.bitwise_not(sealed)
    inv_pad = cv2.copyMakeBorder(inv, 1, 1, 1, 1, cv2.BORDER_CONSTANT, value=255)
    hp, wp  = inv_pad.shape
    seed    = np.zeros((hp + 2, wp + 2), np.uint8)
    cv2.floodFill(inv_pad, seed, (0, 0), 0)
    rooms_mask = inv_pad[1:h + 1, 1:w + 1]
    solid = cv2.bitwise_or(sealed, rooms_mask)

    tol         = max(1.5, thick * 0.15)
    min_room_px = thick * thick * 4

    room_holes = []
    for c in cv2.findContours(rooms_mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)[0]:
        if cv2.contourArea(c) < min_room_px:
            continue
        pts = c.squeeze()
        if pts.ndim != 2 or len(pts) < 4:
            continue
        try:
            sp = make_valid(Polygon(pts.tolist()).simplify(tol, preserve_topology=True))
            if not sp.is_empty:
                room_holes.append(list(sp.exterior.coords))
        except Exception:
            pass

    polys = []
    for c in cv2.findContours(solid, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)[0]:
        if cv2.contourArea(c) < thick * thick * 4:
            continue
        pts = c.squeeze()
        if pts.ndim != 2 or len(pts) < 4:
            continue
        try:
            outer = make_valid(Polygon(pts.tolist()).simplify(tol, preserve_topology=True))
            if outer.is_empty:
                continue
            my_holes = [hh for hh in room_holes
                        if outer.contains(Polygon(hh).centroid)]
            p = make_valid(Polygon(list(outer.exterior.coords), my_holes))
            if not p.is_empty:
                polys.append(p)
        except Exception:
            pass

    if not polys:
        return []
    merged = make_valid(unary_union(polys))
    return list(merged.geoms) if isinstance(merged, MultiPolygon) else [merged]


def _rectify_thin(wall_mask: np.ndarray, input_thick: float,
                  output_thick: float = 10.0) -> list:
    """
    Directional morphological open → axis-aligned rectangles for thin CV walls.

    Hough-based methods produce slightly imprecise angles (1° resolution).
    Multiple nearly-parallel rectangles for the same wall create bumpy union
    outlines.  This method avoids angle estimation entirely:

      1. MORPH_OPEN with a horizontal kernel → keeps only long H runs
         MORPH_OPEN with a vertical   kernel → keeps only long V runs
      2. Each surviving CC is a single straight wall run with a known pixel
         extent.  Build a perfectly axis-aligned rectangle from the centroid
         (wall centre) and bounding-box extent (wall length).
      3. Union of clean H + V rectangles → exact right-angle T/L/cross joints.
         No Hough, no contour tracing, no angle approximation.
    """
    kl = max(3, int(input_thick) // 3 * 2 + 1)
    closed = cv2.morphologyEx(wall_mask, cv2.MORPH_CLOSE,
                               cv2.getStructuringElement(cv2.MORPH_RECT, (kl, kl)))

    half    = output_thick / 2.0
    ext     = half                          # extend each end for junction overlap
    min_run = max(8, int(input_thick * 2))  # ignore stubs shorter than ~2 wall-widths

    polys = []

    for is_horiz in (True, False):
        kshape = (min_run, 1) if is_horiz else (1, min_run)
        opened = cv2.morphologyEx(
            closed, cv2.MORPH_OPEN,
            cv2.getStructuringElement(cv2.MORPH_RECT, kshape))

        if opened.sum() == 0:
            continue

        n, _, stats, centroids = cv2.connectedComponentsWithStats(
            opened, connectivity=8)

        for lbl in range(1, n):
            x0 = stats[lbl, cv2.CC_STAT_LEFT]
            y0 = stats[lbl, cv2.CC_STAT_TOP]
            bw = stats[lbl, cv2.CC_STAT_WIDTH]
            bh = stats[lbl, cv2.CC_STAT_HEIGHT]
            cx = float(centroids[lbl, 0])
            cy = float(centroids[lbl, 1])

            if is_horiz:
                rect = Polygon([
                    (x0 - ext,      cy - half),
                    (x0 + bw + ext, cy - half),
                    (x0 + bw + ext, cy + half),
                    (x0 - ext,      cy + half),
                ])
            else:
                rect = Polygon([
                    (cx - half, y0 - ext),
                    (cx + half, y0 - ext),
                    (cx + half, y0 + bh + ext),
                    (cx - half, y0 + bh + ext),
                ])

            try:
                p = make_valid(rect)
                if not p.is_empty:
                    polys.append(p)
            except Exception:
                pass

    if not polys:
        return []
    merged = make_valid(unary_union(polys))
    return list(merged.geoms) if isinstance(merged, MultiPolygon) else [merged]


def rectify_walls(wall_mask: np.ndarray, thickness_px: float,
                  opening_mask: np.ndarray = None) -> list:
    """
    Convert wall mask → Shapely polygons.

    Dispatches on wall thickness:
      ≤ 12 px  (CV thin lines)  → dilate to 10 px then extract contours directly.
                                   CV walls are fragmented at door gaps so each
                                   piece is already a separate CC — Hough would
                                   miss the short ones; direct contours don't.
      > 12 px  (ML thick blobs) → Flood-fill: seal door gaps, find enclosed
                                   rooms, return Polygon(building, room_holes).
    """
    thick = max(3, int(thickness_px))

    if thick <= 12:
        return _rectify_thin(wall_mask, float(thick), output_thick=10.0)
    else:
        h, w   = wall_mask.shape[:2]
        kl     = max(3, thick // 3 * 2 + 1)
        filled = cv2.morphologyEx(wall_mask, cv2.MORPH_CLOSE,
                                  cv2.getStructuringElement(cv2.MORPH_RECT, (kl, kl)))
        return _rectify_flood(filled, float(thick), opening_mask)


def estimate_wall_thickness(wall_mask: np.ndarray) -> float:
    """Estimate average wall thickness via distance transform."""
    dist = cv2.distanceTransform(wall_mask, cv2.DIST_L2, 5)
    nonzero = dist[dist > 0]
    if len(nonzero) == 0:
        return 5.0  # fallback default
    return float(np.median(nonzero) * 2)  # diameter = 2× radius


def detect_openings(opening_mask: np.ndarray, min_area: float = 100):
    """Return list of door/window bounding boxes as dicts."""
    cnts, _ = cv2.findContours(opening_mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    boxes = []
    for c in cnts:
        if cv2.contourArea(c) < min_area:
            continue
        x, y, w, h = cv2.boundingRect(c)
        boxes.append({"x": int(x), "y": int(y), "w": int(w), "h": int(h)})
    return boxes


def label_rooms(room_map: np.ndarray, min_area: float):
    """
    Extract connected room regions from the coloured room map.
    Returns list of {label, polygon_coords, color, centroid}.
    """
    gray = cv2.cvtColor(room_map, cv2.COLOR_RGB2GRAY) if room_map.ndim == 3 else room_map
    _, binary = cv2.threshold(gray, 10, 255, cv2.THRESH_BINARY)

    num_labels, label_img, stats, centroids = cv2.connectedComponentsWithStats(
        binary, connectivity=8
    )

    rooms = []
    label_idx = 0
    for i in range(1, num_labels):  # skip background (0)
        area = stats[i, cv2.CC_STAT_AREA]
        if area < min_area:
            continue
        cx, cy = int(centroids[i][0]), int(centroids[i][1])
        x, y   = stats[i, cv2.CC_STAT_LEFT], stats[i, cv2.CC_STAT_TOP]
        w, h   = stats[i, cv2.CC_STAT_WIDTH], stats[i, cv2.CC_STAT_HEIGHT]

        # Sample dominant colour of this region
        region_mask = (label_img == i).astype(np.uint8)
        if room_map.ndim == 3:
            mean_color = cv2.mean(room_map, mask=region_mask)[:3]
            color = [int(mean_color[0]), int(mean_color[1]), int(mean_color[2])]
        else:
            color = [200, 200, 200]

        rooms.append({
            "id": label_idx,
            "label": ROOM_LABELS[label_idx % len(ROOM_LABELS)],
            "bbox": [int(x), int(y), int(w), int(h)],
            "centroid": [cx, cy],
            "color": color,
            "area_px": int(area),
        })
        label_idx += 1

    return rooms


def detect_rooms_from_walls(wall_mask: np.ndarray,
                             min_area: int = ROOM_MIN_AREA) -> list:
    """
    Detect rooms directly from the wall mask when stage-2 room_map is unavailable.

    Strategy:
      1. MORPH_CLOSE with a kernel just larger than a typical door opening → seals
         every doorway without filling room interiors.
      2. Invert → the interior of each enclosed room is white.
      3. Flood-fill from the image border to zero out the exterior.
      4. Connected components of what remains = individual rooms.
    """
    h, w   = wall_mask.shape[:2]
    # Estimate door-gap width from image size: assume ~4% of shorter dimension.
    # Must be large enough to bridge open doorways (typically 40-60 px at common
    # scan resolutions) but smaller than any room interior.
    door_gap = max(44, int(min(h, w) * 0.04))
    close_r  = door_gap // 2 + 6          # radius comfortably larger than half the gap
    k        = cv2.getStructuringElement(
        cv2.MORPH_RECT, (close_r * 2 + 1, close_r * 2 + 1))
    sealed   = cv2.morphologyEx(wall_mask, cv2.MORPH_CLOSE, k)

    # Invert so room interiors are white, walls are black
    inv     = cv2.bitwise_not(sealed)

    # Pad by 1, flood-fill from corner to remove exterior, unpad
    padded  = np.pad(inv, 1, constant_values=255)
    cv2.floodFill(padded, np.zeros((h + 4, w + 4), np.uint8), (0, 0), 0)
    interior = padded[1:h + 1, 1:w + 1]

    n, _, stats, centroids = cv2.connectedComponentsWithStats(
        interior, connectivity=4)

    ROOM_COLORS = [
        [200, 230, 255], [255, 230, 180], [200, 255, 200],
        [255, 200, 220], [220, 220, 255], [255, 255, 200],
        [200, 255, 255], [255, 210, 180], [230, 255, 230],
    ]

    rooms = []
    for i in range(1, n):
        area = stats[i, cv2.CC_STAT_AREA]
        if area < min_area:
            continue
        cx = float(centroids[i, 0])
        cy = float(centroids[i, 1])
        x0 = int(stats[i, cv2.CC_STAT_LEFT])
        y0 = int(stats[i, cv2.CC_STAT_TOP])
        bw = int(stats[i, cv2.CC_STAT_WIDTH])
        bh = int(stats[i, cv2.CC_STAT_HEIGHT])
        rooms.append({
            "id": len(rooms),
            "label": "room",
            "bbox": [x0, y0, bw, bh],
            "centroid": [int(cx), int(cy)],
            "area_px": int(area),
            "color": ROOM_COLORS[len(rooms) % len(ROOM_COLORS)],
        })

    return rooms


def poly_to_coords(poly: Polygon):
    """Convert Shapely polygon exterior to list of [x, y] pairs (ints)."""
    coords = list(poly.exterior.coords)
    return [[int(x), int(y)] for x, y in coords]


# ── Main ──────────────────────────────────────────────────────────────────────

def vectorize(npz_path: Path, out_dir: Path, scale: float = SCALE_M_PER_PX):
    data = np.load(str(npz_path), allow_pickle=True)
    wall_mask    = data["walls"]
    room_map     = data["rooms"] if data["rooms"].ndim == 3 else None
    opening_mask = data["openings"]

    h_px, w_px = wall_mask.shape[:2]
    print(f"  Image size: {w_px}x{h_px} px  ->  {w_px*scale:.2f}x{h_px*scale:.2f} m")

    # ── Walls ──────────────────────────────────────────────────────────────
    wall_clean        = morph_clean(wall_mask, WALL_CLOSE_KERNEL)
    wall_thickness_px = estimate_wall_thickness(wall_mask)

    # Pass the raw mask and measured thickness; rectify_walls dispatches on thickness:
    # ≤12 px → Hough on thin centerlines, rectangles drawn at 10 px output width
    # >12 px → flood-fill with room holes
    wall_polys = rectify_walls(wall_mask, wall_thickness_px, opening_mask)

    print(f"  Wall polygons: {len(wall_polys)}  |  native {w_px}×{h_px}  |  "
          f"thickness ~= {wall_thickness_px:.1f} px = {wall_thickness_px*scale:.3f} m")

    # ── Rooms ──────────────────────────────────────────────────────────────
    rooms = []
    if room_map is not None:
        rooms = label_rooms(room_map, ROOM_MIN_AREA)
    if not rooms:                          # fallback: detect from wall mask directly
        rooms = detect_rooms_from_walls(wall_mask, ROOM_MIN_AREA)
    print(f"  Rooms detected: {len(rooms)}")

    # ── Openings (doors / windows) ─────────────────────────────────────────
    openings = detect_openings(opening_mask)
    print(f"  Openings detected: {len(openings)}")

    # ── Wall pixel bounding box (used by stage_llm for accurate scale) ────────
    ys_nz, xs_nz = np.where(wall_mask > 0)
    if len(xs_nz):
        wall_bbox = {
            "x_min": int(xs_nz.min()), "x_max": int(xs_nz.max()),
            "y_min": int(ys_nz.min()), "y_max": int(ys_nz.max()),
            "width_px":  int(xs_nz.max() - xs_nz.min()),
            "height_px": int(ys_nz.max() - ys_nz.min()),
        }
    else:
        wall_bbox = {}

    # ── Build scene graph ──────────────────────────────────────────────────
    scene = {
        "meta": {
            "width_px": w_px,
            "height_px": h_px,
            "scale_m_per_px": scale,
            "wall_thickness_px": wall_thickness_px,
            "wall_thickness_m": wall_thickness_px * scale,
            "wall_bbox_px": wall_bbox,
        },
        "walls": [
            {
                "id":      i,
                "polygon": poly_to_coords(p),
                # Interior rings = room holes so stage4 extrudes only the wall material
                "holes":   [[[int(x), int(y)] for x, y in ring.coords]
                            for ring in p.interiors],
                "area_px": int(p.area),
            }
            for i, p in enumerate(wall_polys)
            if not p.is_empty and p.area >= WALL_MIN_AREA
        ],
        "rooms": rooms,
        "openings": openings,
    }

    return scene, wall_clean, wall_polys, rooms, openings


def render_preview(scene: dict, wall_clean: np.ndarray, out_path: Path):
    h, w = wall_clean.shape[:2]
    canvas = np.full((h, w, 3), 255, dtype=np.uint8)

    # Draw wall exteriors dark, then erase room holes (restore white)
    for wall in scene["walls"]:
        pts = np.array(wall["polygon"], dtype=np.int32)
        if len(pts) >= 3:
            cv2.fillPoly(canvas, [pts], (50, 50, 50))
        for hole in wall.get("holes", []):
            h_pts = np.array(hole, dtype=np.int32)
            if len(h_pts) >= 3:
                cv2.fillPoly(canvas, [h_pts], (255, 255, 255))

    # Draw room centroids
    for r in scene["rooms"]:
        cx, cy = r["centroid"]
        color  = tuple(int(c) for c in r["color"])
        cv2.circle(canvas, (cx, cy), 6, color, -1)
        cv2.putText(canvas, r["label"], (cx + 8, cy + 5),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.35, (80, 80, 80), 1)

    # Draw openings
    for op in scene["openings"]:
        x, y, bw, bh = op["x"], op["y"], op["w"], op["h"]
        cv2.rectangle(canvas, (x, y), (x + bw, y + bh), (0, 120, 255), 2)

    cv2.imwrite(str(out_path), canvas)


def main():
    parser = argparse.ArgumentParser(description="Stage 3: Vectorize segmentation masks")
    parser.add_argument("input", help="Path to <stem>_masks.npz from Stage 2")
    parser.add_argument("--output-dir", default="output/vectors")
    parser.add_argument("--scale", type=float, default=SCALE_M_PER_PX,
                        help="Metres per pixel (default 0.01 = 1 cm/px)")
    args = parser.parse_args()

    npz_path = Path(args.input)
    if not npz_path.exists():
        print(f"[ERROR] File not found: {npz_path}")
        sys.exit(1)

    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = npz_path.stem.replace("_masks", "")

    print(f"[Stage 3] Vectorizing: {npz_path}")
    scene, wall_clean, wall_polys, rooms, openings = vectorize(npz_path, out_dir, args.scale)

    scene_path   = out_dir / f"{stem}_scene.json"
    preview_path = out_dir / f"{stem}_preview.png"

    with open(scene_path, "w") as f:
        json.dump(scene, f, indent=2)
    render_preview(scene, wall_clean, preview_path)

    print(f"[Stage 3] Scene graph -> {scene_path}")
    print(f"[Stage 3] Preview     -> {preview_path}")
    return str(scene_path)


if __name__ == "__main__":
    main()
