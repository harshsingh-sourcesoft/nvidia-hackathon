"""
Diagnostic script — run this directly on any floor plan image to see
what the opening detection finds, WITHOUT needing the web server.

Usage:
  venv\Scripts\python.exe test_detection.py path\to\floor_plan.png

Outputs:
  debug_walls.png    — wall scanlines + detected openings drawn on the image
  Console            — counts at every step
"""

import sys
import math
from pathlib import Path

import cv2
import numpy as np

# ── Paste of the same functions used by api_server.py ─────────────────────────

def walls_from_npz(npz_path: Path) -> list:
    data      = np.load(str(npz_path), allow_pickle=True)
    wall_mask = data["walls"].astype(np.uint8)
    if wall_mask.max() == 0:
        print("  [WARN] Wall mask is ALL ZERO — stage2 produced nothing")
        return []

    dist   = cv2.distanceTransform(wall_mask, cv2.DIST_L2, 5)
    nz     = dist[dist > 0]
    thick  = float(np.median(nz) * 2) if len(nz) else 5.0
    h, w   = wall_mask.shape[:2]
    output_thick = max(15.0, thick)
    min_run      = max(int(min(h, w) * 0.035), int(thick * 4), 15)
    print(f"  thick={thick:.1f}px  min_run={min_run}px  image={w}x{h}")

    kl     = max(3, int(thick) // 3 * 2 + 1)
    closed = cv2.morphologyEx(wall_mask, cv2.MORPH_CLOSE,
                              cv2.getStructuringElement(cv2.MORPH_RECT, (kl, kl)))
    walls, idx = [], 0
    for is_horiz in (True, False):
        kshape = (min_run, 1) if is_horiz else (1, min_run)
        opened = cv2.morphologyEx(closed, cv2.MORPH_OPEN,
                                  cv2.getStructuringElement(cv2.MORPH_RECT, kshape))
        if opened.sum() == 0:
            continue
        n, _, stats, centroids = cv2.connectedComponentsWithStats(opened, connectivity=8)
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
                walls.append({"id": f"mw{idx}", "start": {"x": round(x0, 1), "y": round(cy, 1)},
                               "end": {"x": round(x0+bw, 1), "y": round(cy, 1)},
                               "thickness": round(output_thick, 1), "height": 270.0})
            else:
                walls.append({"id": f"mw{idx}", "start": {"x": round(cx, 1), "y": round(y0, 1)},
                               "end": {"x": round(cx, 1), "y": round(y0+bh, 1)},
                               "thickness": round(output_thick, 1), "height": 270.0})
            idx += 1
    print(f"  walls extracted: {len(walls)}")
    return walls


def detect_openings_from_image(walls: list, img_path: Path, thick: float, debug_img=None):
    img = cv2.imread(str(img_path), cv2.IMREAD_GRAYSCALE)
    if img is None:
        print(f"  [ERROR] Cannot read image: {img_path}")
        return [], []

    h_img, w_img = img.shape
    print(f"  image size: {w_img}x{h_img}")

    scan_half   = max(6, min(25, int(thick * 0.18)))
    min_opening = max(25, int(min(h_img, w_img) * 0.012))
    max_opening = max(350, int(min(h_img, w_img) * 0.22))
    margin      = max(int(thick * 0.25), 20)
    print(f"  scan_half={scan_half}  min_opening={min_opening}  max_opening={max_opening}  margin={margin}")

    blurred  = cv2.GaussianBlur(img, (5, 5), 0)
    min_r    = max(8, min(h_img, w_img) // 60)
    max_r    = max(min_r + 2, min(h_img, w_img) // 6)
    raw_arcs = cv2.HoughCircles(blurred, cv2.HOUGH_GRADIENT, dp=1.5,
                                 minDist=min_r*2, param1=50, param2=16,
                                 minRadius=min_r, maxRadius=max_r)
    arcs = []
    if raw_arcs is not None:
        for cx_i, cy_i, r_i in np.round(raw_arcs[0]).astype(int):
            arcs.append((float(cx_i), float(cy_i), float(r_i)))
    print(f"  Hough arcs: {len(arcs)}")

    # Draw arcs on debug image
    if debug_img is not None:
        for (cx_a, cy_a, r_a) in arcs:
            cv2.circle(debug_img, (int(cx_a), int(cy_a)), int(r_a), (0, 255, 0), 1)

    all_gaps = []
    for wall in walls:
        x1 = float(wall["start"]["x"]); y1 = float(wall["start"]["y"])
        x2 = float(wall["end"]["x"]);   y2 = float(wall["end"]["y"])
        is_horiz = abs(x2 - x1) >= abs(y2 - y1)

        if is_horiz:
            y_c  = int(round((y1+y2)/2))
            xa   = int(round(min(x1,x2)));  xb = int(round(max(x1,x2)))
            wlen = xb - xa
            if wlen < min_opening*2:
                continue
            y_lo = max(0, y_c - scan_half);  y_hi = min(h_img, y_c + scan_half + 1)
            if y_lo >= y_hi: continue
            band    = img[y_lo:y_hi, xa:xb]
            profile = band.min(axis=0).astype(float)
            def gap_coords(mid, _xa=xa, _yc=y_c):
                return (_xa + mid, float(_yc))
            # Draw scan line on debug image
            if debug_img is not None:
                cv2.line(debug_img, (xa, y_c), (xb, y_c), (255, 0, 0), 1)
        else:
            x_c  = int(round((x1+x2)/2))
            ya   = int(round(min(y1,y2)));  yb = int(round(max(y1,y2)))
            wlen = yb - ya
            if wlen < min_opening*2:
                continue
            x_lo = max(0, x_c - scan_half);  x_hi = min(w_img, x_c + scan_half + 1)
            if x_lo >= x_hi: continue
            band    = img[ya:yb, x_lo:x_hi]
            profile = band.min(axis=1).astype(float)
            def gap_coords(mid, _xc=x_c, _ya=ya):
                return (float(_xc), _ya + mid)
            if debug_img is not None:
                cv2.line(debug_img, (x_c, ya), (x_c, yb), (255, 0, 0), 1)

        if len(profile) < min_opening*2:
            continue

        wall_dark = float(np.percentile(profile, 10))
        thresh    = min(220.0, max(130.0, wall_dark + 70.0))

        is_open = profile > thresh
        if margin*2 < len(is_open):
            is_open[:margin]  = False
            is_open[-margin:] = False

        n, i = len(is_open), 0
        while i < n:
            if is_open[i]:
                j = i
                while j < n and is_open[j]:
                    j += 1
                gap_w = j - i
                if min_opening <= gap_w <= max_opening:
                    mid = (i + j) / 2.0
                    t   = max(0.05, min(0.95, mid / float(wlen)))
                    gcx, gcy = gap_coords(mid)
                    all_gaps.append({"wallId": wall["id"], "t": round(t, 3),
                                     "width": float(gap_w), "cx": gcx, "cy": gcy})
                    # Draw gap on debug image
                    if debug_img is not None:
                        if is_horiz:
                            cv2.rectangle(debug_img,
                                          (int(gcx - gap_w/2), int(gcy - scan_half*2)),
                                          (int(gcx + gap_w/2), int(gcy + scan_half*2)),
                                          (0, 0, 255), 2)
                        else:
                            cv2.rectangle(debug_img,
                                          (int(gcx - scan_half*2), int(gcy - gap_w/2)),
                                          (int(gcx + scan_half*2), int(gcy + gap_w/2)),
                                          (0, 0, 255), 2)
                i = j
            else:
                i += 1

    print(f"  Raw openings: {len(all_gaps)}")

    # Sample: print first 5 gaps
    for g in all_gaps[:5]:
        print(f"    gap wallId={g['wallId']}  t={g['t']}  width={g['width']:.0f}px  cx={g['cx']:.0f}  cy={g['cy']:.0f}")

    used_arcs: set[int] = set()
    doors, windows = [], []
    for gi, gap in enumerate(all_gaps):
        best_ai, best_ad = None, float("inf")
        for ai, (cx_a, cy_a, r_a) in enumerate(arcs):
            d = math.hypot(cx_a - gap["cx"], cy_a - gap["cy"])
            if d < r_a * 3.0 and d < best_ad:
                best_ad, best_ai = d, ai
        wid_val = round(max(30.0, gap["width"]), 1)
        if best_ai is not None and best_ai not in used_arcs:
            used_arcs.add(best_ai)
            doors.append({"id": f"pd{gi}", "wallId": gap["wallId"],
                          "positionOnWall": gap["t"], "width": wid_val, "height": 210.0})
            if debug_img is not None:
                cv2.putText(debug_img, "D", (int(gap["cx"]), int(gap["cy"])),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 0, 200), 2)
        else:
            windows.append({"id": f"pw{gi}", "wallId": gap["wallId"],
                            "positionOnWall": gap["t"], "width": wid_val, "height": 120.0})
            if debug_img is not None:
                cv2.putText(debug_img, "W", (int(gap["cx"]), int(gap["cy"])),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.7, (200, 0, 0), 2)

    print(f"  Doors: {len(doors)}  Windows: {len(windows)}")
    return doors, windows


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    if len(sys.argv) < 2:
        print("Usage: python test_detection.py <floor_plan_image.png>")
        sys.exit(1)

    img_path = Path(sys.argv[1])
    if not img_path.exists():
        print(f"Image not found: {img_path}")
        sys.exit(1)

    import subprocess, os
    BASE_DIR = Path(__file__).parent
    PYTHON   = str(BASE_DIR / "venv" / "Scripts" / "python.exe")
    env      = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"

    print("=== Stage 1: preprocess ===")
    r = subprocess.run([PYTHON, "stage1_preprocess.py", str(img_path)],
                       cwd=str(BASE_DIR), env=env, capture_output=True, text=True)
    print(r.stdout[-500:] if r.stdout else "  (no output)")
    if r.returncode != 0:
        print("FAILED:", r.stderr[-300:])
        sys.exit(1)

    stem = img_path.stem
    preprocessed = BASE_DIR / "output" / "masks" / f"{stem}_preprocessed.png"
    if not preprocessed.exists():
        preprocessed = img_path
    print(f"  preprocessed: {preprocessed}")

    print("\n=== Stage 2: segment ===")
    r = subprocess.run([PYTHON, "stage2_segment.py", str(preprocessed)],
                       cwd=str(BASE_DIR), env=env, capture_output=True, text=True)
    print(r.stdout[-1000:] if r.stdout else "  (no output)")
    if r.returncode != 0:
        print("FAILED:", r.stderr[-300:])
        sys.exit(1)

    masks_npz = BASE_DIR / "output" / "masks" / f"{preprocessed.stem}_masks.npz"
    if not masks_npz.exists():
        masks_npz = BASE_DIR / "output" / "masks" / f"{stem}_masks.npz"
    print(f"  masks_npz: {masks_npz}  exists={masks_npz.exists()}")

    print("\n=== Wall extraction ===")
    walls = walls_from_npz(masks_npz)

    data  = np.load(str(masks_npz), allow_pickle=True)
    wm    = data["walls"].astype(np.uint8)
    dist_t = cv2.distanceTransform(wm, cv2.DIST_L2, 5)
    nz_t   = dist_t[dist_t > 0]
    thick  = float(np.median(nz_t) * 2) if len(nz_t) > 0 else 6.0
    print(f"  thick from mask dist-transform: {thick:.1f}px")

    print("\n=== Opening detection ===")
    dbg   = cv2.imread(str(preprocessed))
    if dbg is None:
        dbg = cv2.imread(str(img_path))
    doors, windows = detect_openings_from_image(walls, preprocessed, thick, debug_img=dbg)

    out_path = img_path.parent / "debug_walls.png"
    if dbg is not None:
        cv2.imwrite(str(out_path), dbg)
        print(f"\n  Debug image saved: {out_path}")

    print(f"\n=== RESULT: {len(walls)} walls, {len(doors)} doors, {len(windows)} windows ===")


if __name__ == "__main__":
    main()
