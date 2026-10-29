"""
predict_rv.py — Raster-to-Vector floor plan prediction
======================================================
Wraps the FloorplanTransformation PyTorch model for single-image inference.
Returns walls (as corner pairs), doors, icons in PlanLift-compatible format.
"""

import sys
import os
import io

import numpy as np
import cv2
import torch

# Add the FloorplanTransformation pytorch dir to path
_RV_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       "FloorplanTransformation", "pytorch")
if _RV_DIR not in sys.path:
    sys.path.insert(0, _RV_DIR)

from utils import NUM_WALL_CORNERS, NUM_CORNERS
from models.model import Model
from options import parse_args

# Lazy-loaded globals
_rv_model = None
_rv_device = None


def _load_model():
    global _rv_model, _rv_device
    if _rv_model is not None:
        return

    args = parse_args()
    _rv_device = torch.device("cuda" if torch.cuda.is_available() else "cpu")

    model = Model(args)
    ckpt = os.path.join(_RV_DIR, "checkpoint", "checkpoint.pth")
    state = torch.load(ckpt, map_location="cpu", weights_only=False)
    model.load_state_dict(state)
    model.to(_rv_device)
    model.eval()
    _rv_model = model
    print(f"[RV] FloorplanTransformation model loaded on {_rv_device}")


def predict_image(image_bytes: bytes) -> dict:
    """
    Run Raster-to-Vector prediction on a floor plan image.

    Returns dict compatible with PlanLift frontend:
    {
        "Width": int, "Height": int,
        "points": [{"x1","y1","x2","y2"}, ...],
        "classes": [{"name": "wall"|"door"|"window"}, ...],
        "corners": [{"x": int, "y": int, "type": int}, ...],
        "wallLines": [[idx1, idx2], ...],
        "doorLines": [[idx1, idx2], ...],
        "averageDoor": float
    }
    """
    _load_model()

    # Decode image
    nparr = np.frombuffer(image_bytes, np.uint8)
    img_orig = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
    if img_orig is None:
        raise ValueError("Could not decode image")

    orig_h, orig_w = img_orig.shape[:2]

    # Resize preserving aspect ratio, pad with white — matches training augmentSample()
    max_dim = max(orig_h, orig_w)
    scale_256 = 256.0 / max_dim
    new_w = int(orig_w * scale_256)
    new_h = int(orig_h * scale_256)
    img_resized = cv2.resize(img_orig, (new_w, new_h))
    # Place on 256x256 white canvas (model trained with white padding)
    canvas = np.full((256, 256, 3), 255, dtype=np.uint8)
    canvas[:new_h, :new_w] = img_resized
    # Normalize: keep BGR (model trained on cv2.imread BGR), [0,255] -> [-0.5, 0.5]
    img_norm = canvas.astype(np.float32) / 255.0 - 0.5
    # HWC -> CHW -> batch
    tensor = torch.from_numpy(img_norm.transpose(2, 0, 1)).unsqueeze(0).to(_rv_device)

    # Run model
    with torch.no_grad():
        corner_pred, icon_pred, room_pred = _rv_model(tensor)

    corner_heatmaps = corner_pred[0].cpu().numpy()
    icon_heatmaps = torch.nn.functional.softmax(icon_pred[0], dim=-1).cpu().numpy()
    room_heatmaps = torch.nn.functional.softmax(room_pred[0], dim=-1).cpu().numpy()

    # Run integer programming to get clean wall graph
    # Reload IP module each call to reset module-level globals
    import importlib
    import IP as _ip_module
    importlib.reload(_ip_module)
    from IP import reconstructFloorplan
    result = reconstructFloorplan(
        corner_heatmaps[:, :, :NUM_WALL_CORNERS],
        corner_heatmaps[:, :, NUM_WALL_CORNERS:NUM_WALL_CORNERS + 4],
        corner_heatmaps[:, :, -4:],
        icon_heatmaps, room_heatmaps,
        output_prefix=os.path.join(os.path.dirname(os.path.abspath(__file__)), "output", "rv_"),
        densityImage=None, gt_dict=None, gt=False,
        gap=-1, distanceThreshold=-1, lengthThreshold=-1,
        debug_prefix=os.path.join(os.path.dirname(os.path.abspath(__file__)), "output", "rv_debug"),
        enableAugmentation=True
    )

    if not result:
        return {"Width": orig_w, "Height": orig_h, "points": [], "classes": [], "averageDoor": 0}

    # Scale factor: model outputs in 256-space where image fits with aspect ratio preserved
    sx = orig_w / new_w   # = max_dim / 256 * (orig_w / max_dim) = orig_w / new_w
    sy = orig_h / new_h

    # Convert to PlanLift format
    points = []
    classes = []

    wall_points_raw = result.get("wall", [[], [], []])[0]
    wall_lines = result.get("wall", [[], [], []])[1]
    wall_labels = result.get("wall", [[], [], []])[2]

    door_points_raw = result.get("door", [[], [], []])[0]
    door_lines = result.get("door", [[], [], []])[1]

    icon_points_raw = result.get("icon", [[], [], []])[0]
    icons = result.get("icon", [[], [], []])[1]
    icon_types = result.get("icon", [[], [], []])[2]

    # Walls -> bounding boxes (like Mask R-CNN output)
    wall_thickness = 8  # default wall thickness in original-scale pixels
    door_widths = []

    for line in wall_lines:
        p1 = wall_points_raw[line[0]]
        p2 = wall_points_raw[line[1]]
        x1_256, y1_256 = p1[0], p1[1]
        x2_256, y2_256 = p2[0], p2[1]

        # Scale to original image coordinates
        x1 = int(x1_256 * sx)
        y1 = int(y1_256 * sy)
        x2 = int(x2_256 * sx)
        y2 = int(y2_256 * sy)

        # Determine if H or V wall and create bbox
        dx, dy = abs(x2 - x1), abs(y2 - y1)
        wt = max(int(wall_thickness * max(sx, sy) / 2), 8)
        if dx >= dy:  # horizontal
            bx1 = min(x1, x2)
            bx2 = max(x1, x2)
            by1 = (y1 + y2) // 2 - wt
            by2 = (y1 + y2) // 2 + wt
        else:  # vertical
            by1 = min(y1, y2)
            by2 = max(y1, y2)
            bx1 = (x1 + x2) // 2 - wt
            bx2 = (x1 + x2) // 2 + wt

        points.append({"x1": bx1, "y1": by1, "x2": bx2, "y2": by2})
        classes.append({"name": "wall"})

    # Doors -> bounding boxes
    for line in door_lines:
        p1 = door_points_raw[line[0]]
        p2 = door_points_raw[line[1]]
        x1 = int(p1[0] * sx)
        y1 = int(p1[1] * sy)
        x2 = int(p2[0] * sx)
        y2 = int(p2[1] * sy)

        dx, dy = abs(x2 - x1), abs(y2 - y1)
        door_w = max(dx, dy)
        door_widths.append(door_w)
        dt = max(int(wall_thickness * max(sx, sy) / 3), 5)

        if dx >= dy:  # horizontal door
            bx1 = min(x1, x2)
            bx2 = max(x1, x2)
            by1 = (y1 + y2) // 2 - dt
            by2 = (y1 + y2) // 2 + dt
        else:
            by1 = min(y1, y2)
            by2 = max(y1, y2)
            bx1 = (x1 + x2) // 2 - dt
            bx2 = (x1 + x2) // 2 + dt

        points.append({"x1": bx1, "y1": by1, "x2": bx2, "y2": by2})
        classes.append({"name": "door"})

    avg_door = np.mean(door_widths) if door_widths else 0

    return {
        "Width": orig_w,
        "Height": orig_h,
        "points": points,
        "classes": classes,
        "averageDoor": float(avg_door),
    }
