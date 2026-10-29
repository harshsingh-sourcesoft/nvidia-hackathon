"""
Diagnostic: per-wall image-scan dark-pixel stats.
Run with:
  venv\Scripts\python.exe test_gap_diag.py [masks.npz] [preprocessed.png]
Defaults to the most recent upload_* pair.
"""
import sys, math
from pathlib import Path
import cv2
import numpy as np

BASE = Path(__file__).parent

# ── Find latest NPZ + image ───────────────────────────────────────────────────
if len(sys.argv) >= 3:
    npz_path = Path(sys.argv[1])
    img_path = Path(sys.argv[2])
else:
    npzs = sorted((BASE / "output" / "masks").glob("upload_*_masks.npz"),
                  key=lambda p: p.stat().st_mtime, reverse=True)
    if not npzs:
        print("No upload_*_masks.npz found"); sys.exit(1)
    npz_path = npzs[0]
    stem = npz_path.stem.replace("_masks", "")
    img_path = npz_path.parent / f"{stem}_preprocessed.png"

print(f"NPZ : {npz_path.name}")
print(f"IMG : {img_path.name}  exists={img_path.exists()}")

data      = np.load(str(npz_path), allow_pickle=True)
wall_mask = data["walls"].astype(np.uint8)
h_m, w_m  = wall_mask.shape
print(f"Mask shape: {w_m}x{h_m}")

img_gray = cv2.imread(str(img_path), cv2.IMREAD_GRAYSCALE)
if img_gray is None:
    print("ERROR: cannot load image"); sys.exit(1)

if img_gray.shape[:2] != (h_m, w_m):
    print(f"  resizing image from {img_gray.shape[1]}x{img_gray.shape[0]} -> {w_m}x{h_m}")
    img_gray = cv2.resize(img_gray, (w_m, h_m), interpolation=cv2.INTER_AREA)

print(f"Image shape after resize: {img_gray.shape[1]}x{img_gray.shape[0]}")
print(f"Image pixel range: {img_gray.min()}-{img_gray.max()}  mean={img_gray.mean():.1f}")

# ── Compute thick ─────────────────────────────────────────────────────────────
dist = cv2.distanceTransform(wall_mask, cv2.DIST_L2, 5)
nz   = dist[dist > 0]
thick = float(np.median(nz) * 2) if len(nz) else 6.0
print(f"\nthick={thick:.1f}px")

thick_px_est = max(8, int(min(h_m, w_m) * 0.015))
dil_per_side = (thick_px_est - 1) // 2
img_wall_est = max(4, int(thick) - 2 * dil_per_side)
band_half    = max(img_wall_est, 8)
print(f"thick_px_est={thick_px_est}  dil_per_side={dil_per_side}")
print(f"img_wall_est={img_wall_est}  band_half={band_half}")

# ── Extract walls (same logic as walls_from_npz) ──────────────────────────────
min_run = max(int(min(h_m, w_m) * 0.035), int(thick * 4), 15)
kl      = max(3, int(thick) // 3 * 2 + 1)
closed  = cv2.morphologyEx(wall_mask, cv2.MORPH_CLOSE,
          cv2.getStructuringElement(cv2.MORPH_RECT, (kl, kl)))

walls = []
for is_h in (True, False):
    ks     = (min_run, 1) if is_h else (1, min_run)
    opened = cv2.morphologyEx(closed, cv2.MORPH_OPEN,
             cv2.getStructuringElement(cv2.MORPH_RECT, ks))
    n, _, stats, cents = cv2.connectedComponentsWithStats(opened, connectivity=8)
    for lbl in range(1, n):
        bw  = float(stats[lbl, cv2.CC_STAT_WIDTH])
        bh  = float(stats[lbl, cv2.CC_STAT_HEIGHT])
        run = bw if is_h else bh
        if run < min_run: continue
        x0  = float(stats[lbl, cv2.CC_STAT_LEFT])
        y0  = float(stats[lbl, cv2.CC_STAT_TOP])
        cx  = float(cents[lbl, 0]); cy = float(cents[lbl, 1])
        if is_h:
            walls.append({"id": f"mw{len(walls)}", "start": {"x": x0, "y": cy},
                           "end": {"x": x0+bw, "y": cy}, "horiz": True})
        else:
            walls.append({"id": f"mw{len(walls)}", "start": {"x": cx, "y": y0},
                           "end": {"x": cx, "y": y0+bh}, "horiz": False})

print(f"\nWalls extracted: {len(walls)}  min_run={min_run}")

# ── Per-wall scan ─────────────────────────────────────────────────────────────
dark_thresh  = 100
rel_gap_frac = 0.30
min_gap      = max(20, int(min(h_m, w_m) * 0.010))
max_gap      = max(350, int(min(h_m, w_m) * 0.30))
img_margin   = max(5, dil_per_side)

print(f"\nmin_gap={min_gap}  max_gap={max_gap}  img_margin={img_margin}")
print(f"\n{'id':6} {'dir':1} {'wlen':6} {'y_c/x_c':7} {'band':10} {'max_dc':6} {'thr':5} {'gap_cols':8} {'gaps_found':10}")
print("-" * 70)

total_gaps = []
for wall in walls:
    x1 = float(wall["start"]["x"]); y1 = float(wall["start"]["y"])
    x2 = float(wall["end"]["x"]);   y2 = float(wall["end"]["y"])
    is_h = abs(x2 - x1) >= abs(y2 - y1)

    if is_h:
        xa   = int(round(min(x1, x2))); xb = int(round(max(x1, x2)))
        y_c  = int(round((y1 + y2) / 2)); wlen = xb - xa
        y_lo = max(0, y_c - band_half); y_hi = min(h_m, y_c + band_half + 1)
        if y_lo >= y_hi or wlen < min_gap * 2:
            print(f"  {wall['id']:6} H {wlen:6}  (skipped - too short or band invalid)")
            continue
        band = img_gray[y_lo:y_hi, xa:xb].astype(np.float32)
        dc   = (band < dark_thresh).sum(axis=0)
        center_str = f"y={y_c}"
        band_str   = f"[{y_lo},{y_hi})"
    else:
        ya   = int(round(min(y1, y2))); yb = int(round(max(y1, y2)))
        x_c  = int(round((x1 + x2) / 2)); wlen = yb - ya
        x_lo = max(0, x_c - band_half); x_hi = min(w_m, x_c + band_half + 1)
        if x_lo >= x_hi or wlen < min_gap * 2:
            print(f"  {wall['id']:6} V {wlen:6}  (skipped - too short or band invalid)")
            continue
        band = img_gray[ya:yb, x_lo:x_hi].astype(np.float32)
        dc   = (band < dark_thresh).sum(axis=1)
        center_str = f"x={x_c}"
        band_str   = f"[{x_lo},{x_hi})"

    max_dc    = int(dc.max())
    thr       = rel_gap_frac * max_dc if max_dc >= 3 else 0
    gap_cols  = int((dc < max(1, thr)).sum()) if max_dc >= 3 else 0

    # Count actual detected gaps (after margin, size filter)
    gaps_here = 0
    if max_dc >= 3:
        prof = dc < (rel_gap_frac * max_dc)
        n_p  = len(prof)
        if img_margin * 2 < n_p:
            prof[:img_margin] = False; prof[-img_margin:] = False
        i = 0
        while i < n_p:
            if prof[i]:
                j = i
                while j < n_p and prof[j]: j += 1
                if min_gap <= (j - i) <= max_gap:
                    gaps_here += 1
                    total_gaps.append({"wallId": wall["id"], "gap_w": j-i, "t_mid": (i+j)/2/wlen})
                i = j
            else:
                i += 1

    flag = " <<<" if gaps_here > 0 else ""
    print(f"  {wall['id']:6} {'H' if is_h else 'V'} {wlen:6}  {center_str:7}  band={band_str:12} "
          f"max_dc={max_dc:4}  thr={thr:4.0f}  gap_cols={gap_cols:4}  gaps={gaps_here}{flag}")

print(f"\nTotal gaps found: {len(total_gaps)}")
for g in total_gaps:
    print(f"  wallId={g['wallId']}  gap_w={g['gap_w']}px  t_mid={g['t_mid']:.2f}")
