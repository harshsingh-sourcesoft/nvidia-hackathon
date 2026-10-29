"""
Stage 2b — CV Symbol Detection  (fallback; no API key required)

Detects floor plan symbols using classical computer vision:
  - OCR text labels via EasyOCR  → room abbreviations, fixture labels (BR, WIC, REF…)
  - Hough circle/arc detection   → door swing type (swing vs sliding)
  - Repeated-line pattern        → stair tread count and bounding box

Produces a "symbols" dict in the same schema as stage_llm.py so Stage 4
can consume it without knowing which source was used.

Output saved to output/symbols/<stem>_symbols.json.
When --scene is given the symbols are also merged into the scene JSON.

Dependencies (all pip-installable):
  easyocr   — pip install easyocr
"""

import argparse
import json
import re
import sys
from pathlib import Path

import cv2
import numpy as np

# ── Abbreviation → semantic meaning ──────────────────────────────────────────
ABBREV_MAP = {
    # Rooms
    "br":    "bedroom",         "bed":   "bedroom",      "bdrm":  "bedroom",
    "mbr":   "master bedroom",  "mb":    "master bedroom","mstr":  "master bedroom",
    "lr":    "living room",     "liv":   "living room",   "living":"living room",
    "dr":    "dining room",     "din":   "dining room",   "dining":"dining room",
    "kit":   "kitchen",         "kitch": "kitchen",
    "bath":  "bathroom",        "ba":    "bathroom",      "bth":   "bathroom",
    "tlt":   "toilet",          "wc":    "toilet",        "toilet":"toilet",
    "shwr":  "shower",          "shower":"shower",
    "lav":   "sink",
    "cl":    "closet",          "cls":   "closet",        "clos":  "closet",
    "wic":   "walk-in closet",
    "util":  "utility",         "mech":  "utility",       "mech rm":"utility",
    "gar":   "garage",          "grg":   "garage",
    "laun":  "laundry",         "laundry":"laundry",
    "hall":  "hallway",         "cor":   "corridor",      "corr":  "corridor",
    "entry": "entrance",        "foyer": "entrance",
    # Fixtures / appliances
    "ref":   "refrigerator",    "refrig":"refrigerator",  "refr":  "refrigerator",
    "wh":    "water_heater",    "dw":    "dishwasher",
    # Electrical / HVAC
    "ac":    "air_conditioning","hvac":  "hvac",
    # Stair labels
    "up":    "stair_up",        "dn":    "stair_down",    "down":  "stair_down",
}

# Regex that matches dimension annotations (numbers with optional units)
_DIM_RE = re.compile(
    r"""^\s*
        [\d,./]+            # number(s)
        \s*
        (?:
          ['"]\s*[\d,./'"]*  # feet/inches like 12'6"
          | ft\b | feet\b
          | m\b | mm\b | cm\b
          | x\s*[\d,./]+    # 3x4 format
        )?
        \s*$""",
    re.VERBOSE | re.IGNORECASE,
)


# ── Utility ───────────────────────────────────────────────────────────────────

def _load_wall_mask(img_path: Path) -> np.ndarray | None:
    """Try to load the wall mask produced by Stage 2."""
    stem = img_path.stem.replace("_preprocessed", "")
    candidates = [
        img_path.parent / f"{stem}_masks.npz",
        img_path.parent.parent / "masks" / f"{stem}_masks.npz",
        Path("output") / "masks" / f"{stem}_masks.npz",
    ]
    for nc in candidates:
        if nc.exists():
            data = np.load(str(nc))
            if "walls" in data:
                return data["walls"]
    return None


def _wall_thickness_px(wall_mask: np.ndarray | None) -> float:
    if wall_mask is None or not wall_mask.any():
        return 6.0
    dist = cv2.distanceTransform(wall_mask, cv2.DIST_L2, 5)
    nz = dist[dist > 0]
    return float(np.median(nz) * 2) if len(nz) else 6.0


# ── OCR: text labels ──────────────────────────────────────────────────────────

def detect_text_labels(img_bgr: np.ndarray) -> list:
    """
    Run EasyOCR and return text detections with abbreviation mapping.
    Returns [] gracefully when easyocr is not installed.
    """
    try:
        import easyocr
    except ImportError:
        print("  [WARN] easyocr not installed — text detection skipped.\n"
              "         Install with: pip install easyocr")
        return []

    reader = easyocr.Reader(["en"], gpu=False, verbose=False)
    results = reader.readtext(img_bgr)

    labels = []
    for bbox_pts, text, conf in results:
        if conf < 0.30:
            continue
        clean = text.strip()
        if not clean:
            continue
        # Skip purely numeric / dimension annotations
        if _DIM_RE.match(clean):
            continue

        xs = [p[0] for p in bbox_pts]
        ys = [p[1] for p in bbox_pts]
        cx = int(sum(xs) / 4)
        cy = int(sum(ys) / 4)

        key = clean.lower().replace(" ", "").replace(".", "").replace("-", "")
        meaning = ABBREV_MAP.get(key, "unknown")

        labels.append({
            "text":    clean,
            "meaning": meaning,
            "x":       cx,
            "y":       cy,
            "conf":    round(conf, 2),
        })

    return labels


# ── Door type detection ───────────────────────────────────────────────────────

def detect_door_types(img_gray: np.ndarray,
                      openings: list,
                      wall_thickness: float) -> list:
    """
    For each known opening bbox classify the door type:
      swing    — a circular arc (Hough circles) touches the opening region
      sliding  — two or more parallel lines span the opening, no arc present
      unknown  — neither pattern found
    """
    h, w = img_gray.shape[:2]
    margin = int(wall_thickness * 3)

    typed_doors = []
    for op in openings:
        if op.get("type") == "window":
            continue

        ox, oy = op.get("x", 0), op.get("y", 0)
        ow, oh  = op.get("w", 10), op.get("h", 10)

        # Search window slightly larger than the opening
        sx1 = max(0, ox - margin);  sy1 = max(0, oy - margin)
        sx2 = min(w, ox + ow + margin); sy2 = min(h, oy + oh + margin)
        crop = img_gray[sy1:sy2, sx1:sx2]
        if crop.size == 0:
            continue

        door_dim = max(ow, oh)
        r_min    = max(int(door_dim * 0.4), 8)
        r_max    = max(int(door_dim * 1.6), 30)

        blurred = cv2.GaussianBlur(crop, (5, 5), 0)
        circles = cv2.HoughCircles(
            blurred, cv2.HOUGH_GRADIENT, dp=1.5,
            minDist=door_dim // 2,
            param1=60, param2=22,
            minRadius=r_min, maxRadius=r_max,
        )

        if circles is not None:
            door_type = "swing"
        else:
            edges = cv2.Canny(blurred, 40, 120)
            lines = cv2.HoughLinesP(
                edges, 1, np.pi / 180, threshold=12,
                minLineLength=door_dim // 3, maxLineGap=5,
            )
            door_type = "sliding" if (lines is not None and len(lines) >= 2) else "unknown"

        typed_doors.append({
            "type": door_type,
            "bbox": [ox, oy, ow, oh],
        })

    return typed_doors


# ── Stair detection ───────────────────────────────────────────────────────────

def detect_stairs(img_gray: np.ndarray, wall_thickness: float) -> list:
    """
    Detect staircase regions by finding clusters of evenly-spaced parallel lines
    (tread lines).  Works for both horizontal and vertical stair runs.
    Returns list of { direction, tread_count, bbox }.
    """
    h, w = img_gray.shape[:2]
    min_tread_len = max(20, int(min(h, w) * 0.04))
    max_spacing   = max(int(wall_thickness * 3), 18)

    edges = cv2.Canny(img_gray, 40, 120)
    lines = cv2.HoughLinesP(
        edges, 1, np.pi / 180, threshold=18,
        minLineLength=min_tread_len,
        maxLineGap=int(wall_thickness * 1.5),
    )
    if lines is None:
        return []

    results = []
    for is_horiz in (True, False):
        # Keep only near-horizontal or near-vertical segments
        filtered = []
        for seg in lines[:, 0]:
            x1, y1, x2, y2 = seg
            dx, dy = abs(int(x2) - int(x1)), abs(int(y2) - int(y1))
            if is_horiz and dx > dy * 3:
                filtered.append(seg)
            elif not is_horiz and dy > dx * 3:
                filtered.append(seg)

        if len(filtered) < 3:
            continue

        # Fixed-axis mid-coordinate (y for H-lines, x for V-lines)
        coord_idx = 1 if is_horiz else 0
        coords = sorted(set(
            int((s[coord_idx] + s[coord_idx + 2]) / 2) for s in filtered
        ))

        # Find runs of evenly spaced lines
        runs: list[list[int]] = []
        cur: list[int] = [coords[0]]
        for c in coords[1:]:
            if c - cur[-1] <= max_spacing:
                cur.append(c)
            else:
                if len(cur) >= 3:
                    runs.append(cur)
                cur = [c]
        if len(cur) >= 3:
            runs.append(cur)

        for run in runs:
            run_set = set(run)
            run_lines = [
                s for s in filtered
                if int((s[coord_idx] + s[coord_idx + 2]) / 2) in run_set
            ]
            if not run_lines:
                continue
            xs = [s[0] for s in run_lines] + [s[2] for s in run_lines]
            ys = [s[1] for s in run_lines] + [s[3] for s in run_lines]
            bx  = max(0, min(xs));   by  = max(0, min(ys))
            bx2 = min(w, max(xs));   by2 = min(h, max(ys))

            results.append({
                "direction":   "unknown",
                "tread_count": len(run),
                "bbox":        [bx, by, bx2 - bx, by2 - by],
            })

    return results


# ── Orchestrator ──────────────────────────────────────────────────────────────

def detect_symbols_cv(img_path: Path, scene: dict | None = None) -> dict:
    """
    Run all CV symbol detectors and return a symbols dict compatible with
    the schema produced by stage_llm.py.
    """
    img_bgr = cv2.imread(str(img_path))
    if img_bgr is None:
        print(f"[ERROR] Cannot read image: {img_path}")
        return {}

    img_gray     = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2GRAY)
    wall_mask    = _load_wall_mask(img_path)
    wall_thick   = _wall_thickness_px(wall_mask)
    openings     = scene.get("openings", []) if scene else []

    print("  Running OCR for text labels...")
    text_labels = detect_text_labels(img_bgr)
    print(f"  Text labels found: {len(text_labels)}")

    print("  Classifying door types...")
    doors = detect_door_types(img_gray, openings, wall_thick)
    print(f"  Doors classified: {len(doors)}")

    print("  Detecting stair regions...")
    stairs = detect_stairs(img_gray, wall_thick)
    print(f"  Stair regions: {len(stairs)}")

    return {
        "doors":       doors,
        "windows":     [],   # window-type CV detection not implemented
        "stairs":      stairs,
        "fixtures":    [],   # fixture CV detection not implemented
        "furniture":   [],   # furniture CV detection not implemented
        "text_labels": text_labels,
    }


def main():
    parser = argparse.ArgumentParser(
        description="Stage 2b: CV-based floor plan symbol detection"
    )
    parser.add_argument("image", help="Floor plan image (original or preprocessed)")
    parser.add_argument("--scene", default=None,
                        help="Scene JSON from Stage 3 — adds symbol data to it")
    parser.add_argument("--output-dir", default="output/symbols")
    args = parser.parse_args()

    img_path   = Path(args.image)
    scene_path = Path(args.scene) if args.scene else None

    if not img_path.exists():
        print(f"[ERROR] Image not found: {img_path}")
        sys.exit(1)

    scene: dict = {}
    if scene_path and scene_path.exists():
        with open(scene_path, encoding="utf-8") as f:
            scene = json.load(f)

    print(f"[Stage 2b] CV symbol detection: {img_path.name}")
    symbols = detect_symbols_cv(img_path, scene)

    # Merge non-empty results into scene and save
    if scene and scene_path:
        scene.setdefault("symbols", {})
        for key, val in symbols.items():
            if val:
                scene["symbols"][key] = val

        # Backfill door_type onto matching openings
        for door in symbols.get("doors", []):
            op_cx = door["bbox"][0] + door["bbox"][2] / 2
            op_cy = door["bbox"][1] + door["bbox"][3] / 2
            for op in scene.get("openings", []):
                if op.get("type") == "window":
                    continue
                dist = ((op["x"] + op["w"]/2 - op_cx)**2 +
                        (op["y"] + op["h"]/2 - op_cy)**2) ** 0.5
                if dist < max(op["w"], op["h"]) * 2:
                    op.setdefault("door_type", door["type"])

        with open(scene_path, "w", encoding="utf-8") as f:
            json.dump(scene, f, indent=2)
        print(f"[Stage 2b] Scene updated    -> {scene_path}")

    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    stem     = img_path.stem.replace("_preprocessed", "")
    out_path = out_dir / f"{stem}_symbols.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(symbols, f, indent=2)
    print(f"[Stage 2b] Symbols saved    -> {out_path}")
    return str(out_path)


if __name__ == "__main__":
    main()
