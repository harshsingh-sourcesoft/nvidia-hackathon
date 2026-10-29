"""
PlanLift Pipeline API Server
Accepts a floor plan PNG and runs stages 1-4, returning the GLB model.

Start with:
  venv\Scripts\python.exe api_server.py

Then in the web editor click "Analyze Floor Plan" and upload a PNG.
"""

import base64
import math
import sys as _sys
print(f"[PlanLift] api_server.py loading from: {__file__}", flush=True)
print(f"[PlanLift] Python executable: {_sys.executable}", flush=True)
import os
import json
import subprocess
import threading
import time
import uuid
from pathlib import Path
import cv2
import numpy as np
from flask import Flask, request, jsonify
from flask_cors import CORS

app = Flask(__name__)
CORS(app)  # allow requests from Next.js dev server (localhost:3000)

BASE_DIR = Path(__file__).parent
PYTHON   = str(BASE_DIR / "venv" / "Scripts" / "python.exe")

# ── Isaac Sim task store ──────────────────────────────────────────────────────
_isaac_tasks: dict[str, dict] = {}
_isaac_lock  = threading.Lock()

# ── Wall + door extractors ────────────────────────────────────────────────────

def _seg_t(px, py, x1, y1, x2, y2):
    """Return (dist, t) — distance from point to segment and parameter t ∈ [0,1]."""
    dx, dy = x2 - x1, y2 - y1
    lsq = dx*dx + dy*dy
    if lsq < 1e-6:
        return math.hypot(px - x1, py - y1), 0.0
    t = max(0.0, min(1.0, ((px-x1)*dx + (py-y1)*dy) / lsq))
    return math.hypot(px - (x1+t*dx), py - (y1+t*dy)), t


def walls_from_npz(npz_path: Path) -> list:
    """
    Extract individual axis-aligned wall segments directly from the stage-2 wall mask.

    Stage-3 merges H/V rectangles with unary_union, producing L/T/cross-shaped blobs
    whose PCA aspect ratio is too low to pass any elongation filter.  This function
    runs the same morphological-OPEN pass as stage-3's _rectify_thin, but skips the
    union step so each connected component becomes one clean wall segment.

    Algorithm:
      1. Load wall mask from NPZ; estimate wall thickness via distance transform.
      2. Morph-CLOSE to heal tiny gaps in the thin skeleton.
      3. Morph-OPEN with horizontal kernel  → isolates horizontal runs.
         Morph-OPEN with vertical   kernel  → isolates vertical   runs.
      4. Each connected component of the opened mask = one wall segment.
      5. Return centerline wall dicts (no duplicates: each wall is strictly H or V).
    """
    try:
        data      = np.load(str(npz_path), allow_pickle=True)
        wall_mask = data["walls"].astype(np.uint8)
    except Exception:
        return []

    if wall_mask.max() == 0:
        return []

    # ── Estimate wall thickness ────────────────────────────────────────────────
    dist = cv2.distanceTransform(wall_mask, cv2.DIST_L2, 5)
    nz   = dist[dist > 0]
    thick = float(np.median(nz) * 2) if len(nz) > 0 else 5.0

    h, w         = wall_mask.shape[:2]
    output_thick = max(15.0, thick)          # visual editor thickness (min 15 du)
    # Reject stubs shorter than ~3.5% of the image or 4× wall thickness.
    # This filters junction-corner fragments and door-gap slivers while keeping
    # any real wall segment longer than ~0.35m at typical scan resolution.
    min_run      = max(int(min(h, w) * 0.035), int(thick * 4), 15)

    # Small close pass to smooth sub-pixel gaps in the skeleton
    kl     = max(3, int(thick) // 3 * 2 + 1)
    closed = cv2.morphologyEx(wall_mask, cv2.MORPH_CLOSE,
                              cv2.getStructuringElement(cv2.MORPH_RECT, (kl, kl)))

    walls = []
    idx   = 0

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
            x0  = float(stats[lbl, cv2.CC_STAT_LEFT])
            y0  = float(stats[lbl, cv2.CC_STAT_TOP])
            bw  = float(stats[lbl, cv2.CC_STAT_WIDTH])
            bh  = float(stats[lbl, cv2.CC_STAT_HEIGHT])
            cx  = float(centroids[lbl, 0])
            cy  = float(centroids[lbl, 1])
            run = bw if is_horiz else bh
            if run < min_run:
                continue

            if is_horiz:
                walls.append({
                    "id":        f"mw{idx}",
                    "start":     {"x": round(x0,       1), "y": round(cy, 1)},
                    "end":       {"x": round(x0 + bw,  1), "y": round(cy, 1)},
                    "thickness": round(output_thick, 1),
                    "height":    270.0,
                    "type":      "wall",
                })
            else:
                walls.append({
                    "id":        f"mw{idx}",
                    "start":     {"x": round(cx, 1), "y": round(y0,       1)},
                    "end":       {"x": round(cx, 1), "y": round(y0 + bh,  1)},
                    "thickness": round(output_thick, 1),
                    "height":    270.0,
                    "type":      "wall",
                })
            idx += 1

    return walls


def snap_wall_endpoints(walls: list, thick: float) -> list:
    """
    Snap wall endpoints to nearby perpendicular walls to close T/L junction gaps.
    Morphological OPEN leaves a small erosion gap (≈ wall thickness) at every junction.
    This pass re-extends each endpoint to the centre-line of the crossing wall.
    """
    snap = max(int(thick * 3.5), 20)
    result = [dict(w) for w in walls]

    h_walls = [(i, w) for i, w in enumerate(result)
               if abs(w['start']['y'] - w['end']['y']) < 2]
    v_walls = [(i, w) for i, w in enumerate(result)
               if abs(w['start']['x'] - w['end']['x']) < 2]

    # Snap H-wall endpoints onto nearby V-wall centre-lines
    for hi, hw in h_walls:
        hy = (hw['start']['y'] + hw['end']['y']) / 2
        for ep in ('start', 'end'):
            ex = hw[ep]['x']
            best_x, best_d = None, snap
            for _, vw in v_walls:
                vx     = (vw['start']['x'] + vw['end']['x']) / 2
                vy_min = min(vw['start']['y'], vw['end']['y'])
                vy_max = max(vw['start']['y'], vw['end']['y'])
                d = abs(vx - ex)
                if d < best_d and vy_min - snap <= hy <= vy_max + snap:
                    best_d, best_x = d, vx
            if best_x is not None:
                result[hi] = dict(result[hi])
                result[hi][ep] = {'x': round(best_x, 1), 'y': round(hy, 1)}

    # Snap V-wall endpoints onto nearby H-wall centre-lines
    for vi, vw in v_walls:
        vx = (vw['start']['x'] + vw['end']['x']) / 2
        for ep in ('start', 'end'):
            ey = vw[ep]['y']
            best_y, best_d = None, snap
            for _, hw in h_walls:
                hy     = (hw['start']['y'] + hw['end']['y']) / 2
                hx_min = min(hw['start']['x'], hw['end']['x'])
                hx_max = max(hw['start']['x'], hw['end']['x'])
                d = abs(hy - ey)
                if d < best_d and hx_min - snap <= vx <= hx_max + snap:
                    best_d, best_y = d, hy
            if best_y is not None:
                result[vi] = dict(result[vi])
                result[vi][ep] = {'x': round(vx, 1), 'y': round(best_y, 1)}

    return result


def rooms_from_npz(npz_path: Path, thick: float) -> list:
    """
    Detect enclosed room polygons from the stage-2 wall mask.
    Seals door/window openings with a large close kernel, flood-fills from the
    image border to remove exterior, then traces each interior connected component
    as a simplified polygon.
    """
    try:
        data      = np.load(str(npz_path), allow_pickle=True)
        wall_mask = data["walls"].astype(np.uint8)
    except Exception:
        return []

    h, w = wall_mask.shape[:2]

    # Close kernel large enough to bridge typical door openings (~8% of image)
    close_r = max(int(min(h, w) * 0.08), 45)
    k_seal  = cv2.getStructuringElement(cv2.MORPH_RECT, (close_r * 2 + 1, close_r * 2 + 1))
    sealed  = cv2.morphologyEx(wall_mask, cv2.MORPH_CLOSE, k_seal)

    # Flood-fill from border to isolate interior (room) regions
    inv    = cv2.bitwise_not(sealed)
    padded = np.pad(inv, 1, constant_values=255)
    cv2.floodFill(padded, np.zeros((h + 4, w + 4), np.uint8), (0, 0), 0)
    interior = padded[1:h + 1, 1:w + 1]

    n, label_img, stats, _ = cv2.connectedComponentsWithStats(interior, connectivity=4)

    min_area = (min(h, w) * 0.04) ** 2   # ignore tiny gaps (< 4% × 4% of image)

    COLORS = ['#c8dff5', '#ffddb0', '#c8f5c8', '#f5c8d8', '#ddc8f5',
              '#f5f5c8', '#c8e5d8', '#f0d8a0', '#d8c8f5', '#f0f0d8']
    NAMES  = ['Living Room', 'Bedroom', 'Kitchen', 'Bathroom', 'Hallway',
              'Dining Room', 'Office', 'Storage', 'Closet', 'Room']

    rooms = []
    for i in range(1, n):
        if stats[i, cv2.CC_STAT_AREA] < min_area:
            continue

        room_mask = ((label_img == i) * 255).astype(np.uint8)
        cnts, _   = cv2.findContours(room_mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        if not cnts:
            continue
        cnt = max(cnts, key=cv2.contourArea)

        # Simplify to key corners (1.5% of perimeter tolerance)
        eps    = 0.015 * cv2.arcLength(cnt, True)
        approx = cv2.approxPolyDP(cnt, eps, True)
        pts    = approx.squeeze().tolist()
        if not pts or not isinstance(pts[0], list) or len(pts) < 3:
            continue

        idx = len(rooms)
        area_m2 = round(float(stats[i, cv2.CC_STAT_AREA]) / 10000, 1)
        rooms.append({
            "id":      f"pr{i}",
            "name":    NAMES[idx % len(NAMES)],
            "polygon": [{"x": float(p[0]), "y": float(p[1])} for p in pts],
            "area":    area_m2,
            "color":   COLORS[idx % len(COLORS)],
            "type":    "room",
        })

    return rooms


def _collect_gaps(profile: np.ndarray, wlen: int, is_horiz: bool,
                  xa: int, ya: int, y_c: int, x_c: int,
                  wall_id: str, min_gap: int, max_gap: int, margin: int,
                  width_adjust: float = 0.0) -> list:
    """Scan a boolean profile for True-runs = gaps; return list of gap dicts."""
    gaps: list[dict] = []
    n = len(profile)
    if margin * 2 >= n:
        return gaps
    profile = profile.copy()
    profile[:margin]  = False
    profile[-margin:] = False
    i = 0
    while i < n:
        if profile[i]:
            j = i
            while j < n and profile[j]:
                j += 1
            gap_w = j - i
            # A real opening spans at most 60 % of its wall; larger = spurious detection
            if min_gap <= gap_w <= max_gap and gap_w / float(wlen) < 0.60:
                mid = (i + j) / 2.0
                t   = max(0.05, min(0.95, mid / float(wlen)))
                gcx = float(xa + mid) if is_horiz else float(x_c)
                gcy = float(y_c)       if is_horiz else float(ya + mid)
                gaps.append({"wallId": wall_id, "t": round(t, 3),
                             "width": float(gap_w) + width_adjust,
                             "cx": gcx, "cy": gcy})
            i = j
        else:
            i += 1
    return gaps


def _split_wide_ml(cands: list, img_gray, walls: list,
                   min_gap: int, max_gap: int, band_half: int,
                   margin: int, expected_max: int) -> list:
    """Split any ML candidate opening wider than expected_max via min-pixel rescan."""
    if img_gray is None:
        return cands
    result: list[dict] = []
    for c in cands:
        if c["width"] > expected_max:
            subs = _rescan_gap(c, img_gray, walls, min_gap, margin, band_half, max_gap=max_gap)
            result.extend(subs)
        else:
            result.append(c)
    if len(result) != len(cands):
        print(f"  Split wide: {len(cands)} → {len(result)}")
    return result


def _rescan_gap(gap: dict, img_gray: np.ndarray, walls: list,
                min_gap: int, margin: int, band_half: int,
                _unused: int = 0, max_gap: int = 350) -> list:
    """
    Re-scan a wide gap using the min-pixel adaptive approach to find two
    adjacent openings that the primary pass merged into one.
    Returns [gap] unchanged if no clean split is found.
    """
    wall = next((w for w in walls if w["id"] == gap["wallId"]), None)
    if wall is None:
        return [gap]
    x1 = float(wall["start"]["x"]); y1 = float(wall["start"]["y"])
    x2 = float(wall["end"]["x"]);   y2 = float(wall["end"]["y"])
    is_h  = abs(x2 - x1) >= abs(y2 - y1)
    h_m, w_m = img_gray.shape
    half_w = gap["width"] / 2.0

    if is_h:
        xa  = max(0, int(round(gap["cx"] - half_w)))
        xb  = min(w_m, int(round(gap["cx"] + half_w)))
        y_c = int(round(gap["cy"]))
        wlen = xb - xa
        if wlen < min_gap * 2:
            return [gap]
        y_lo = max(0, y_c - band_half); y_hi = min(h_m, y_c + band_half + 1)
        band    = img_gray[y_lo:y_hi, xa:xb].astype(np.float32)
        profile = band.min(axis=0)
    else:
        ya  = max(0, int(round(gap["cy"] - half_w)))
        yb  = min(h_m, int(round(gap["cy"] + half_w)))
        x_c = int(round(gap["cx"]))
        wlen = yb - ya
        if wlen < min_gap * 2:
            return [gap]
        x_lo = max(0, x_c - band_half); x_hi = min(w_m, x_c + band_half + 1)
        band    = img_gray[ya:yb, x_lo:x_hi].astype(np.float32)
        profile = band.min(axis=1)

    wall_dark = float(np.percentile(profile, 10))
    thresh    = min(220.0, max(130.0, wall_dark + 70.0))
    prof      = profile > thresh
    subs = _collect_gaps(
        prof, wlen, is_h,
        xa if is_h else 0,
        0  if is_h else ya,
        y_c if is_h else 0,
        0   if is_h else x_c,
        gap["wallId"], min_gap, max_gap, margin,
    )
    if len(subs) < 2:
        return [gap]
    return subs


def _classify_door_type(img_gray, cx: float, cy: float, gap_w: float) -> str:
    """Return 'sliding' if parallel lines detected in the gap region, else 'swing'."""
    if img_gray is None:
        return "swing"
    dim    = max(int(gap_w * 1.4), 20)
    x0     = max(0, int(cx - dim // 2))
    y0     = max(0, int(cy - dim // 2))
    x1     = min(img_gray.shape[1], x0 + dim)
    y1     = min(img_gray.shape[0], y0 + dim)
    roi    = img_gray[y0:y1, x0:x1]
    if roi.size == 0:
        return "swing"
    min_len = max(int(gap_w * 0.3), 8)
    lines   = cv2.HoughLinesP(roi, 1, np.pi / 180, threshold=10,
                               minLineLength=min_len, maxLineGap=5)
    return "sliding" if (lines is not None and len(lines) >= 2) else "swing"


def openings_from_npz(
    npz_path: Path, walls: list, img_path: Path, thick: float
) -> tuple[list, list]:
    """
    Detect doors and windows using a hybrid approach.

    PRIMARY PATH (standard architectural floor plans):
      Method A: min-pixel scan along each wall → accurate gap positions from
        the actual image.  ML heatmaps / opening_polys classify each gap as
        door or window; Hough arcs are the final fallback classifier.
      This path is always tried first, even when CubiCasa ML weights produced
      heatmaps — those heatmaps are used for classification, not positioning,
      because Method A gives more accurate and non-merged gap positions.

    FALLBACK PATH (< 3 image-scan gaps — solid-wall / CAD plans):
      ML-Poly → ML heatmaps → Method B (mask scan) → Method D (Hough only).
      These paths also call _split_wide_ml to break merged adjacent windows.
    """
    try:
        data = np.load(str(npz_path), allow_pickle=True)
    except Exception:
        return [], []
    if "walls" not in data:
        return [], []

    wall_mask = data["walls"].astype(np.uint8)
    h_m, w_m  = wall_mask.shape

    # ── Shared setup (needed by all paths) ───────────────────────────────
    img_gray = cv2.imread(str(img_path), cv2.IMREAD_GRAYSCALE)
    if img_gray is not None and img_gray.shape[:2] != (h_m, w_m):
        img_gray = cv2.resize(img_gray, (w_m, h_m), interpolation=cv2.INTER_AREA)

    min_gap      = max(20, int(min(h_m, w_m) * 0.010))
    max_gap      = max(350, int(min(h_m, w_m) * 0.30))
    thick_px_est = max(8, int(min(h_m, w_m) * 0.015))
    dil_per_side = (thick_px_est - 1) // 2
    img_wall_est = max(4, int(thick) - 2 * dil_per_side)
    band_half    = max(img_wall_est, 8)
    img_margin   = max(5, dil_per_side)
    mask_margin  = max(int(thick * 0.5), 8)
    expected_max = max(int(min_gap * 5), int(min(h_m, w_m) * 0.15))

    # Hough arcs — used for door classification in all paths
    arcs: list[tuple[float, float, float]] = []
    if img_gray is not None:
        blurred  = cv2.GaussianBlur(img_gray, (5, 5), 0)
        min_r    = max(8, min(img_gray.shape) // 60)
        max_r    = max(min_r + 2, min(img_gray.shape) // 6)
        raw_arcs = cv2.HoughCircles(blurred, cv2.HOUGH_GRADIENT, dp=1.2,
                                    minDist=min_r * 1.5, param1=60, param2=40,
                                    minRadius=min_r, maxRadius=max_r)
        if raw_arcs is not None:
            for cx_i, cy_i, r_i in np.round(raw_arcs[0]).astype(int):
                arcs.append((float(cx_i), float(cy_i), float(r_i)))
    print(f"  Hough arcs: {len(arcs)}")

    # Extract ML data early for use as classifiers (not positioners)
    door_hmap     = data["doors_ml"].astype(np.float32)   if "doors_ml"      in data else None
    win_hmap      = data["windows_ml"].astype(np.float32) if "windows_ml"    in data else None
    opening_polys = data["opening_polys"]                 if "opening_polys" in data else None
    if opening_polys is not None and len(opening_polys) == 0:
        opening_polys = None

    # ── Method A: min-pixel image scan (PRIMARY gap detector) ────────────
    # Always runs first.  ML heatmaps are used only to classify each gap as
    # door or window — they do NOT provide positions.  This avoids the
    # merged-window problem where two adjacent windows appear as one wide blob
    # in the heatmap, and eliminates door-position errors caused by projecting
    # arc centroids that are in the middle of the room, not on the wall.
    all_gaps: list[dict] = []
    if img_gray is not None:
        for wall in walls:
            x1 = float(wall["start"]["x"]); y1 = float(wall["start"]["y"])
            x2 = float(wall["end"]["x"]);   y2 = float(wall["end"]["y"])
            is_h = abs(x2 - x1) >= abs(y2 - y1)

            if is_h:
                xa = int(round(min(x1, x2))); xb = int(round(max(x1, x2)))
                y_c = int(round((y1 + y2) / 2)); wlen = xb - xa
                if wlen < min_gap * 2: continue
                y_lo = max(0, y_c - band_half); y_hi = min(h_m, y_c + band_half + 1)
                if y_lo >= y_hi: continue
                band    = img_gray[y_lo:y_hi, xa:xb].astype(np.float32)
                profile = band.min(axis=0)
            else:
                ya = int(round(min(y1, y2))); yb = int(round(max(y1, y2)))
                x_c = int(round((x1 + x2) / 2)); wlen = yb - ya
                if wlen < min_gap * 2: continue
                x_lo = max(0, x_c - band_half); x_hi = min(w_m, x_c + band_half + 1)
                if x_lo >= x_hi: continue
                band    = img_gray[ya:yb, x_lo:x_hi].astype(np.float32)
                profile = band.min(axis=1)

            if float(profile.min()) > 180:
                continue

            wall_dark = float(np.percentile(profile, 10))
            thresh    = min(220.0, max(130.0, wall_dark + 70.0))
            prof = profile > thresh
            all_gaps.extend(_collect_gaps(
                prof, wlen, is_h,
                xa if is_h else 0,
                0  if is_h else ya,
                y_c if is_h else 0,
                0   if is_h else x_c,
                wall["id"], min_gap, max_gap, img_margin))

    print(f"  Method A gaps (image scan): {len(all_gaps)}")

    # Split wide gaps (two adjacent windows merged into one)
    if all_gaps and img_gray is not None:
        split_result: list[dict] = []
        for g in all_gaps:
            if g["width"] > expected_max:
                split_result.extend(_rescan_gap(
                    g, img_gray, walls, min_gap, img_margin, band_half, 100, max_gap))
            else:
                split_result.append(g)
        if len(split_result) != len(all_gaps):
            print(f"  Gap split: {len(all_gaps)} → {len(split_result)}")
            all_gaps = split_result

    # ── Method A succeeded → classify gaps using ML heatmaps + Hough ─────
    if len(all_gaps) >= 3:
        deduped: list[dict] = []
        for gap in all_gaps:
            dup = next((e for e in deduped
                        if e["wallId"] == gap["wallId"]
                        and abs(e["t"] - gap["t"]) < 0.04), None)
            if dup:
                if gap["width"] > dup["width"]: dup.update(gap)
            else:
                deduped.append(gap)
        print(f"  Method A gaps (deduped): {len(deduped)}")

        used_arcs: set[int] = set()
        doors:   list[dict] = []
        windows: list[dict] = []
        door_idx = win_idx = 0
        # Build a wall lookup for orientation checks in the classifier
        wall_map = {w["id"]: w for w in walls}

        for gap in deduped:
            cx, cy = gap["cx"], gap["cy"]
            # ml_type: None = undecided, True = door, False = window
            ml_type: bool | None = None

            # 1. opening_polys: find nearest door AND window polygon, pick closer one.
            #    Use a tight match radius (gap_width, min 30px) to avoid cross-matches.
            if opening_polys is not None and ml_type is None:
                best_d = float("inf"); best_w = float("inf")
                for row in opening_polys:
                    poly_cx = float(row[0]); poly_cy = float(row[1])
                    poly_w  = float(row[2]); cls     = int(row[3])
                    dist    = math.hypot(poly_cx - cx, poly_cy - cy)
                    radius  = max(poly_w * 0.8, 30.0)
                    if dist < radius:
                        if cls == 2: best_d = min(best_d, dist)
                        elif cls == 1: best_w = min(best_w, dist)
                if best_d < float("inf") or best_w < float("inf"):
                    ml_type = (best_d <= best_w)   # True=door if door closer/equal

            # 2. ML heatmaps: sample within a narrow wall-aligned band only.
            #    A square region would extend into rooms and pick up door-arc heatmaps
            #    from nearby doors, causing windows to be misclassified as doors.
            if ml_type is None and door_hmap is not None and win_hmap is not None:
                wg = wall_map.get(gap["wallId"])
                if wg is not None:
                    g_is_h = abs(wg["end"]["x"] - wg["start"]["x"]) >= abs(wg["end"]["y"] - wg["start"]["y"])
                    half_len = max(8, int(gap["width"] / 2))
                    if g_is_h:   # horizontal wall: wide along x, narrow along y
                        sx0 = max(0, int(cx) - half_len); sx1 = min(w_m, int(cx) + half_len + 1)
                        sy0 = max(0, int(cy) - band_half); sy1 = min(h_m, int(cy) + band_half + 1)
                    else:        # vertical wall: narrow along x, wide along y
                        sx0 = max(0, int(cx) - band_half); sx1 = min(w_m, int(cx) + band_half + 1)
                        sy0 = max(0, int(cy) - half_len); sy1 = min(h_m, int(cy) + half_len + 1)
                    d_v = float(door_hmap[sy0:sy1, sx0:sx1].max())
                    w_v = float(win_hmap[sy0:sy1, sx0:sx1].max())
                    if max(d_v, w_v) > 0.25:
                        ml_type = (d_v > w_v)   # True=door if door stronger

            # 3. Hough arc nearby → door (only when ML gave no signal)
            is_door = bool(ml_type) if ml_type is not None else False
            if ml_type is None:
                for ai, (cx_a, cy_a, r_a) in enumerate(arcs):
                    if math.hypot(cx_a - cx, cy_a - cy) < r_a * 2.5 and ai not in used_arcs:
                        is_door = True
                        used_arcs.add(ai)
                        break

            wid_val = round(max(30.0, gap["width"]), 1)
            if is_door:
                door_type = _classify_door_type(img_gray, cx, cy, gap["width"])
                doors.append({"id": f"pd{door_idx}", "wallId": gap["wallId"],
                              "positionOnWall": gap["t"], "width": wid_val,
                              "height": 210.0, "type": "door", "doorType": door_type})
                door_idx += 1
            else:
                windows.append({"id": f"pw{win_idx}", "wallId": gap["wallId"],
                                "positionOnWall": gap["t"], "width": wid_val,
                                "height": 120.0, "sillHeight": 90.0, "type": "window"})
                win_idx += 1

        print(f"  Doors: {len(doors)}  Windows: {len(windows)}")
        return doors, windows

    print(f"  Method A unreliable ({len(all_gaps)} < 3) → ML/fallback paths")
    all_gaps = []

    # ── ML-Poly fallback (solid-wall / CAD plans) ─────────────────────────
    if opening_polys is not None:
        print("  [Method ML-Poly] wall-snapped exact opening polygons")
        max_poly_d = max(thick * 5.0, 80.0)

        def _proj_poly(px: float, py: float):
            bw: dict | None = None; bt = 0.5; bd = float("inf")
            for wall in walls:
                x1w = wall["start"]["x"]; y1w = wall["start"]["y"]
                x2w = wall["end"]["x"];   y2w = wall["end"]["y"]
                dx = x2w - x1w; dy = y2w - y1w; lsq = dx*dx + dy*dy
                if lsq < 1: continue
                t = max(0.0, min(1.0, ((px-x1w)*dx+(py-y1w)*dy)/lsq))
                d = math.hypot(px-(x1w+t*dx), py-(y1w+t*dy))
                if d < bd: bd = d; bw = wall; bt = t
            return bw, bt, bd

        door_cands_p: list[dict] = []
        win_cands_p:  list[dict] = []
        for row in opening_polys:
            cx, cy, width, cls = (float(row[0]), float(row[1]),
                                  float(row[2]), int(row[3]))
            bw, bt, bd = _proj_poly(cx, cy)
            if bw is None or bd >= max_poly_d: continue
            entry = {"wallId": bw["id"], "t": round(max(0.05, min(0.95, bt)), 3),
                     "width": width, "cx": cx, "cy": cy}
            (door_cands_p if cls == 2 else win_cands_p).append(entry)

        def _dedup_p(lst: list) -> list:
            res: list[dict] = []
            for item in lst:
                dup = next((e for e in res if e["wallId"] == item["wallId"]
                            and abs(e["t"] - item["t"]) < 0.04), None)
                if dup:
                    if item["width"] > dup["width"]: dup.update(item)
                else:
                    res.append(item)
            return res

        door_cands_p = _dedup_p(door_cands_p)
        win_cands_p  = _dedup_p(win_cands_p)
        win_cands_p  = [w for w in win_cands_p
                        if not any(w["wallId"] == d["wallId"]
                                   and abs(w["t"] - d["t"]) < 0.05
                                   for d in door_cands_p)]
        win_cands_p = _split_wide_ml(win_cands_p, img_gray, walls,
                                     min_gap, max_gap, band_half, img_margin, expected_max)
        print(f"  ML-Poly: {len(door_cands_p)} door(s), {len(win_cands_p)} window(s)")
        if door_cands_p or win_cands_p:
            doors_p   = [{"id": f"pd{i}", "wallId": d["wallId"],
                           "positionOnWall": d["t"],
                           "width": round(max(30.0, d["width"]), 1),
                           "height": 210.0, "type": "door", "doorType": "swing"}
                          for i, d in enumerate(door_cands_p)]
            windows_p = [{"id": f"pw{i}", "wallId": w["wallId"],
                           "positionOnWall": w["t"],
                           "width": round(max(30.0, w["width"]), 1),
                           "height": 120.0, "sillHeight": 90.0, "type": "window"}
                          for i, w in enumerate(win_cands_p)]
            print(f"  Doors: {len(doors_p)}  Windows: {len(windows_p)}")
            return doors_p, windows_p

    # ── ML heatmap fallback ───────────────────────────────────────────────
    if door_hmap is not None or win_hmap is not None:
        print("  [Method ML] CubiCasa5k door/window heatmaps")
        max_ml_d = max(thick * 4.0, 60.0)
        min_span = max(15, int(min(h_m, w_m) * 0.010))
        max_span = max(300, int(min(h_m, w_m) * 0.25))
        min_area = max(40, min_span * min_span // 3)
        THRESH   = {"doors_ml": 0.22, "windows_ml": 0.28}
        PEAK_MIN = {"doors_ml": 0.35, "windows_ml": 0.40}

        def _proj_ml(px: float, py: float):
            bw: dict | None = None; bt = 0.5; bd = float("inf")
            for wall in walls:
                x1w = wall["start"]["x"]; y1w = wall["start"]["y"]
                x2w = wall["end"]["x"];   y2w = wall["end"]["y"]
                dx = x2w - x1w; dy = y2w - y1w; lsq = dx*dx + dy*dy
                if lsq < 1: continue
                t = max(0.0, min(1.0, ((px-x1w)*dx+(py-y1w)*dy)/lsq))
                d = math.hypot(px-(x1w+t*dx), py-(y1w+t*dy))
                if d < bd: bd = d; bw = wall; bt = t
            return bw, bt, bd

        def _ml_cands(key: str, hmap) -> list:
            if hmap is None: return []
            thr      = THRESH.get(key, 0.25)
            peak_min = PEAK_MIN.get(key, 0.38)
            if float(hmap.max()) < thr: return []
            bm = (hmap > thr).astype(np.uint8) * 255
            n_cc, labels, stats_cc, _ = cv2.connectedComponentsWithStats(bm, connectivity=8)
            cands: list[dict] = []
            for i in range(1, n_cc):
                area = int(stats_cc[i, cv2.CC_STAT_AREA])
                if area < min_area: continue
                span = max(int(stats_cc[i, cv2.CC_STAT_WIDTH]),
                           int(stats_cc[i, cv2.CC_STAT_HEIGHT]))
                if span < min_span or span > max_span: continue
                cc_pixels = hmap * (labels == i)
                peak_val  = float(cc_pixels.max())
                if peak_val < peak_min: continue
                peak_idx  = int(np.argmax(cc_pixels))
                py_px = peak_idx // w_m
                px_px = peak_idx %  w_m
                cx = float(px_px); cy = float(py_px)
                bw, bt, bd = _proj_ml(cx, cy)
                if bw is None or bd >= max_ml_d: continue
                cands.append({"wallId": bw["id"], "t": round(max(0.05, min(0.95, bt)), 3),
                              "width": float(span), "cx": cx, "cy": cy})
            return cands

        def _dedup_ml(lst: list) -> list:
            res: list[dict] = []
            for item in lst:
                dup = next((e for e in res
                            if e["wallId"] == item["wallId"]
                            and abs(e["t"] - item["t"]) < 0.04), None)
                if dup:
                    if item["width"] > dup["width"]: dup.update(item)
                else:
                    res.append(item)
            return res

        for _k, _h, _lbl in (("doors_ml", door_hmap, "door"), ("windows_ml", win_hmap, "window")):
            if _h is not None:
                print(f"  [ML-diag] {_lbl} heatmap max={_h.max():.3f} "
                      f"px>{THRESH[_k]}={int((_h>THRESH[_k]).sum())} "
                      f"px>{PEAK_MIN[_k]}={int((_h>PEAK_MIN[_k]).sum())}")
        door_cands = _dedup_ml(_ml_cands("doors_ml", door_hmap))
        win_cands  = _dedup_ml(_ml_cands("windows_ml", win_hmap))
        win_cands  = [w for w in win_cands
                      if not any(w["wallId"] == d["wallId"]
                                 and abs(w["t"] - d["t"]) < 0.05
                                 for d in door_cands)]
        win_cands = _split_wide_ml(win_cands, img_gray, walls,
                                   min_gap, max_gap, band_half, img_margin, expected_max)
        print(f"  Method ML: {len(door_cands)} door(s), {len(win_cands)} window(s)")
        doors_ml   = [{"id": f"pd{i}", "wallId": d["wallId"], "positionOnWall": d["t"],
                       "width": round(max(30.0, d["width"]), 1), "height": 210.0,
                       "type": "door", "doorType": "swing"}
                      for i, d in enumerate(door_cands)]
        windows_ml = [{"id": f"pw{i}", "wallId": w["wallId"], "positionOnWall": w["t"],
                       "width": round(max(30.0, w["width"]), 1), "height": 120.0,
                       "sillHeight": 90.0, "type": "window"}
                      for i, w in enumerate(win_cands)]
        print(f"  Doors: {len(doors_ml)}  Windows: {len(windows_ml)}")
        return doors_ml, windows_ml

    # ── Method B: wall-mask zero-pixel scan ───────────────────────────────
    print("  [Method B] wall-mask scan")
    sh = max(4, int(thick * 0.35))
    for wall in walls:
        x1 = float(wall["start"]["x"]); y1 = float(wall["start"]["y"])
        x2 = float(wall["end"]["x"]);   y2 = float(wall["end"]["y"])
        is_h = abs(x2 - x1) >= abs(y2 - y1)
        if is_h:
            xa = int(round(min(x1, x2))); xb = int(round(max(x1, x2)))
            y_c = int(round((y1 + y2) / 2)); wlen = xb - xa
            if wlen < min_gap * 2: continue
            y_lo = max(0, y_c - sh); y_hi = min(h_m, y_c + sh + 1)
            if y_lo >= y_hi: continue
            prof = wall_mask[y_lo:y_hi, xa:xb].max(axis=0) == 0
            all_gaps.extend(_collect_gaps(prof, wlen, True,  xa, 0,  y_c, 0,
                                          wall["id"], min_gap, max_gap, mask_margin,
                                          width_adjust=thick))
        else:
            ya = int(round(min(y1, y2))); yb = int(round(max(y1, y2)))
            x_c = int(round((x1 + x2) / 2)); wlen = yb - ya
            if wlen < min_gap * 2: continue
            x_lo = max(0, x_c - sh); x_hi = min(w_m, x_c + sh + 1)
            if x_lo >= x_hi: continue
            prof = wall_mask[ya:yb, x_lo:x_hi].max(axis=1) == 0
            all_gaps.extend(_collect_gaps(prof, wlen, False, 0, ya, 0,  x_c,
                                          wall["id"], min_gap, max_gap, mask_margin,
                                          width_adjust=thick))
    print(f"  Method B gaps: {len(all_gaps)}")

    # ── Method D: Hough-center doors + openings-mask CC windows ──────────
    if len(all_gaps) == 0:
        print("  [Method D] Hough-center doors + openings-mask windows")
        max_hinge_d = max(thick * 2.0, 20.0)
        max_win_d   = max(thick * 3.0, 40.0)

        def _proj(px: float, py: float, wall: dict):
            x1w = wall["start"]["x"]; y1w = wall["start"]["y"]
            x2w = wall["end"]["x"];   y2w = wall["end"]["y"]
            dx = x2w - x1w; dy = y2w - y1w; lsq = dx*dx + dy*dy
            if lsq < 1: return None, None, None
            t = max(0.0, min(1.0, ((px - x1w)*dx + (py - y1w)*dy) / lsq))
            d = math.hypot(px - (x1w + t*dx), py - (y1w + t*dy))
            return t, d, math.sqrt(lsq)

        door_cands: list[dict] = []
        for _ai, (cx_a, cy_a, r_a) in enumerate(arcs):
            bw: dict | None = None; bt = 0.5; bd = float("inf"); bwlen = 1.0
            for wall in walls:
                t, d, wlen = _proj(cx_a, cy_a, wall)
                if t is None: continue
                if d < bd: bd = d; bw = wall; bt = t; bwlen = wlen
            if bw is not None and bd < max_hinge_d:
                door_cands.append({
                    "wallId": bw["id"], "t": round(max(0.05, min(0.95, bt)), 3),
                    "width": float(r_a), "cx": cx_a, "cy": cy_a, "r": r_a,
                })

        deduped_d: list[dict] = []
        for dc in door_cands:
            dup = next((e for e in deduped_d
                        if e["wallId"] == dc["wallId"] and abs(e["t"] - dc["t"]) < 0.12), None)
            if dup:
                if dc["r"] > dup["r"]: dup.update(dc)
            else:
                deduped_d.append(dc)

        win_cands: list[dict] = []
        if "openings" in data and int(data["openings"].max()) > 0:
            op_mask = (data["openings"] > 128).astype(np.uint8) * 255
            n_cc, _, stats_op, cents_op = cv2.connectedComponentsWithStats(
                op_mask, connectivity=8)
            min_span = max(30, int(min(h_m, w_m) * 0.020))
            max_span = max(300, int(min(h_m, w_m) * 0.20))
            for i in range(1, n_cc):
                span = max(int(stats_op[i, cv2.CC_STAT_WIDTH]),
                           int(stats_op[i, cv2.CC_STAT_HEIGHT]))
                if span < min_span or span > max_span: continue
                cx = float(cents_op[i, 0]); cy = float(cents_op[i, 1])
                if any(math.hypot(cx - dc["cx"], cy - dc["cy"]) < dc["r"] * 1.5
                       for dc in deduped_d):
                    continue
                bw = None; bt = 0.5; bd = float("inf")
                for wall in walls:
                    t, d, _ = _proj(cx, cy, wall)
                    if t is None: continue
                    if d < bd: bd = d; bw = wall; bt = t
                if bw is not None and bd < max_win_d:
                    win_cands.append({
                        "wallId": bw["id"],
                        "t": round(max(0.05, min(0.95, bt)), 3),
                        "width": float(span), "cx": cx, "cy": cy,
                    })

        deduped_w: list[dict] = []
        for wc in win_cands:
            dup = next((e for e in deduped_w
                        if e["wallId"] == wc["wallId"] and abs(e["t"] - wc["t"]) < 0.08), None)
            if dup:
                if wc["width"] > dup["width"]: dup.update(wc)
            else:
                deduped_w.append(wc)
        deduped_w = [w for w in deduped_w
                     if not any(w["wallId"] == d["wallId"]
                                and abs(w["t"] - d["t"]) < 0.10
                                for d in deduped_d)]

        print(f"  Method D: {len(deduped_d)} door(s), {len(deduped_w)} window(s)")
        doors   = [{"id": f"pd{i}", "wallId": d["wallId"], "positionOnWall": d["t"],
                    "width": round(max(30.0, d["width"]), 1), "height": 210.0,
                    "type": "door", "doorType": "swing"}
                   for i, d in enumerate(deduped_d)]
        windows = [{"id": f"pw{i}", "wallId": w["wallId"], "positionOnWall": w["t"],
                    "width": round(max(30.0, w["width"]), 1), "height": 120.0,
                    "sillHeight": 90.0, "type": "window"}
                   for i, w in enumerate(deduped_w)]
        print(f"  Doors: {len(doors)}  Windows: {len(windows)}")
        return doors, windows

    # ── Method B found gaps → deduplicate then classify ───────────────────
    deduped_b: list[dict] = []
    for gap in all_gaps:
        dup = next((e for e in deduped_b
                    if e["wallId"] == gap["wallId"]
                    and abs(e["t"] - gap["t"]) < 0.04), None)
        if dup:
            if gap["width"] > dup["width"]: dup.update(gap)
        else:
            deduped_b.append(gap)
    print(f"  Gaps (deduped): {len(deduped_b)}")

    used_arcs_b: set[int] = set()
    doors_b:   list[dict] = []
    windows_b: list[dict] = []
    for gi, gap in enumerate(deduped_b):
        best_ai: int | None = None
        best_ad = float("inf")
        for ai, (cx_a, cy_a, r_a) in enumerate(arcs):
            d = math.hypot(cx_a - gap["cx"], cy_a - gap["cy"])
            if d < r_a * 2.5 and d < best_ad:
                best_ad, best_ai = d, ai
        wid_val = round(max(30.0, gap["width"]), 1)
        if best_ai is not None and best_ai not in used_arcs_b:
            used_arcs_b.add(best_ai)
            doors_b.append({"id": f"pd{gi}", "wallId": gap["wallId"],
                            "positionOnWall": gap["t"], "width": wid_val,
                            "height": 210.0, "type": "door", "doorType": "swing"})
        else:
            windows_b.append({"id": f"pw{gi}", "wallId": gap["wallId"],
                              "positionOnWall": gap["t"], "width": wid_val,
                              "height": 120.0, "sillHeight": 90.0, "type": "window"})

    print(f"  Doors: {len(doors_b)}  Windows: {len(windows_b)}")
    return doors_b, windows_b


# ── Pipeline stage runners ────────────────────────────────────────────────────

def run_stage(script: str, *args, cwd=BASE_DIR) -> tuple[bool, str]:
    """Run a pipeline stage script; return (success, stderr)."""
    cmd = [PYTHON, script, *args]
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    result = subprocess.run(
        cmd, cwd=str(cwd), env=env,
        capture_output=True, text=True, encoding="utf-8", timeout=300,
    )
    if result.returncode != 0:
        return False, result.stderr or result.stdout
    return True, ""


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "message": "PlanLift Pipeline API running"})


@app.route("/analyze", methods=["POST"])
def analyze():
    """
    Accept a floor plan image, run stages 1-3, return editor-ready scene data.
    Stage 4 (GLB extrusion) is skipped — the editor renders walls procedurally.
    Response: { walls, doors, windows, rooms }
    """
    if "file" not in request.files:
        return jsonify({"error": "No file uploaded"}), 400

    file = request.files["file"]
    if not file.filename:
        return jsonify({"error": "Empty filename"}), 400

    stem   = f"upload_{uuid.uuid4().hex[:8]}"
    suffix = Path(file.filename).suffix.lower() or ".png"

    input_path = BASE_DIR / "output" / "uploads" / f"{stem}{suffix}"
    input_path.parent.mkdir(parents=True, exist_ok=True)
    file.save(str(input_path))

    try:
        # Stage 1: preprocess
        ok, err = run_stage("stage1_preprocess.py", str(input_path))
        if not ok:
            return jsonify({"error": f"Stage 1 failed: {err}"}), 500

        preprocessed = BASE_DIR / "output" / "masks" / f"{stem}_preprocessed.png"
        if not preprocessed.exists():
            preprocessed = input_path

        # Stage 2: segment → masks.npz
        ok, err = run_stage("stage2_segment.py", str(preprocessed))
        if not ok:
            return jsonify({"error": f"Stage 2 failed: {err}"}), 500

        masks_npz = BASE_DIR / "output" / "masks" / f"{stem}_masks.npz"
        if not masks_npz.exists():
            stem2     = preprocessed.stem
            masks_npz = BASE_DIR / "output" / "masks" / f"{stem2}_masks.npz"
        if not masks_npz.exists():
            return jsonify({"error": "Stage 2 produced no masks.npz"}), 500

        # Stage 3 is no longer needed: opening detection now uses symbol recognition
        # directly on the preprocessed image instead of stage-3 bounding boxes.

        # Load wall mask for thickness estimation, wall extraction, and gap scanning
        data      = np.load(str(masks_npz), allow_pickle=True)
        wm        = data["walls"].astype(np.uint8)
        dist_t    = cv2.distanceTransform(wm, cv2.DIST_L2, 5)
        nz_t      = dist_t[dist_t > 0]
        thick     = float(np.median(nz_t) * 2) if len(nz_t) > 0 else 6.0

        # 1. Extract axis-aligned wall segments from the wall mask
        walls = walls_from_npz(masks_npz)

        # 2. Snap endpoints so walls meet cleanly at T/L junctions
        walls = snap_wall_endpoints(walls, thick)

        # 3. Use stage2's pre-computed openings mask to find doors/windows.
        doors, windows = openings_from_npz(masks_npz, walls, preprocessed, thick)

        # 4. Detect enclosed room polygons
        rooms = rooms_from_npz(masks_npz, thick)

        return jsonify({"walls": walls, "doors": doors, "windows": windows, "rooms": rooms})

    except subprocess.TimeoutExpired:
        return jsonify({"error": "Pipeline timed out (> 5 min)"}), 504
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        # Clean up uploaded file
        try:
            input_path.unlink(missing_ok=True)
        except Exception:
            pass


@app.route("/predict", methods=["POST"])
def predict_cubicasa():
    """
    FloorPlanTo3D-compatible endpoint for the CubiCasa pipeline.
    Accepts 'image' field (FloorPlanTo3D UI convention) or 'file'.
    Returns FloorPlanTo3D JSON: {points, classes, Width, Height, averageDoor}.
    """
    import PIL.Image as PILImage

    field = "image" if "image" in request.files else "file"
    if field not in request.files:
        return jsonify({"error": "No image file (field: 'image' or 'file')"}), 400

    file   = request.files[field]
    stem   = f"upload_{uuid.uuid4().hex[:8]}"
    suffix = Path(file.filename).suffix.lower() or ".png"

    input_path = BASE_DIR / "output" / "uploads" / f"{stem}{suffix}"
    input_path.parent.mkdir(parents=True, exist_ok=True)
    file.save(str(input_path))

    try:
        with PILImage.open(str(input_path)) as img:
            img_w, img_h = img.size
    except Exception:
        img_w, img_h = 1024, 1024

    try:
        ok, err = run_stage("stage1_preprocess.py", str(input_path))
        if not ok:
            return jsonify({"error": f"Stage 1 failed: {err}"}), 500

        preprocessed = BASE_DIR / "output" / "masks" / f"{stem}_preprocessed.png"
        if not preprocessed.exists():
            preprocessed = input_path

        ok, err = run_stage("stage2_segment.py", str(preprocessed))
        if not ok:
            return jsonify({"error": f"Stage 2 failed: {err}"}), 500

        masks_npz = BASE_DIR / "output" / "masks" / f"{stem}_masks.npz"
        if not masks_npz.exists():
            stem2     = preprocessed.stem
            masks_npz = BASE_DIR / "output" / "masks" / f"{stem2}_masks.npz"
        if not masks_npz.exists():
            return jsonify({"error": "Stage 2 produced no masks.npz"}), 500

        data    = np.load(str(masks_npz), allow_pickle=True)
        wm      = data["walls"].astype(np.uint8)
        dist_t  = cv2.distanceTransform(wm, cv2.DIST_L2, 5)
        nz_t    = dist_t[dist_t > 0]
        thick   = float(np.median(nz_t) * 2) if len(nz_t) > 0 else 6.0

        walls   = walls_from_npz(masks_npz)
        walls   = snap_wall_endpoints(walls, thick)
        doors, windows = openings_from_npz(masks_npz, walls, preprocessed, thick)

        # ── Convert PlanLift format → FloorPlanTo3D bbox format ──────────────
        points  = []
        classes = []
        wall_map = {w["id"]: w for w in walls}

        for w in walls:
            sx, sy = w["start"]["x"], w["start"]["y"]
            ex, ey = w["end"]["x"],   w["end"]["y"]
            t = w["thickness"] / 2
            if abs(ex - sx) >= abs(ey - sy):          # horizontal
                mid_y = (sy + ey) / 2
                points.append({"x1": min(sx, ex), "y1": mid_y - t,
                                "x2": max(sx, ex), "y2": mid_y + t})
            else:                                      # vertical
                mid_x = (sx + ex) / 2
                points.append({"x1": mid_x - t, "y1": min(sy, ey),
                                "x2": mid_x + t, "y2": max(sy, ey)})
            classes.append({"name": "wall"})

        door_sizes = []
        for opening in doors + windows:
            wall = wall_map.get(opening["wallId"])
            if not wall:
                continue
            sx, sy = wall["start"]["x"], wall["start"]["y"]
            ex, ey = wall["end"]["x"],   wall["end"]["y"]
            t_wall = wall["thickness"] / 2
            t_val  = opening["positionOnWall"]
            cx = sx + t_val * (ex - sx)
            cy = sy + t_val * (ey - sy)
            hw = opening["width"] / 2

            if abs(ex - sx) >= abs(ey - sy):          # horizontal wall
                points.append({"x1": cx - hw,     "y1": cy - t_wall,
                                "x2": cx + hw,     "y2": cy + t_wall})
            else:                                      # vertical wall
                points.append({"x1": cx - t_wall,  "y1": cy - hw,
                                "x2": cx + t_wall,  "y2": cy + hw})
            classes.append({"name": opening["type"]})
            if opening["type"] == "door":
                door_sizes.append(opening["width"])

        avg_door = float(np.mean(door_sizes)) if door_sizes else 0.0

        return jsonify({
            "points":      points,
            "classes":     classes,
            "Width":       img_w,
            "Height":      img_h,
            "averageDoor": avg_door,
        })

    except subprocess.TimeoutExpired:
        return jsonify({"error": "Pipeline timed out (> 5 min)"}), 504
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        try:
            input_path.unlink(missing_ok=True)
        except Exception:
            pass


@app.route("/analyze/json", methods=["POST"])
def analyze_json():
    """
    Same as /analyze but returns the scene.json instead of GLB —
    used by the editor to import walls/rooms/openings directly.
    """
    if "file" not in request.files:
        return jsonify({"error": "No file uploaded"}), 400

    file = request.files["file"]
    stem = f"upload_{uuid.uuid4().hex[:8]}"
    suffix = Path(file.filename).suffix.lower() or ".png"

    input_path = BASE_DIR / "output" / "uploads" / f"{stem}{suffix}"
    input_path.parent.mkdir(parents=True, exist_ok=True)
    file.save(str(input_path))

    try:
        ok, err = run_stage("stage1_preprocess.py", str(input_path))
        if not ok:
            return jsonify({"error": f"Stage 1: {err}"}), 500

        preprocessed = BASE_DIR / "output" / "masks" / f"{stem}_preprocessed.png"
        if not preprocessed.exists():
            preprocessed = input_path

        ok, err = run_stage("stage2_segment.py", str(preprocessed))
        if not ok:
            return jsonify({"error": f"Stage 2: {err}"}), 500

        masks_npz = BASE_DIR / "output" / "masks" / f"{preprocessed.stem}_masks.npz"
        if not masks_npz.exists():
            return jsonify({"error": "No masks.npz produced"}), 500

        ok, err = run_stage("stage3_vectorize.py", str(masks_npz))
        if not ok:
            return jsonify({"error": f"Stage 3: {err}"}), 500

        for p in (BASE_DIR / "output" / "vectors").glob("*_scene.json"):
            with open(p) as f:
                return jsonify(json.load(f))

        return jsonify({"error": "No scene.json produced"}), 500

    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        try:
            input_path.unlink(missing_ok=True)
        except Exception:
            pass


# ── Isaac Sim integration ─────────────────────────────────────────────────────

def _read_env_var(name: str) -> str:
    """Return env var value, falling back to a .env file next to api_server.py."""
    val = os.environ.get(name, "")
    if val:
        return val
    env_file = BASE_DIR / ".env"
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            if k.strip() == name:
                return v.strip().strip('"').strip("'")
    return ""


def _isaac_worker(task_id: str, glb_path: Path, floorplan_path: Path = None,
                   world_scale: float = 0.0, floors: list = None, rooms: list = None) -> None:
    def set_status(status: str, message: str = "") -> None:
        with _isaac_lock:
            _isaac_tasks[task_id] = {"status": status, "message": message}

    script_path = None
    usd_path    = None

    try:
        set_status("checking", "Checking installation…")

        # ── Resolve paths ────────────────────────────────────────────────────
        # ISAAC_SIM_PATH supports three layouts:
        #   1. pip-installed: points at venv python.exe → launch via "python -m isaacsim"
        #   2. standalone kit.exe: launch directly
        #   3. isaac-sim.bat: derive kit.exe from parent/kit/
        isaac_exe = _read_env_var("ISAAC_SIM_PATH")
        if not isaac_exe:
            set_status("error", "ISAAC_SIM_PATH not set in .env file.")
            return
        isaac_path = Path(isaac_exe)
        use_pip_launch = False  # True → "python -m isaacsim", False → kit.exe directly
        kit_exe = None

        if isaac_path.name.startswith("python") and isaac_path.exists():
            # pip-installed: ISAAC_SIM_PATH = venv/Scripts/python.exe
            # Verify isaacsim is installed in this venv
            isaacsim_pkg = isaac_path.parent.parent / "Lib" / "site-packages" / "isaacsim"
            if isaacsim_pkg.is_dir():
                use_pip_launch = True
                kit_exe = isaac_path
                print(f"[PlanLift] Isaac Sim pip layout: {isaacsim_pkg}", flush=True)
            else:
                set_status("error", f"isaacsim package not found at {isaacsim_pkg}")
                return
        elif isaac_path.name == "kit.exe" and isaac_path.exists():
            kit_exe = isaac_path
        else:
            for candidate in [
                isaac_path.parent / "kit" / "kit.exe",
                isaac_path / "kit" / "kit.exe",
                isaac_path.parent / "kit.exe",
            ]:
                if candidate.exists():
                    kit_exe = candidate
                    break
        if not kit_exe:
            set_status("error", f"kit.exe / python not found at {isaac_path}")
            return

        kit_app_dir = Path(_read_env_var("KIT_APP_PATH") or r"D:\PlanLift\Kit_App")
        kit_file    = kit_app_dir / "apps" / "planlift_timelapse.kit"
        ext_folder  = kit_app_dir / "exts"
        startup_py  = kit_app_dir / "startup.py"
        if not kit_file.exists():
            set_status("error", f"Kit file not found: {kit_file}")
            return

        # ── Prepare GLB → USD paths ──────────────────────────────────────────
        glb_uri  = "file:///" + glb_path.as_posix()
        usd_path = glb_path.with_suffix(".usd")
        usd_uri  = "file:///" + usd_path.as_posix()
        usd_abs  = str(usd_path)           # absolute Windows path for USD reference

        # ── Write injection script (second --exec, runs inside the Kit process) ─
        # Waits for the timelapse stage (opened by planlift.ui_setup) to fully load,
        # converts the GLB to USD using the Kit asset converter, then adds it as a
        # USD reference at /PlanLift_Furniture so it appears in the existing scene.
        script_path = glb_path.parent / f"inject_{task_id}.py"
        # USD reference paths must use forward slashes on all platforms
        usd_ref = usd_path.as_posix()

        debug_log = (glb_path.parent / "inject_debug.txt").as_posix()

        # ── Server-side GLB annotation: find glass material indices ──────────
        # Parse the GLB JSON chunk in the Flask process (no pxr needed) to
        # discover which GLTF material indices are transparent (alphaMode=BLEND,
        # baseColorFactor[3] < 0.95). Bake these directly into the inject script
        # so Kit doesn't need to guess — it gets ground-truth from the source GLB.
        def _glb_glass_material_indices(path: Path):
            import struct, json as _json
            try:
                data = path.read_bytes()
                if data[:4] != b'glTF':
                    return []
                # GLB: 12-byte header, then chunk0: 4-byte len + 4-byte type + JSON
                chunk_len = struct.unpack_from('<I', data, 12)[0]
                gltf = _json.loads(data[20:20 + chunk_len])
                indices = []
                for i, mat in enumerate(gltf.get('materials', [])):
                    alpha_mode = mat.get('alphaMode', 'OPAQUE')
                    pbr = mat.get('pbrMetallicRoughness', {})
                    base = pbr.get('baseColorFactor', [1, 1, 1, 1])
                    alpha = base[3] if len(base) == 4 else 1.0
                    # Window glass has opacity≈0.2 → alpha≈0.2.
                    # Room floors use opacity=0.5 → alpha=0.5 (not glass).
                    # Threshold 0.35 catches glass but not semi-transparent floors.
                    if alpha_mode == 'BLEND' and alpha < 0.35:
                        indices.append(i)
                return indices
            except Exception as exc:
                print(f"[PlanLift] GLB parse warning: {exc}", flush=True)
                return []

        glass_indices = _glb_glass_material_indices(glb_path)
        glass_count   = len(glass_indices)
        print(f"[PlanLift] server-side GLB parse: {glass_count} glass material(s) at GLTF indices {glass_indices}", flush=True)

        # Write sidecar so extension.py can map GLTF indices → USD material names
        import json as _json_mod
        _glass_json = glb_path.parent / (glb_path.stem + ".glass.json")
        _glass_json.write_text(_json_mod.dumps({"glass_indices": glass_indices}), encoding="utf-8")
        print(f"[PlanLift] wrote glass annotation: {_glass_json}", flush=True)

        script_body = f"""\
# PLANLIFT-INJECT-V6-AGENTCTX glass_count={glass_count} indices={glass_indices}
import asyncio, os, sys, threading, time as _t, carb, omni.usd, omni.kit.app
from pxr import UsdGeom

GLB_URI  = "{glb_uri}"
USD_URI  = "{usd_uri}"
USD_REF  = "{usd_ref}"
GLB_PATH = "{glb_path.as_posix()}"   # plain path for asset converter
USD_PATH = "{usd_path.as_posix()}"   # plain path for asset converter
FLOORPLAN_PATH = "{floorplan_path.as_posix() if floorplan_path else ''}"
# World units per floor-plan-image pixel — the SAME `S` editor.html's
# renderFloorLevel/pos() used to place every wall/door/pillar (mpp when
# calibrated, else its fixed-20-unit-box fallback). 0 means the browser
# didn't send one (older client) — callers fall back to a bbox guess.
WORLD_SCALE = {world_scale if world_scale else 0}
# One entry per floor: {{"idx":int,"label":str,"yBase":float,"image":path}}.
# Empty when the browser sent no floors_meta (older client) — the
# floor-plane loop below then falls back to the single FLOORPLAN_PATH pair
# above as a synthetic one-floor list. Written by Flask directly from the
# same `floors` list saved into {task_id}_floors.json for Floor Plan View.
FLOORS_META = {json.dumps([{"idx": f["idx"], "label": f["label"], "yBase": f["yBase"], "image": Path(f["image"]).as_posix(), "wallHeightM": f.get("wallHeightM")} for f in (floors or [])])}
# Labeled rooms -> real-world (x,y,z), for the Isaac Sim AI Agent panel to
# resolve a typed room name against. Empty when the browser sent no
# rooms_data (nothing labeled yet, or an older client) -- the agent panel
# just has no rooms to resolve against until a scene with labels is opened.
ROOMS_DATA = {json.dumps(rooms or [])}
_DEBUG  = r"{debug_log}"

# Ground-truth glass annotation from server-side GLB binary parse.
# GLASS_INDICES: GLTF material indices with alphaMode=BLEND + baseColorFactor[3] < 0.35
# The GLTF→USD converter names USD materials "materialN" (0-indexed).
GLASS_COUNT   = {glass_count}    # 0 means no glass in GLB
GLASS_INDICES = {glass_indices}  # e.g. [2, 3, 4]

def _log(msg):
    import datetime
    line = datetime.datetime.now().strftime("%H:%M:%S") + " " + msg + "\\n"
    try:
        with open(_DEBUG, "a", encoding="utf-8") as f:
            f.write(line)
    except Exception:
        pass
    print("[PlanLift] " + msg, flush=True)

_log("script loaded by Kit --exec")
_app = omni.kit.app.get_app()

# ── Async part: runs inside Kit's event loop ──────────────────────────────────
async def _convert_and_inject():
    ctx = omni.usd.get_context()
    stage = ctx.get_stage()
    if not stage:
        _log("ERROR: no stage for injection")
        print("[PlanLift] injection-failed: no stage", flush=True)
        return
    _log(f"GLB_PATH={{GLB_PATH}} exists={{os.path.exists(GLB_PATH)}} "
         f"size={{os.path.getsize(GLB_PATH) if os.path.exists(GLB_PATH) else -1}}")
    _log("converting GLB to USD...")
    print("[PlanLift] converting-glb", flush=True)
    try:
        import omni.kit.asset_converter as ac
        task = ac.get_instance().create_converter_task(
            GLB_PATH, USD_PATH, None, ac.AssetConverterContext())
        ok = await task.wait_until_finished()
        if not ok:
            err = None
            try:
                err = task.get_error_message()
            except Exception:
                pass
            _log(f"ERROR: converter returned False, error_message={{err!r}}")
            print(f"[PlanLift] injection-failed: converter returned False: {{err}}", flush=True)
            return
        _log(f"GLB conversion OK -> USD_PATH={{USD_PATH}} "
             f"exists={{os.path.exists(USD_PATH)}} "
             f"size={{os.path.getsize(USD_PATH) if os.path.exists(USD_PATH) else -1}}")
    except Exception as e:
        _log(f"ERROR: converter: {{e}}")
        print(f"[PlanLift] injection-failed: {{e}}", flush=True)
        return

    # ── Pre-flatten Xform > Looks + inner Mesh → flat Mesh ──────────────────────
    # Strategy: open the converted USD on a standalone stage, first clear ALL
    # instanceable flags so GetChildren() returns real authored children (not
    # prototype proxies), then flatten every Xform+Looks+inner-Mesh prim.
    # Changes are written to the root layer and saved before AddReference.
    _fl_paths = []   # paths of flattened prims — used by glass patcher below
    try:
        from pxr import Usd as _Usd, UsdGeom as _UsdGeom, UsdShade as _UsdShade
        _fl_st = _Usd.Stage.Open(USD_PATH)
        if not _fl_st:
            _log("ERROR: pre-flatten: could not open stage")
        else:
            _fl_rl = _fl_st.GetRootLayer()
            _GATTRS = ["points","faceVertexCounts","faceVertexIndices",
                       "normals","doubleSided","subdivisionScheme","orientation","extent"]

            # Step A: collect instanceable prim paths (read-only — proxy names OK)
            _inst_paths = [p.GetPath() for p in _fl_st.Traverse() if p.IsInstance()]
            _log(f"pre-flatten: {{len(_inst_paths)}} instanceable prims")

            # Step B: clear instanceable on all of them → USD recomposes
            # children now accessible at their authored paths
            with _Usd.EditContext(_fl_st, _fl_rl):
                for _ip in _inst_paths:
                    _ipr = _fl_st.GetPrimAtPath(_ip)
                    if _ipr and _ipr.IsValid():
                        _ipr.SetInstanceable(False)

            # Step C: collect Xform+Looks+inner-Mesh candidates
            # instanceable=False now so GetChildren() returns real children
            _fcands = []
            for _fp in _fl_st.Traverse():
                if _fp.GetTypeName() != "Xform":
                    continue
                _ckids = {{c.GetName(): c.GetTypeName() for c in _fp.GetChildren()}}
                _imname = next((n for n,t in _ckids.items() if t == "Mesh"), None)
                if _imname and "Looks" in _ckids:
                    _fcands.append((_fp.GetPath(), _imname))
            _log(f"pre-flatten: {{len(_fcands)}} Xform+Looks+Mesh candidates")

            # Step D: flatten each candidate
            with _Usd.EditContext(_fl_st, _fl_rl):
                for (_fpath, _imname) in _fcands:
                    _fxp    = _fl_st.GetPrimAtPath(_fpath)
                    _finner = _fl_st.GetPrimAtPath(_fpath.AppendChild(_imname))
                    if not (_fxp and _fxp.IsValid() and _finner and _finner.IsValid()):
                        _log(f"  pre-flatten: skip {{_fpath}}")
                        continue
                    # Copy geometry attrs inner → outer
                    for _an in _GATTRS:
                        _sa = _finner.GetAttribute(_an)
                        if _sa and _sa.IsValid():
                            _sv = _sa.Get()
                            if _sv is not None:
                                try:
                                    _fxp.CreateAttribute(_an,_sa.GetTypeName(),custom=False).Set(_sv)
                                except Exception:
                                    pass
                    for _pv in _UsdGeom.PrimvarsAPI(_finner).GetPrimvars():
                        try:
                            _pval = _pv.Get()
                            if _pval is None: continue
                            _npv = _UsdGeom.PrimvarsAPI(_fxp).CreatePrimvar(
                                _pv.GetPrimvarName(),_pv.GetTypeName(),_pv.GetInterpolation())
                            _npv.Set(_pval)
                            if _pv.IsIndexed(): _npv.SetIndices(_pv.GetIndices())
                        except Exception:
                            pass
                    # Copy material binding inner → outer (via high-level API)
                    _ib = _UsdShade.MaterialBindingAPI(_finner).GetDirectBinding().GetMaterialPath()
                    if _ib and str(_ib) != "":
                        _imat = _fl_st.GetPrimAtPath(_ib)
                        if _imat and _imat.IsValid():
                            _UsdShade.MaterialBindingAPI(_fxp).Bind(_UsdShade.Material(_imat))
                    # Promote Xform → Mesh; deactivate inner Mesh
                    _fxp.SetTypeName("Mesh")
                    _finner.SetActive(False)
                    _fl_paths.append(str(_fpath))
                    _log(f"  pre-flatten: {{_fpath}}")

            if _fcands:
                _fl_rl.Save()
                _log(f"pre-flatten: saved {{len(_fcands)}} prims")
    except Exception as _fe:
        _log(f"ERROR: pre-flatten: {{_fe}}")

    # ── Apply OmniGlass to glass panes by GLTF material index ────────────
    # GLASS_INDICES is baked in from the server's GLB binary parse.
    # The GLTF→USD converter names materials "materialN" (0-indexed).
    # We patch the converted USD *before* AddReference so the live stage
    # picks up OmniGlass automatically when the reference is loaded.
    _log(f"glass: server annotated {{len(GLASS_INDICES)}} glass indices: {{GLASS_INDICES}}")
    try:
        from pxr import Usd, UsdShade, Sdf
        conv_stage = Usd.Stage.Open(USD_PATH)
        if not conv_stage:
            _log(f"ERROR: glass: could not open USD at {{USD_PATH}}")
            raise RuntimeError("could not open converted USD")

        all_prims = list(conv_stage.Traverse())
        mat_names = [p.GetName() for p in all_prims if p.GetTypeName() == "Material"]
        _log(f"USD: {{len(all_prims)}} prims, {{len(mat_names)}} materials: {{mat_names}}")

        # Find glass material prims by index → name "materialN"
        glass_mat_paths = set()
        for _gi in GLASS_INDICES:
            _tname = f"material{{_gi}}"
            for _p in all_prims:
                if _p.GetTypeName() == "Material" and _p.GetName() == _tname:
                    glass_mat_paths.add(_p.GetPath())
                    _log(f"  glass[{{_gi}}] -> {{_p.GetPath()}}")
                    break
            else:
                _log(f"  glass[{{_gi}}] -> 'material{{_gi}}' NOT FOUND (all: {{mat_names}})")

        # Also detect by name prefix "PlanLift_Glass" — set when Three.js
        # material.name = 'PlanLift_Glass' and GLTFExporter preserves it.
        for _p in all_prims:
            if _p.GetTypeName() == "Material" and _p.GetName().startswith("PlanLift_Glass"):
                if _p.GetPath() not in glass_mat_paths:
                    glass_mat_paths.add(_p.GetPath())
                    _log(f"  glass by name -> {{_p.GetPath()}}")

        _log(f"glass: matched {{len(glass_mat_paths)}} USD materials")

        if glass_mat_paths or _fl_paths:
            gm_path = Sdf.Path("/PlanLift_GlassMat")
            gm = UsdShade.Material.Define(conv_stage, gm_path)
            gs = UsdShade.Shader.Define(conv_stage, str(gm_path) + "/Shader")
            gs.SetSourceAsset(Sdf.AssetPath("OmniGlass.mdl"), "mdl")
            gs.SetSourceAssetSubIdentifier("OmniGlass", "mdl")
            gs.CreateInput("glass_color",        Sdf.ValueTypeNames.Color3f).Set((0.9, 0.95, 1.0))
            gs.CreateInput("glass_ior",          Sdf.ValueTypeNames.Float).Set(1.491)
            gs.CreateInput("frosting_roughness", Sdf.ValueTypeNames.Float).Set(0.0)
            gs.CreateInput("enable_opacity", Sdf.ValueTypeNames.Bool).Set(True)
            gs.CreateInput("cutout_opacity",        Sdf.ValueTypeNames.Float).Set(0.5)
            gm.CreateSurfaceOutput("mdl").ConnectToSource(gs.ConnectableAPI(), "out")
            gm.CreateDisplacementOutput("mdl").ConnectToSource(gs.ConnectableAPI(), "out")
            gm.CreateVolumeOutput("mdl").ConnectToSource(gs.ConnectableAPI(), "out")

            rebound = 0
            for p in all_prims:
                binding = UsdShade.MaterialBindingAPI(p)
                if binding.GetDirectBinding().GetMaterialPath() in glass_mat_paths:
                    binding.Bind(gm)
                    rebound += 1
                    _log(f"  OmniGlass bound -> {{p.GetPath()}}")

            conv_stage.GetRootLayer().Save()
            _log(f"glass DONE: OmniGlass patched into {{rebound}} prim(s) in USD")
        else:
            _log("glass: no glass material paths matched — OmniGlass not applied")
    except Exception as e:
        _log(f"ERROR: glass: {{e}}")

    # Path Tracing is now enabled by the extension (deferred until after
    # furniture + glass setup) to avoid crashing Kit's hang detector during
    # heavy shader compilation at startup.

    try:
        from pxr import Gf, UsdGeom
        xform = UsdGeom.Xform.Define(stage, "/PlanLift_Furniture")
        prim  = xform.GetPrim()
        prim.GetReferences().AddReference(USD_REF)

        # GLB is Y-up; the Kit scene is Z-up.
        # Rotate -90° around X so Y-up becomes Z-up.
        xform.AddRotateXOp().Set(90.0)
        _log("AddReference + axis correction OK — furniture injected!")
        print("[PlanLift] model-injected", flush=True)
    except Exception as e:
        _log(f"ERROR: AddReference: {{e}}")
        print(f"[PlanLift] injection-failed: {{e}}", flush=True)

    # ── 2D floorplan image(s) as horizontal floor-plane textures ──────────
    # One plane per floor (FLOORS_META), each at that floor's own yBase, so
    # Floor Plan View and the camera/zone tools can resolve position and
    # height against the RIGHT floor instead of one shared ground plane.
    # Floor idx 0 keeps the original unsuffixed prim path for backward
    # compatibility with any code still looking for a single
    # /PlanLift_FloorPlanImage; floors 1+ get an indexed sibling.
    _floors_list = FLOORS_META if FLOORS_META else (
        [{{"idx": 0, "label": "Floor 1", "yBase": 0.0, "image": FLOORPLAN_PATH}}] if FLOORPLAN_PATH else []
    )
    for _fp_entry in _floors_list:
        _fp_idx   = _fp_entry.get("idx", 0)
        _fp_ybase = float(_fp_entry.get("yBase", 0.0))
        _fp_img   = _fp_entry.get("image", "")
        if not _fp_img:
            continue
        try:
            from pxr import Gf as _Gf2, Sdf as _Sdf2, Usd as _Usd2
            from pxr import UsdGeom as _UG2, UsdShade as _US2

            # Exact footprint: editor.html's pos(px,py) = [px*S-FW/2, py*S-FD/2]
            # places every wall/door/pillar with the image CENTER at world
            # origin and 1 pixel = WORLD_SCALE world units — so the plane
            # should be exactly (image_w*WORLD_SCALE) x (image_h*WORLD_SCALE),
            # centered at origin. This is the actual pixel-perfect footprint,
            # not an approximation, which is what a geometry-bbox guess (the
            # fallback below) can never quite be — the built geometry's own
            # bbox is normally SMALLER than the full image (any blank margin
            # around the building isn't covered by walls/furniture), so
            # sizing the plane from that bbox instead of the true image
            # extent is exactly what made camera markers in Floor Plan View
            # read a little off from where they actually sit in 3D.
            # WORLD_SCALE*pixels gives a REAL-WORLD METERS footprint, but
            # this stage's own geometry (the GLB-converted walls/furniture)
            # lands at 1 unit = 1cm with no compensating scale anywhere in
            # this conversion — confirmed live: a wall's own Translate read
            # back as (461.56, 274.32, -386.93) in a floor plan whose real
            # footprint is ~15.7x11.8m, a clean ~100x ratio. Without scaling
            # this plane to match, it renders ~100x too small relative to
            # the actual walls — the bbox-fallback branch below does NOT
            # need this, since it reads real stage geometry directly,
            # already in this same (correct) stage-unit scale.
            _STAGE_UNITS_PER_METER = 100.0
            _fp_cx = _fp_cy = _fp_hw = _fp_hd = None
            if WORLD_SCALE and WORLD_SCALE > 0:
                try:
                    from PIL import Image as _FPImg
                    with _FPImg.open(_fp_img) as _fpim:
                        _fp_iw, _fp_ih = _fpim.size
                    _fp_cx, _fp_cy = 0.0, 0.0
                    _fp_hw = (_fp_iw * WORLD_SCALE) / 2.0 * _STAGE_UNITS_PER_METER
                    _fp_hd = (_fp_ih * WORLD_SCALE) / 2.0 * _STAGE_UNITS_PER_METER
                    _log(f"FloorPlanImage[{{_fp_idx}}]: exact footprint from WORLD_SCALE={{WORLD_SCALE}} "
                         f"image={{_fp_iw}}x{{_fp_ih}}")
                except Exception as _fpwe:
                    _log(f"FloorPlanImage[{{_fp_idx}}]: WORLD_SCALE path failed ({{_fpwe}}), falling back to bbox")

            if _fp_hw is None:
                # Fallback (older client that didn't send world_scale, or the
                # exact path above failed): approximate from the converted
                # USD's own geometry bounding box, same as before. Only
                # meaningful for a single-floor scene — a whole-stage bbox
                # guess can't tell floors apart, so in practice this only
                # fires for the idx-0/legacy case.
                _fp_stg = _Usd2.Stage.Open(USD_PATH)
                _fp_bc  = _UG2.BBoxCache(_Usd2.TimeCode.Default(),
                             [_UG2.Tokens.default_, _UG2.Tokens.render],
                             useExtentsHint=True)
                _fp_rng = _Gf2.Range3d()
                for _fp_p in _fp_stg.Traverse():
                    if _fp_p.IsA(_UG2.Boundable):
                        try:
                            _fp_bb = _fp_bc.ComputeWorldBound(_fp_p)
                            _fp_rng = _Gf2.Range3d.GetUnion(_fp_rng, _fp_bb.ComputeAlignedRange())
                        except Exception:
                            pass
                if _fp_rng.IsEmpty():
                    _log("FloorPlan: bbox empty, using 20x20 default")
                    _fp_cx, _fp_cy, _fp_hw, _fp_hd = 0.0, 0.0, 10.0, 10.0
                else:
                    _fp_mn = _fp_rng.GetMin()
                    _fp_mx = _fp_rng.GetMax()
                    # RotateX+90 maps: GLB-X->Stage-X, GLB-Z->Stage-(-Y), GLB-Y->Stage-Z
                    _fp_cx = (_fp_mn[0] + _fp_mx[0]) / 2.0
                    _fp_cy = -((_fp_mn[2] + _fp_mx[2]) / 2.0)
                    _fp_hw = abs(_fp_mx[0] - _fp_mn[0]) / 2.0 * 1.08
                    _fp_hd = abs(_fp_mx[2] - _fp_mn[2]) / 2.0 * 1.08
            # _fp_ybase (each floor's yBase, from editor.html's build3D) is
            # real METERS regardless of which branch above ran — the bbox
            # branch's X/Y came pre-scaled from real stage geometry, but Z
            # here is a SEPARATE value that still needs the same conversion.
            _fp_z  = (_fp_ybase - 0.005) * _STAGE_UNITS_PER_METER
            _fpp   = "/PlanLift_FloorPlanImage" if _fp_idx == 0 else f"/PlanLift_FloorPlanImage_{{_fp_idx}}"
            _fmesh = _UG2.Mesh.Define(stage, _fpp)
            _fmesh.CreatePointsAttr().Set([
                _Gf2.Vec3f(_fp_cx - _fp_hw, _fp_cy - _fp_hd, _fp_z),
                _Gf2.Vec3f(_fp_cx + _fp_hw, _fp_cy - _fp_hd, _fp_z),
                _Gf2.Vec3f(_fp_cx + _fp_hw, _fp_cy + _fp_hd, _fp_z),
                _Gf2.Vec3f(_fp_cx - _fp_hw, _fp_cy + _fp_hd, _fp_z),
            ])
            _fmesh.CreateFaceVertexCountsAttr().Set([4])
            _fmesh.CreateFaceVertexIndicesAttr().Set([0, 1, 2, 3])
            _fmesh.CreateNormalsAttr().Set([_Gf2.Vec3f(0, 0, 1)] * 4)
            _fmesh.SetNormalsInterpolation(_UG2.Tokens.faceVarying)
            _fst = _UG2.PrimvarsAPI(_fmesh.GetPrim()).CreatePrimvar(
                "st", _Sdf2.ValueTypeNames.TexCoord2fArray, _UG2.Tokens.faceVarying)
            _fst.Set([_Gf2.Vec2f(0,0), _Gf2.Vec2f(1,0), _Gf2.Vec2f(1,1), _Gf2.Vec2f(0,1)])
            _fmat  = _US2.Material.Define(stage, _fpp + "/Mat")
            _fsh   = _US2.Shader.Define(stage, _fpp + "/Mat/Sh")
            _fsh.SetSourceAsset(_Sdf2.AssetPath("OmniPBR.mdl"), "mdl")
            _fsh.SetSourceAssetSubIdentifier("OmniPBR", "mdl")
            _ftex  = "file:///" + _fp_img
            _fsh.CreateInput("diffuse_texture",               _Sdf2.ValueTypeNames.Asset).Set(_Sdf2.AssetPath(_ftex))
            _fsh.CreateInput("metallic_constant",             _Sdf2.ValueTypeNames.Float).Set(0.0)
            _fsh.CreateInput("reflection_roughness_constant", _Sdf2.ValueTypeNames.Float).Set(1.0)
            _fmat.CreateSurfaceOutput("mdl").ConnectToSource(_fsh.ConnectableAPI(), "out")
            _fmat.CreateDisplacementOutput("mdl").ConnectToSource(_fsh.ConnectableAPI(), "out")
            _fmat.CreateVolumeOutput("mdl").ConnectToSource(_fsh.ConnectableAPI(), "out")
            _US2.MaterialBindingAPI(_fmesh.GetPrim()).Bind(_fmat)
            _log(f"FloorPlanImage[{{_fp_idx}}]: {{_fp_hw*2:.1f}}x{{_fp_hd*2:.1f}} stage units plane at "
                 f"({{_fp_cx:.1f}}, {{_fp_cy:.1f}}, {{_fp_z:.3f}})")
        except Exception as _fpe:
            _log(f"ERROR: FloorPlanImage[{{_fp_idx}}]: {{_fpe}}")

    # ── Floor Plan View image pinning ────────────────────────────────────
    # Without this call, planlift_floorview's 2D window has no way to know
    # which floor-plan image belongs to THIS launch — its own on-update loop
    # falls back to "whichever *_floorplan.png in isaac_tmp/ has the newest
    # mtime" (see its _on_update), which silently shows a stale/unrelated
    # floor plan from a previous run whenever any leftover PNG in that
    # never-cleaned folder (or a file-copy timestamp quirk) ends up looking
    # newer than this run's own file. Pin it explicitly with the floor data
    # this script already built _floors_list from above (for the 3D
    # floor-plane textures) — floorKey uses the same "floor_N index + 1"
    # string convention set_floor_images/_floor_z_ranges both already
    # document, so this works whether _floors_list came from FLOORS_META
    # (multi-floor) or the single-image legacy fallback.
    try:
        from planlift_floorview.extension import set_floor_images as _set_floor_imgs
        _floor_image_map = {{str(_e.get("idx", 0) + 1): _e.get("image", "") for _e in _floors_list if _e.get("image")}}
        _set_floor_imgs(_floor_image_map)
        _log(f"Floor Plan View image(s) pinned — {{len(_floor_image_map)}} floor(s)")
    except Exception as _fie:
        _log(f"ERROR: set_floor_images failed: {{_fie}}")

    # ── AI Agent panel context ──────────────────────────────────────────
    # Hands the labeled-room list (ROOMS_DATA, already real-world wx/wy/wz
    # — see editor.html's _buildRoomsDataForAgent) to planlift_ui_setup's
    # AI Agent panel, which creates itself on first call. img_w/img_h are
    # passed as 0 — the panel's actual room resolution uses each room's
    # own precomputed wx/wy/wz directly (see set_agent_context's own
    # docstring), never re-derives them from image pixel size, so these
    # two are vestigial/kept only for the function's documented signature.
    try:
        from planlift_ui_setup.extension import set_agent_context as _set_agent_ctx
        _set_agent_ctx(ROOMS_DATA, WORLD_SCALE, 0, 0)
        _log(f"AI Agent panel context set — {{len(ROOMS_DATA)}} room(s)")
    except Exception as _ae:
        _log(f"ERROR: set_agent_context failed: {{_ae}}")

# ── Thread part: uses real time.sleep() — never touches Kit's event loop ──────
def _inject_worker(kit_loop):
    _log("worker thread started — sleeping 15s for Kit startup")
    _t.sleep(15)

    ctx = omni.usd.get_context()
    _log("polling stage (max 60s real time)...")
    deadline = _t.monotonic() + 60
    while _t.monotonic() < deadline:
        _t.sleep(1)
        try:
            stage = ctx.get_stage()
            if stage:
                try:
                    _loading = ctx.is_stage_loading()
                except AttributeError:
                    _loading = False  # API varies by Kit version
                if not _loading:
                    _log(f"stage ready!")
                    break
        except Exception:
            pass
    else:
        _log("WARNING: stage poll timed out (60s), injecting anyway")

    # Schedule the async converter back onto Kit's event loop and wait for it
    future = asyncio.run_coroutine_threadsafe(_convert_and_inject(), kit_loop)
    try:
        future.result(timeout=120)
    except Exception as e:
        _log(f"ERROR: async part failed: {{e}}")
        print(f"[PlanLift] injection-failed: {{e}}", flush=True)

# ── Kickoff: fires on first Kit update tick ───────────────────────────────────
def _kickoff(e):
    _log("kickoff fired — capturing event loop and launching worker thread")
    sys._planlift_sub.unsubscribe()
    loop = asyncio.get_event_loop()
    sys._planlift_loop   = loop
    sys._planlift_thread = threading.Thread(
        target=_inject_worker, args=(loop,), daemon=True)
    sys._planlift_thread.start()

sys._planlift_sub = _app.get_update_event_stream().create_subscription_to_pop(
    _kickoff, name="planlift_inject_kickoff")
_log(f"subscription created: {{sys._planlift_sub}}")
"""
        script_path.write_text(script_body, encoding="utf-8")

        # ── Launch the Kit app (mirrors launch.bat + adds injection script) ──
        set_status("launching", "Launching Kit application…")
        if use_pip_launch:
            # pip-installed Isaac Sim 6.0+: use isaacsim.exe entry point.
            # isaacsim's main() extracts kit_file from args[0], but only
            # strips it when found via search dirs. Pass remaining flags
            # AFTER the kit file so they're appended correctly.
            isaacsim_exe = kit_exe.parent / "isaacsim.exe"  # venv/Scripts/isaacsim.exe
            cmd = [
                str(isaacsim_exe),
                str(kit_file),
                "--ext-folder", str(ext_folder),
                "--enable",     "planlift.ui_setup",
                "--exec",       str(startup_py),
                "--exec",       str(script_path),
            ]
        else:
            # Standalone Isaac Sim 5.x: launch kit.exe directly
            cmd = [
                str(kit_exe),
                str(kit_file),
                "--ext-folder", str(ext_folder),
                "--enable",     "planlift.ui_setup",
                "--exec",       str(startup_py),
                "--exec",       str(script_path),
            ]
        launch_env = os.environ.copy()
        launch_env["OMNI_KIT_ACCEPT_EULA"] = "YES"
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=launch_env,
        )

        # ── Continuously stream ALL Kit stdout to server console ────────────
        # A background thread reads every line Kit prints and mirrors it with
        # [Kit] prefix so the full Isaac Sim log appears in the server console.
        # A shared queue lets the main thread scan for PlanLift sentinels
        # without racing with the reader.
        import queue as _queue
        _q: _queue.Queue = _queue.Queue()

        def _kit_reader():
            try:
                for raw in proc.stdout:
                    line = raw.rstrip()
                    print(f"[Kit] {line}", flush=True)
                    _q.put(line)
            except Exception:
                pass
            finally:
                _q.put(None)   # sentinel: reader done

        threading.Thread(target=_kit_reader, daemon=True).start()

        set_status("waiting",
            "Kit app loading — shader compilation takes 2–5 min on first launch. "
            "'Not Responding' in the title bar is normal. Please wait…")

        deadline = time.time() + 900   # 15-min hard cap
        injected = False

        while time.time() < deadline:
            try:
                line = _q.get(timeout=1)
            except _queue.Empty:
                if proc.poll() is not None:
                    break
                continue

            if line is None:   # reader finished (Kit exited)
                break

            low = line.lower()
            if "[planlift] converting-glb" in low:
                set_status("converting", "Converting GLB → USD inside Kit…")
            elif "[planlift] model-injected" in low:
                injected = True
                break
            elif "[planlift] injection-failed" in low:
                set_status("error", f"Injection failed: {line}")
                return

        if proc.poll() is not None and proc.returncode not in (0, None):
            set_status("error", f"Kit app exited with code {proc.returncode}.")
            return

        if injected:
            set_status("done", "Furniture model loaded into the timelapse scene!")
        else:
            set_status("done",
                "Kit app is open. Furniture is loading into the scene…")

    except Exception as exc:
        set_status("error", str(exc))
    finally:
        def _cleanup():
            time.sleep(3600)   # keep USD for 1 h so Kit can reference it
            # floorplan_path/per-floor floorplan PNGs/{task_id}_floors.json
            # are NOT included here on purpose: Floor Plan View keeps
            # polling isaac_tmp for "the newest *_floors.json" (or
            # *_floorplan.png for an old session with no manifest) for as
            # long as that Isaac Sim session runs, which is routinely much
            # longer than this 1h window. Deleting them on the same timer as
            # the transient glb/usd/script left the newest-by-mtime scan
            # falling back to whatever older files happened to still be
            # sitting on disk (e.g. orphaned ones from a past session whose
            # own cleanup thread never got to run) — showing a stale
            # floorplan even though the current session's model was correct.
            for p in filter(None, [glb_path, script_path, usd_path]):
                try:
                    p.unlink(missing_ok=True)
                except Exception:
                    pass
        threading.Thread(target=_cleanup, daemon=True).start()


def _dedup_glass_meshes(glb_path: Path) -> None:
    """
    Duplicate any GLTF glass mesh referenced by 2+ nodes before Isaac Sim
    loads the file.

    When a glass mesh is shared between nodes, NVIDIA's GLTF→USD converter
    wraps it in an instanceable Xform with an embedded Looks/ scope.  That
    buried structure blocks USD material-binding overrides, so PlanLift_Glass
    never reaches the actual geometry.

    Giving each node its own copy of the mesh data eliminates the sharing.
    The converter then produces a flat Mesh prim (identical to window glass)
    and normal material binding works.  Only the GLTF JSON changes; the BIN
    buffer is unchanged because copies reference the same accessor indices.
    """
    import struct, copy as _copy
    data = glb_path.read_bytes()
    if data[:4] != b"glTF":
        return

    json_len  = struct.unpack_from("<I", data, 12)[0]
    bin_start = 20 + json_len                  # offset of BIN chunk header
    bin_len   = struct.unpack_from("<I", data, bin_start)[0]
    bin_data  = data[bin_start + 8 : bin_start + 8 + bin_len]

    gltf = json.loads(data[20 : 20 + json_len])

    # Glass material indices: alphaMode=BLEND + baseColorFactor[3] < 0.35
    glass_mats = {
        i for i, m in enumerate(gltf.get("materials", []))
        if m.get("alphaMode", "OPAQUE") == "BLEND"
        and m.get("pbrMetallicRoughness", {})
              .get("baseColorFactor", [1, 1, 1, 1])[3] < 0.35
    }
    if not glass_mats:
        return

    # Mesh indices that have at least one glass primitive
    glass_meshes = {
        mi for mi, mesh in enumerate(gltf.get("meshes", []))
        if any(p.get("material") in glass_mats
               for p in mesh.get("primitives", []))
    }

    # Count how many nodes reference each mesh
    refcount: dict = {}
    for node in gltf.get("nodes", []):
        mi = node.get("mesh")
        if mi is not None:
            refcount[mi] = refcount.get(mi, 0) + 1

    shared = {mi for mi in glass_meshes if refcount.get(mi, 0) > 1}
    if not shared:
        return

    # First node keeps the original mesh; every subsequent node gets a deep
    # copy.  Copies use the same accessor / bufferView indices so the BIN
    # buffer needs no change — perfectly legal in GLTF 2.0.
    meshes = gltf["meshes"]
    first_seen: set = set()
    n_duped = 0
    for node in gltf.get("nodes", []):
        mi = node.get("mesh")
        if mi not in shared:
            continue
        if mi not in first_seen:
            first_seen.add(mi)
            continue
        new_idx = len(meshes)
        meshes.append(_copy.deepcopy(meshes[mi]))
        node["mesh"] = new_idx
        n_duped += 1

    if n_duped == 0:
        return

    # Repack GLB — only JSON chunk changes
    new_json = json.dumps(gltf, separators=(",", ":")).encode("utf-8")
    pad = (4 - len(new_json) % 4) % 4          # GLB spec: 4-byte alignment
    new_json += b" " * pad

    if len(bin_data) % 4:                       # keep BIN 4-byte aligned too
        bin_data = bin_data + b"\x00" * (4 - len(bin_data) % 4)

    total    = 12 + 8 + len(new_json) + 8 + len(bin_data)
    header   = struct.pack("<III", 0x46546C67, 2, total)
    json_hdr = struct.pack("<II",  len(new_json), 0x4E4F534A)   # 'JSON'
    bin_hdr  = struct.pack("<II",  len(bin_data), 0x004E4942)   # 'BIN\0'

    glb_path.write_bytes(header + json_hdr + new_json + bin_hdr + bin_data)
    print(
        f"[PlanLift] _dedup_glass_meshes: duplicated {n_duped} shared glass "
        f"mesh node(s) → flat hierarchy for {glb_path.name}",
        flush=True,
    )


@app.route("/isaac-sim/launch-and-open", methods=["POST"])
def isaac_launch_and_open():
    """Receive a GLB blob, save it, and start Isaac Sim in the background."""
    if "file" not in request.files:
        return jsonify({"error": "No GLB file provided"}), 400

    task_id  = uuid.uuid4().hex
    glb_dir  = BASE_DIR / "output" / "isaac_tmp"
    glb_dir.mkdir(parents=True, exist_ok=True)
    glb_path = glb_dir / f"{task_id}.glb"
    request.files["file"].save(str(glb_path))
    _dedup_glass_meshes(glb_path)   # eliminate shared-mesh instancing before Isaac Sim loads
    try:
        world_scale = float(request.form.get("world_scale", 0) or 0)
    except (TypeError, ValueError):
        world_scale = 0.0

    # Per-floor floorplan images: floorplan_image_<idx> fields + a
    # floors_meta JSON array of {idx, label, yBase}, one entry per floor of
    # the exported building. Falls back to the single legacy
    # floorplan_image field (pre-multi-floor client) as one synthetic
    # Floor-1 entry, so an un-updated frontend still works.
    floors_meta = []
    floors_meta_raw = request.form.get("floors_meta", "")
    if floors_meta_raw:
        try:
            floors_meta = json.loads(floors_meta_raw)
        except (TypeError, ValueError):
            floors_meta = []

    floors = []  # [{idx, label, yBase, image: <saved absolute path>}]
    if floors_meta:
        for entry in floors_meta:
            idx = int(entry.get("idx", 0))
            field = f"floorplan_image_{idx}"
            if field not in request.files:
                continue
            img_path = glb_dir / f"{task_id}_floorplan_{idx}.png"
            request.files[field].save(str(img_path))
            wall_height_m = entry.get("wallHeightM")
            floors.append({
                "idx": idx,
                "label": entry.get("label") or f"Floor {idx + 1}",
                "yBase": float(entry.get("yBase", 0.0)),
                "image": str(img_path),
                "wallHeightM": float(wall_height_m) if wall_height_m is not None else None,
            })
    elif "floorplan_image" in request.files:
        img_path = glb_dir / f"{task_id}_floorplan.png"
        request.files["floorplan_image"].save(str(img_path))
        floors.append({"idx": 0, "label": "Floor 1", "yBase": 0.0, "image": str(img_path), "wallHeightM": None})

    floorplan_path = Path(floors[0]["image"]) if floors else None

    # Written synchronously (not from the background worker thread) so Floor
    # Plan View — which polls this folder independently of whether Isaac Sim
    # itself is even open yet — can pick up the floor tabs immediately.
    if floors:
        floors_json_path = glb_dir / f"{task_id}_floors.json"
        floors_json_path.write_text(
            json.dumps({"task_id": task_id, "world_scale": world_scale, "floors": floors}),
            encoding="utf-8",
        )

    # Labeled rooms -> real-world coordinates, for the Isaac Sim AI Agent to
    # resolve room names against (see planlift_floorview's room registry).
    # Optional -- an un-labeled or un-updated client simply sends nothing,
    # and the agent panel just has no rooms to resolve against yet.
    rooms = []
    rooms_data_raw = request.form.get("rooms_data", "")
    if rooms_data_raw:
        try:
            rooms = json.loads(rooms_data_raw)
        except (TypeError, ValueError):
            rooms = []
    if rooms:
        rooms_json_path = glb_dir / f"{task_id}_rooms.json"
        rooms_json_path.write_text(
            json.dumps({"task_id": task_id, "rooms": rooms}),
            encoding="utf-8",
        )

    with _isaac_lock:
        _isaac_tasks[task_id] = {"status": "starting", "message": ""}

    threading.Thread(
        target=_isaac_worker, args=(task_id, glb_path, floorplan_path, world_scale, floors, rooms), daemon=True
    ).start()

    return jsonify({"task_id": task_id})


@app.route("/isaac-sim/task/<task_id>", methods=["GET"])
def isaac_task_status(task_id: str):
    """Return the current status of an Isaac Sim launch task."""
    with _isaac_lock:
        task = _isaac_tasks.get(task_id)
    if task is None:
        return jsonify({"error": "Task not found"}), 404
    return jsonify(task)


if __name__ == "__main__":
    print("=" * 55)
    print("  PlanLift Pipeline API  →  http://localhost:8003")
    print("  POST /predict              → FloorPlanTo3D-compatible JSON")
    print("  POST /analyze              → PlanLift-compatible JSON")
    print("  POST /analyze/json         → scene.json")
    print("  POST /isaac-sim/launch-and-open → start Isaac Sim")
    print("  GET  /isaac-sim/task/<id>  → poll task status")
    print("=" * 55)
    # use_reloader=False: prevents Werkzeug from spawning a child process.
    # With use_reloader=True, closing the terminal kills only the parent —
    # the child survives as an orphan, holds port 8003, and runs old code forever.
    app.run(host="0.0.0.0", port=8003, debug=True, use_reloader=False)
