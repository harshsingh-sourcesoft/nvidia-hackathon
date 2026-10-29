"""
sketch_to_floorplan.py
======================
Utilities for cleaning a hand-drawn sketch and rendering a clean architectural
floor plan from structured feature data.

Feature extraction (room polygons, doors, windows) is performed by the
manual editor (editor.html) — no AI model is involved in this module.
"""

import io
import math
import os

import numpy as np
from PIL import Image, ImageDraw


# ─────────────────────────────────────────────────────────────────────────────
# Room fill colours (used by render_clean_plan)
# ─────────────────────────────────────────────────────────────────────────────

_ROOM_FILLS = {
    "MASTER":    (245, 235, 225),
    "BEDROOM":   (250, 243, 235),
    "BATHROOM":  (230, 242, 250),
    "TOILET":    (230, 242, 250),
    "WC":        (230, 242, 250),
    "KITCHEN":   (250, 250, 232),
    "LIVING":    (237, 250, 237),
    "DINING":    (250, 247, 232),
    "HALLWAY":   (245, 245, 245),
    "CORRIDOR":  (245, 245, 245),
    "ENTRY":     (242, 242, 250),
    "ENTRANCE":  (242, 242, 250),
    "CLOSET":    (242, 238, 250),
    "CUPBOARD":  (242, 238, 250),
    "GARAGE":    (238, 238, 238),
    "OFFICE":    (232, 242, 250),
    "LAUNDRY":   (250, 245, 235),
    "UTILITY":   (238, 242, 238),
    "SAUNA":     (250, 235, 225),
    "STAIR":     (242, 242, 242),
    "CPD":       (245, 245, 245),
    "DEFAULT":   (248, 248, 248),
}


# ─────────────────────────────────────────────────────────────────────────────
# Render clean architectural floor plan from a features dict
# ─────────────────────────────────────────────────────────────────────────────

def render_clean_plan(features: dict, output_px: int = 1200) -> bytes:
    """
    Render a clean computer-generated floor plan from a features dict.
    The dict must have keys: image_width, image_height, rooms, doors, windows.
    Output is a PNG suitable for Mask R-CNN inference.
    """
    src_w = float(features.get("image_width", 1000))
    src_h = float(features.get("image_height", src_w * 0.75))
    scale = output_px / src_w
    dst_w = output_px
    dst_h = max(1, int(src_h * scale))

    WALL_T = max(3, int(5 * scale * 0.005 * src_w))
    WALL_T = min(WALL_T, 8)
    BLACK  = (30, 30, 30)
    WHITE  = (255, 255, 255)

    def px(x, y):
        return (int(x * scale), int(y * scale))

    img  = Image.new("RGB", (dst_w, dst_h), WHITE)
    draw = ImageDraw.Draw(img)

    # ── 1. Room fills ─────────────────────────────────────────────────────────
    for room in features.get("rooms", []):
        poly = room.get("polygon", [])
        if len(poly) < 3:
            continue
        pts = [px(p[0], p[1]) for p in poly]
        name = (room.get("name") or "").upper()
        color = _ROOM_FILLS["DEFAULT"]
        for key, col in _ROOM_FILLS.items():
            if key in name:
                color = col
                break
        draw.polygon(pts, fill=color, outline=None)

    # ── 2. Wall outlines ──────────────────────────────────────────────────────
    for room in features.get("rooms", []):
        poly = room.get("polygon", [])
        if len(poly) < 3:
            continue
        pts = [px(p[0], p[1]) for p in poly]
        for i in range(len(pts)):
            p1 = pts[i]
            p2 = pts[(i + 1) % len(pts)]
            draw.line([p1, p2], fill=BLACK, width=WALL_T)

    # ── 3. Doors: gap + arc ───────────────────────────────────────────────────
    for door in features.get("doors", []):
        cx, cy = door.get("cx", 0), door.get("cy", 0)
        dw     = door.get("width", 65)
        ang    = door.get("wall_angle", 0)
        swing  = door.get("swing", "cw")
        half   = dw / 2

        pcx, pcy = px(cx, cy)
        phalf    = int(half * scale)
        clear_t  = WALL_T + 4

        if ang == 0:
            draw.rectangle([pcx - phalf, pcy - clear_t,
                            pcx + phalf, pcy + clear_t], fill=WHITE)
            hx = pcx - phalf
            r  = phalf * 2
            bbox = [hx - r, pcy - r, hx + r, pcy + r]
            if swing == "cw":
                draw.arc(bbox, start=0, end=90, fill=BLACK, width=2)
                draw.line([(hx, pcy), (hx, pcy + r)], fill=BLACK, width=1)
            else:
                draw.arc(bbox, start=270, end=360, fill=BLACK, width=2)
                draw.line([(hx, pcy), (hx, pcy - r)], fill=BLACK, width=1)
        else:
            draw.rectangle([pcx - clear_t, pcy - phalf,
                            pcx + clear_t, pcy + phalf], fill=WHITE)
            hy = pcy - phalf
            r  = phalf * 2
            bbox = [pcx - r, hy - r, pcx + r, hy + r]
            if swing == "cw":
                draw.arc(bbox, start=0, end=90, fill=BLACK, width=2)
                draw.line([(pcx, hy), (pcx + r, hy)], fill=BLACK, width=1)
            else:
                draw.arc(bbox, start=90, end=180, fill=BLACK, width=2)
                draw.line([(pcx, hy), (pcx - r, hy)], fill=BLACK, width=1)

    # ── 4. Windows: triple parallel lines ─────────────────────────────────────
    for win in features.get("windows", []):
        cx, cy = win.get("cx", 0), win.get("cy", 0)
        ww     = win.get("width", 80)
        ang    = win.get("wall_angle", 0)

        pcx, pcy = px(cx, cy)
        phalf    = int(ww / 2 * scale)
        clear_t  = WALL_T + 4
        gap      = max(3, int(3 * scale))

        if ang == 0:
            draw.rectangle([pcx - phalf, pcy - clear_t,
                            pcx + phalf, pcy + clear_t], fill=WHITE)
            for offset in [-gap, 0, gap]:
                draw.line([(pcx - phalf, pcy + offset),
                           (pcx + phalf, pcy + offset)],
                          fill=BLACK, width=1)
        else:
            draw.rectangle([pcx - clear_t, pcy - phalf,
                            pcx + clear_t, pcy + phalf], fill=WHITE)
            for offset in [-gap, 0, gap]:
                draw.line([(pcx + offset, pcy - phalf),
                           (pcx + offset, pcy + phalf)],
                          fill=BLACK, width=1)

    # ── 5. Room labels ────────────────────────────────────────────────────────
    try:
        from PIL import ImageFont
        font_sz = max(11, int(12 * scale))
        font = None
        for path in ("C:/Windows/Fonts/calibri.ttf",
                     "C:/Windows/Fonts/arial.ttf"):
            try:
                font = ImageFont.truetype(path, font_sz)
                break
            except Exception:
                pass
        if font is None:
            font = ImageFont.load_default()
    except Exception:
        font = None

    for room in features.get("rooms", []):
        name = (room.get("name") or "").strip().upper()
        poly = room.get("polygon", [])
        if not name or len(poly) < 3:
            continue
        cx = sum(p[0] for p in poly) / len(poly)
        cy = sum(p[1] for p in poly) / len(poly)
        rpx, rpy = px(cx, cy)

        if font:
            try:
                bb = draw.textbbox((0, 0), name, font=font)
                tw, th = bb[2] - bb[0], bb[3] - bb[1]
                for ox, oy in [(-1,-1),(1,-1),(-1,1),(1,1)]:
                    draw.text((rpx - tw//2 + ox, rpy - th//2 + oy),
                              name, fill=(255,255,255), font=font)
                draw.text((rpx - tw//2, rpy - th//2),
                          name, fill=(60, 60, 70), font=font)
            except Exception:
                pass

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


# ─────────────────────────────────────────────────────────────────────────────
# OpenCV sketch cleaner (binarize uploaded image for Mask R-CNN)
# ─────────────────────────────────────────────────────────────────────────────

def clean_sketch(image_bytes: bytes) -> bytes:
    import cv2
    nparr = np.frombuffer(image_bytes, np.uint8)
    img = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
    if img is None:
        raise ValueError("Could not decode image")
    gray   = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    gray   = cv2.GaussianBlur(gray, (3, 3), 0)
    binary = cv2.adaptiveThreshold(gray, 255,
        cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY, blockSize=21, C=10)
    result = cv2.cvtColor(binary, cv2.COLOR_GRAY2BGR)
    _, png  = cv2.imencode('.png', result)
    return png.tobytes()


# ─────────────────────────────────────────────────────────────────────────────
# render_preview — kept for api_server_mrcnn.py compatibility
# ─────────────────────────────────────────────────────────────────────────────

def render_preview(original_bytes: bytes, cleaned_png: bytes) -> tuple:
    """
    Returns (preview_png, clean_plan_png, features_dict).
    Feature extraction was previously done by Claude Vision; since floor plans
    are now drawn manually in the editor, this returns the cleaned image
    unchanged with an empty features dict.
    """
    features = {"rooms": [], "doors": [], "windows": []}
    return cleaned_png, cleaned_png, features


def render_floorplan(data: dict, output_px: int = 1200) -> bytes:
    return data.get("cleaned", b"")
