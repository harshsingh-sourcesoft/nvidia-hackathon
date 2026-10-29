"""
Stage LLM — GPT-4o Floor Plan Understanding
Reads: original input image + scene JSON from Stage 3
Writes: enriched scene JSON with accurate room labels, calibrated scale,
        and a full symbols catalogue (doors, windows, stairs, fixtures,
        furniture, text labels).

Scale calibration strategy:
  GPT-4o reads every dimension annotation in the image (it does this perfectly).
  Python then finds the scale (m/px) that best maps the room bounding-box pixel
  dimensions onto that annotation set — no pixel estimation from the LLM needed.

Slots between Stage 3 (vectorize) and Stage 4 (extrude).
Skipped automatically if OPENAI_API_KEY is not set.
"""

import argparse
import base64
import json
import math
import os
import sys
from pathlib import Path

import numpy as np
from dotenv import load_dotenv

load_dotenv()


SYSTEM_PROMPT = """\
You are an expert at reading architectural floor plan drawings.

Read dimension annotations exactly as printed. Conversion: 1 ft = 0.3048 m, 1 in = 0.0254 m.

═══ DIMENSION RULES ═══
For perimeter_horizontal: use the outer edge (top OR bottom) whose annotation
  segments sum to the LARGEST total — this is the full exterior building width.
For perimeter_vertical: use the outer edge (left OR right) whose annotation
  segments sum to the LARGEST total — this is the full exterior building height.
Ignore interior room-width annotations; use only the outermost dimension strings.
═══════════════════════

Return a JSON object with EXACTLY these keys:

1. "perimeter_horizontal"
   List of outer horizontal dimension annotations, each:
   { "text": "17'1\"", "real_m": 5.207 }

2. "perimeter_vertical"
   List of outer vertical dimension annotations, each:
   { "text": "14'10\"", "real_m": 4.521 }

3. "total_width_m"   — float, sum of perimeter_horizontal real_m values
4. "total_height_m"  — float, sum of perimeter_vertical real_m values

5. "room_labels"
   Dict mapping centroid index string ("0", "1", …) to room type.
   Centroid positions are given in the user message.
   Choose from: living room, bedroom, master bedroom, kitchen, bathroom, toilet,
   hallway, dining room, office, storage, balcony, garage, laundry, walk-in closet,
   closet, utility, staircase, corridor, terrace, entrance, library, gym, unknown

6. "openings"
   List — same order as given — of "door" or "window" for each opening bbox.

7. "symbols"
   Every recognisable non-wall symbol visible in the image.
   All bboxes use [x, y, w, h] in pixels, origin = top-left corner of image.

   "doors": list of
     { "type": "swing" | "double_swing" | "sliding" | "pocket" | "bifold",
       "bbox": [x, y, w, h] }
     swing     = single arc (standard hinged door)
     double_swing = two arcs (double doors)
     sliding   = two parallel lines (no arc)
     pocket    = dashed rectangle (door slides into wall)
     bifold    = two small folded panels

   "windows": list of
     { "type": "standard" | "casement" | "sliding" | "bay" | "fixed",
       "bbox": [x, y, w, h] }

   "stairs": list of
     { "direction": "up" | "down" | "unknown",
       "tread_count": <integer>,
       "bbox": [x, y, w, h] }

   "fixtures": list of
     { "type": "toilet" | "shower" | "bathtub" | "sink" | "kitchen_sink" |
               "refrigerator" | "oven" | "range" | "dishwasher" |
               "washer" | "dryer" | "water_heater",
       "bbox": [x, y, w, h] }

   "furniture": list of
     { "type": "bed" | "double_bed" | "sofa" | "chair" | "armchair" |
               "dining_table" | "desk" | "bookshelf" | "wardrobe" | "table",
       "bbox": [x, y, w, h] }

   "text_labels": list of
     { "text": "<text as printed>", "meaning": "<interpreted type>",
       "x": <centre x px>, "y": <centre y px> }
     Include room abbreviations (BR, KIT, WIC …), fixture labels (REF, TLT …),
     and directional labels (UP, DN).

   Rules:
   - Use pixel coordinates that match the image dimensions given in the user message.
   - Leave a category as [] when it is absent from this floor plan.
   - Bounding boxes may be approximate; type accuracy is more important than
     perfect bbox precision.
   - Do NOT list structural walls or room boundary lines as symbols.

Respond ONLY with valid JSON. No markdown fences. No prose.
"""


def encode_image(image_path: Path) -> str:
    with open(image_path, "rb") as f:
        return base64.b64encode(f.read()).decode("utf-8")


def build_user_message(image_path: Path, scene: dict) -> list:
    b64  = encode_image(image_path)
    ext  = image_path.suffix.lower().lstrip(".")
    mime = "image/png" if ext in ("png", "") else f"image/{ext}"

    meta = scene.get("meta", {})
    room_centroids = [
        {"index": r["id"], "centroid_px": r["centroid"]}
        for r in scene.get("rooms", [])
    ]
    openings_info = [
        {"index": i, "x": op["x"], "y": op["y"], "w": op["w"], "h": op["h"]}
        for i, op in enumerate(scene.get("openings", []))
    ]

    text = (
        f"Floor plan image: {meta.get('width_px')} × {meta.get('height_px')} px\n\n"
        f"Room centroids for labelling:\n{json.dumps(room_centroids, indent=2)}\n\n"
        f"Openings to classify (door or window):\n{json.dumps(openings_info)}\n\n"
        "Read ALL dimension annotation text visible in the image.\n"
        "Detect ALL floor plan symbols (doors, windows, stairs, fixtures, "
        "furniture, text labels) and return their pixel bounding boxes."
    )

    return [
        {"type": "image_url",
         "image_url": {"url": f"data:{mime};base64,{b64}", "detail": "high"}},
        {"type": "text", "text": text},
    ]


def query_gpt4o(image_path: Path, scene: dict, api_key: str) -> dict:
    from openai import OpenAI
    client = OpenAI(api_key=api_key)

    print("  Sending floor plan to GPT-4o (detail=high)...")
    resp = client.chat.completions.create(
        model="gpt-4o",
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user",   "content": build_user_message(image_path, scene)},
        ],
        max_tokens=4096,
        temperature=0.0,
    )
    raw = resp.choices[0].message.content.strip()
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
        raw = raw.rsplit("```", 1)[0]
    return json.loads(raw)


# ── Scene enhancement ─────────────────────────────────────────────────────────

def _nearest_symbol(op: dict, sym_list: list, max_dist_px: float) -> dict | None:
    """Return the symbol in sym_list whose bbox centre is closest to op's bbox centre."""
    op_cx = op["x"] + op["w"] / 2
    op_cy = op["y"] + op["h"] / 2
    best_dist, best = float("inf"), None
    for s in sym_list:
        bb = s.get("bbox")
        if not bb:
            continue
        sc_x = bb[0] + bb[2] / 2
        sc_y = bb[1] + bb[3] / 2
        dist = math.hypot(op_cx - sc_x, op_cy - sc_y)
        if dist < best_dist:
            best_dist, best = dist, s
    return best if best_dist <= max_dist_px else None


def enhance_scene(scene: dict, llm_result: dict) -> dict:
    meta = scene["meta"]

    # ── Scale calibration ─────────────────────────────────────────────────────
    horiz_raw = llm_result.get("perimeter_horizontal", [])
    vert_raw  = llm_result.get("perimeter_vertical",   [])

    horiz = [(a.get("text", ""), a.get("real_m", 0)) for a in horiz_raw]
    vert  = [(a.get("text", ""), a.get("real_m", 0)) for a in vert_raw]

    total_w_m = sum(m for _, m in horiz) if horiz else None
    total_h_m = sum(m for _, m in vert)  if vert  else None

    if horiz:
        print(f"  Perimeter H: {' + '.join(t for t, _ in horiz)} = {total_w_m:.3f} m")
    if vert:
        print(f"  Perimeter V: {' + '.join(t for t, _ in vert)} = {total_h_m:.3f} m")

    if total_w_m and not (3.0 < total_w_m < 120.0):
        print(f"  [WARN] total_width_m={total_w_m:.2f} unrealistic — ignoring.")
        total_w_m = None
    if total_h_m and not (3.0 < total_h_m < 120.0):
        print(f"  [WARN] total_height_m={total_h_m:.2f} unrealistic — ignoring.")
        total_h_m = None

    bbox   = meta.get("wall_bbox_px", {})
    bbox_w = bbox.get("width_px")
    bbox_h = bbox.get("height_px")

    s_w = (total_w_m / bbox_w) if (total_w_m and bbox_w) else None
    s_h = (total_h_m / bbox_h) if (total_h_m and bbox_h) else None

    if s_w:
        print(f"  s_w = {total_w_m:.3f} m / {bbox_w} px = {s_w:.6f} m/px")
    if s_h:
        print(f"  s_h = {total_h_m:.3f} m / {bbox_h} px = {s_h:.6f} m/px")

    scale = None
    if s_w and s_h:
        ratio = max(s_w, s_h) / min(s_w, s_h)
        print(f"  s_w={s_w:.6f}  s_h={s_h:.6f}  ratio={ratio:.3f}")
        if ratio <= 1.25:
            scale = (s_w + s_h) / 2
            print("  H and V agree — using mean.")
        else:
            scale = s_w
            print(f"  [WARN] H/V disagree by {(ratio-1)*100:.0f}% — using horizontal only.")
    elif s_w:
        scale = s_w
    elif s_h:
        scale = s_h

    if scale and 0.003 < scale < 0.05:
        old = meta.get("scale_m_per_px")
        meta["scale_m_per_px"]   = float(scale)
        meta["wall_thickness_m"] = meta.get("wall_thickness_px", 0) * float(scale)
        print(f"  Scale: {old} → {scale:.6f} m/px  ✓")

    if total_w_m:
        meta["building_width_m"]  = float(total_w_m)
    if total_h_m:
        meta["building_height_m"] = float(total_h_m)

    if not scale:
        print("  No perimeter dimensions — scale unchanged.")

    # ── Room labels ───────────────────────────────────────────────────────────
    room_labels = llm_result.get("room_labels", {})
    for room in scene.get("rooms", []):
        new_label = room_labels.get(str(room["id"]))
        if new_label:
            room["label"] = new_label
    if room_labels:
        print(f"  Room labels: {room_labels}")

    # ── Opening types (door / window) ─────────────────────────────────────────
    opening_types = llm_result.get("openings", [])
    for i, op in enumerate(scene.get("openings", [])):
        if i < len(opening_types):
            op["type"] = opening_types[i]

    # ── Symbols catalogue ─────────────────────────────────────────────────────
    raw_symbols = llm_result.get("symbols", {})
    if raw_symbols:
        scene.setdefault("symbols", {})
        for key, val in raw_symbols.items():
            if isinstance(val, list):
                scene["symbols"][key] = val

        counts = {k: len(v) for k, v in scene["symbols"].items()
                  if isinstance(v, list) and v}
        if counts:
            print(f"  Symbols: { {k: v for k, v in counts.items()} }")

        # Propagate door_type onto matching openings for Stage 4 geometry
        sym_doors = raw_symbols.get("doors", [])
        if sym_doors:
            for op in scene.get("openings", []):
                if op.get("type") != "door":
                    continue
                match_dist = max(op["w"], op["h"]) * 3.0
                match = _nearest_symbol(op, sym_doors, match_dist)
                if match:
                    op.setdefault("door_type", match["type"])

        # Propagate window_type onto matching openings
        sym_windows = raw_symbols.get("windows", [])
        if sym_windows:
            for op in scene.get("openings", []):
                if op.get("type") != "window":
                    continue
                match_dist = max(op["w"], op["h"]) * 3.0
                match = _nearest_symbol(op, sym_windows, match_dist)
                if match:
                    op.setdefault("window_type", match["type"])

    scene["meta"]["llm_enhanced"] = True
    return scene


def main():
    parser = argparse.ArgumentParser(description="Stage LLM: Enhance scene with GPT-4o")
    parser.add_argument("image", help="Original input floor plan image")
    parser.add_argument("scene", help="Path to <stem>_scene.json from Stage 3")
    parser.add_argument("--output", default=None,
                        help="Output path (default: overwrites input scene JSON)")
    args = parser.parse_args()

    api_key = os.environ.get("OPENAI_API_KEY", "")
    if not api_key:
        print("[Stage LLM] OPENAI_API_KEY not set — skipping LLM enhancement.")
        sys.exit(0)

    image_path = Path(args.image)
    scene_path = Path(args.scene)

    if not image_path.exists():
        print(f"[ERROR] Image not found: {image_path}")
        sys.exit(1)
    if not scene_path.exists():
        print(f"[ERROR] Scene JSON not found: {scene_path}")
        sys.exit(1)

    with open(scene_path, encoding="utf-8") as f:
        scene = json.load(f)

    print(f"[Stage LLM] Image: {image_path.name}  |  Scene: {scene_path.name}")

    try:
        llm_result = query_gpt4o(image_path, scene, api_key)
        stem = scene_path.stem.replace("_scene", "")
        debug_path = scene_path.parent / f"{stem}_llm_debug.json"
        with open(debug_path, "w", encoding="utf-8") as f:
            json.dump(llm_result, f, indent=2)
        print(f"  Debug output  -> {debug_path}")
    except Exception as e:
        print(f"[WARN] GPT-4o call failed: {e} — scene unchanged.")
        sys.exit(0)

    scene = enhance_scene(scene, llm_result)

    out_path = Path(args.output) if args.output else scene_path
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(scene, f, indent=2)

    print(f"[Stage LLM] Enhanced scene -> {out_path}")
    return str(out_path)


if __name__ == "__main__":
    main()
