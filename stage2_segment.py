"""
Stage 2 - Semantic Segmentation

Two methods, chosen automatically:
  cv  (default when no trained weights) - classical CV wall detection.
      Works excellently on clean architectural drawings (CAD-style, white
      background, thick black walls).  No GPU needed.
  ml  - hg_furukawa_original (CubiCasa5k architecture).
      Requires weights/cubicasa_model.pkl (model_best_val_loss_var.pkl from
      the CubiCasa5k README Dropbox link).  Use --method ml to force.

Outputs saved to output/masks/:
  <stem>_walls.png      wall pixel mask (white = wall)
  <stem>_rooms.png      colour-coded room map
  <stem>_openings.png   door/window heatmap
  <stem>_masks.npz      raw arrays for Stage 3
"""

import argparse
import sys
from pathlib import Path

import cv2
import numpy as np
from PIL import Image

# ── Shared colour palette ─────────────────────────────────────────────────────
ROOM_COLORS = [
    (70,  130, 180), (255, 165,   0), (144, 238, 144),
    (255, 182, 193), (173, 216, 230), (255, 228, 181),
    (216, 191, 216), (240, 230, 140), (152, 251, 152),
    (255, 160, 122), (100, 149, 237), (255, 215,   0),
]

ROOM_LABELS = [
    "Living Room", "Bedroom", "Kitchen", "Bathroom", "Hallway",
    "Dining Room", "Office", "Storage", "Balcony", "Garage",
    "Laundry", "Other",
]


# ══════════════════════════════════════════════════════════════════════════════
#  METHOD A — Classical CV  (works on clean CAD-style drawings)
# ══════════════════════════════════════════════════════════════════════════════

def _detect_walls(gray: np.ndarray) -> tuple:
    """Return (wall_mask, dark_mask). wall_mask is thickened to realistic width."""
    H, W = gray.shape
    # Otsu's threshold — automatic, works on any floor plan regardless of scan darkness
    _, dark = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    k_noise = cv2.getStructuringElement(cv2.MORPH_RECT, (2, 2))
    dark = cv2.morphologyEx(dark, cv2.MORPH_OPEN, k_noise, iterations=1)

    # ── Pixel-level thickness filter ─────────────────────────────────────────
    # Compute the distance transform of all dark pixels. The value at each pixel
    # = distance to the nearest background pixel = local half-thickness.
    # Remove pixels deep inside thick blobs (staircase, solid furniture) before
    # CC analysis — they share pixels with wall lines, so CC-level filtering
    # alone cannot separate them.
    dist_all = cv2.distanceTransform(dark, cv2.DIST_L2, 5)
    nz = dist_all[dist_all > 0]
    median_r = float(np.median(nz)) if len(nz) else 3.0
    # Keep pixels within 5× median radius of an edge.  Walls (thin lines) are
    # fully preserved; thick staircase/furniture interiors are stripped away.
    thin_thresh = max(median_r * 5.0, 12.0)
    thin_dark = ((dark > 0) & (dist_all <= thin_thresh)).astype(np.uint8) * 255

    num_cc, labels_cc, stats_cc, _ = cv2.connectedComponentsWithStats(thin_dark, connectivity=8)
    min_area = max(300, int(H * W * 0.0005))
    large_area_thresh = int(H * W * 0.005)

    print(f"  CC total: {num_cc - 1}  min_area: {min_area}px"
          f"  median_r: {median_r:.1f}px  thin_thresh: {thin_thresh:.1f}px")

    wall_mask = np.zeros((H, W), dtype=np.uint8)
    n_kept = n_compact = 0

    for i in range(1, num_cc):
        area = stats_cc[i, cv2.CC_STAT_AREA]
        if area < min_area:
            continue

        x  = stats_cc[i, cv2.CC_STAT_LEFT]
        y  = stats_cc[i, cv2.CC_STAT_TOP]
        bw = stats_cc[i, cv2.CC_STAT_WIDTH]
        bh = stats_cc[i, cv2.CC_STAT_HEIGHT]
        cc_crop = (labels_cc[y:y+bh, x:x+bw] == i).astype(np.uint8)

        # ── Aspect ratio + solidity filter (small CCs only) ──────────────────
        # After pixel-level thinning, large CCs are wall networks — keep them.
        # Small CCs that are compact and solid are furniture outlines.
        if area < large_area_thresh:
            aspect  = max(bw, bh) / max(min(bw, bh), 1)
            cnts, _ = cv2.findContours(cc_crop, cv2.RETR_EXTERNAL,
                                        cv2.CHAIN_APPROX_SIMPLE)
            if cnts:
                hull_area = cv2.contourArea(cv2.convexHull(cnts[0]))
                solidity  = area / hull_area if hull_area > 0 else 1.0
            else:
                solidity = 1.0

            # Remove compact solid blobs: text, small annotations
            if aspect < 3.0 and solidity > 0.5:
                n_compact += 1
                continue

            # Remove hollow outlines: furniture borders, door arcs, window symbols.
            # Walls are solid filled bars (solidity ≈ 1.0); furniture borders are
            # hollow (e.g. a bed outline has solidity 0.05–0.15 because nearly all
            # the area inside the convex hull is empty).
            if aspect < 6.0 and solidity < 0.20:
                n_compact += 1
                continue

        wall_mask[labels_cc == i] = 255
        n_kept += 1

    print(f"  Kept: {n_kept}  Rejected compact: {n_compact}")

    k_close = cv2.getStructuringElement(cv2.MORPH_RECT, (3, 3))
    wall_mask = cv2.morphologyEx(wall_mask, cv2.MORPH_CLOSE, k_close, iterations=2)

    thick_px = max(8, int(min(H, W) * 0.015))
    k_thick  = cv2.getStructuringElement(cv2.MORPH_RECT, (thick_px, thick_px))
    wall_mask = cv2.dilate(wall_mask, k_thick)

    wall_pct = wall_mask.astype(bool).sum() / (H * W) * 100
    print(f"  Wall coverage (thickened {thick_px}px): {wall_pct:.1f}%")
    return wall_mask, dark


def _rooms_from_color(img_color_bgr: np.ndarray, wall_mask: np.ndarray) -> np.ndarray | None:
    """
    Detect rooms from the colour-preserved image.

    Works on floor plans where each room has a distinct background tint
    (light blue, beige, yellow, etc.).  Returns None when the image is
    mostly black-and-white and colour detection would give no useful result.
    """
    H, W = img_color_bgr.shape[:2]
    lab = cv2.cvtColor(img_color_bgr, cv2.COLOR_BGR2LAB)
    L, a, b = lab[:,:,0].astype(int), lab[:,:,1].astype(int), lab[:,:,2].astype(int)

    # Classify pixels:  dark = walls/ink,  white = background,  colored = rooms
    is_dark    = L < 80
    is_white   = (L > 210) & (np.abs(a - 128) < 18) & (np.abs(b - 128) < 18)
    is_colored = (~is_dark) & (~is_white)

    colored_mask = is_colored.astype(np.uint8) * 255
    colored_mask  = cv2.bitwise_and(colored_mask, cv2.bitwise_not(wall_mask))

    k = cv2.getStructuringElement(cv2.MORPH_RECT, (5, 5))
    colored_mask = cv2.morphologyEx(colored_mask, cv2.MORPH_OPEN,  k)
    colored_mask = cv2.morphologyEx(colored_mask, cv2.MORPH_CLOSE, k)

    colored_pct = colored_mask.astype(bool).sum() / (H * W) * 100
    print(f"  Colored-pixel coverage: {colored_pct:.1f}%")
    if colored_pct < 3.0:
        return None   # not a coloured floor plan — fall back

    bdr = 8
    colored_mask[:bdr, :] = 0;  colored_mask[-bdr:, :] = 0
    colored_mask[:, :bdr] = 0;  colored_mask[:, -bdr:] = 0

    num_r, labels_r, stats_r, _ = cv2.connectedComponentsWithStats(
        colored_mask, connectivity=8)

    border_mask = np.zeros((H, W), dtype=np.uint8)
    border_mask[:bdr+1, :] = 1;  border_mask[-(bdr+1):, :] = 1
    border_mask[:, :bdr+1] = 1;  border_mask[:, -(bdr+1):] = 1
    exterior_labels = set(int(x) for x in np.unique(labels_r[border_mask == 1])) - {0}

    MIN_ROOM_AREA = max(500, int(H * W * 0.002))
    room_map = np.zeros((H, W, 3), dtype=np.uint8)
    color_idx = 0
    for i in range(1, num_r):
        if i in exterior_labels:
            continue
        if stats_r[i, cv2.CC_STAT_AREA] < MIN_ROOM_AREA:
            continue
        room_map[labels_r == i] = ROOM_COLORS[color_idx % len(ROOM_COLORS)]
        color_idx += 1

    if color_idx == 0:
        return None
    print(f"  Rooms (colour): {color_idx}")
    return room_map


def _rooms_from_dilated_walls(wall_mask: np.ndarray) -> np.ndarray:
    """
    Detect rooms by dilating walls to bridge door gaps, then finding
    enclosed non-wall regions.  Used for black-and-white floor plans.
    """
    H, W = wall_mask.shape
    gap_px = max(20, int(min(H, W) * 0.04))
    k_gap  = cv2.getStructuringElement(cv2.MORPH_RECT, (gap_px, gap_px))
    walls_closed    = cv2.dilate(wall_mask, k_gap)
    non_wall_closed = cv2.bitwise_not(walls_closed)

    bdr = 3
    non_wall_closed[:bdr, :] = 0;  non_wall_closed[-bdr:, :] = 0
    non_wall_closed[:, :bdr] = 0;  non_wall_closed[:, -bdr:] = 0

    num_r, labels_r, stats_r, _ = cv2.connectedComponentsWithStats(
        non_wall_closed, connectivity=8)

    if num_r <= 1:
        return np.zeros((H, W, 3), dtype=np.uint8)

    border_mask = np.zeros((H, W), dtype=np.uint8)
    border_mask[:bdr+1, :] = 1;  border_mask[-(bdr+1):, :] = 1
    border_mask[:, :bdr+1] = 1;  border_mask[:, -(bdr+1):] = 1
    exterior_labels = set(int(x) for x in np.unique(labels_r[border_mask == 1])) - {0}

    MIN_ROOM_AREA = max(200, int(H * W * 0.0002))
    rooms_found = []
    for i in range(1, num_r):
        if i in exterior_labels:
            continue
        if stats_r[i, cv2.CC_STAT_AREA] < MIN_ROOM_AREA:
            continue
        rooms_found.append((stats_r[i, cv2.CC_STAT_AREA], i))
    rooms_found.sort(reverse=True)

    room_map = np.zeros((H, W, 3), dtype=np.uint8)
    for color_idx, (_, i) in enumerate(rooms_found):
        room_map[labels_r == i] = ROOM_COLORS[color_idx % len(ROOM_COLORS)]

    print(f"  Rooms (dilated-wall): {len(rooms_found)}")
    return room_map


def cv_segment(img_bgr: np.ndarray, img_color_bgr: np.ndarray = None,
               original_bgr: np.ndarray = None):
    """
    Classical CV segmentation for architectural floor plans.

    img_bgr       : preprocessed image (stage 1 output) — used for sizing
    img_color_bgr : colour-preserved image — enables colour-based room detection
    original_bgr  : original input image — used for wall detection when provided
                    (higher quality: no contrast-processing artefacts, full detail)
    """
    H_out, W_out = img_bgr.shape[:2]

    # Use original image for wall detection when available — it has sharper lines
    detect_src = original_bgr if original_bgr is not None else img_bgr
    gray = cv2.cvtColor(detect_src, cv2.COLOR_BGR2GRAY)
    # Resize to match output mask dimensions if the source has a different resolution
    if gray.shape[:2] != (H_out, W_out):
        gray = cv2.resize(gray, (W_out, H_out), interpolation=cv2.INTER_AREA)
    H, W = gray.shape

    # ── 1. Walls ──────────────────────────────────────────────────────────────
    wall_mask, dark = _detect_walls(gray)

    # ── 2. Rooms ──────────────────────────────────────────────────────────────
    room_map = None
    if img_color_bgr is not None:
        room_map = _rooms_from_color(img_color_bgr, wall_mask)
    if room_map is None:
        room_map = _rooms_from_dilated_walls(wall_mask)

    # ── 3. Openings ───────────────────────────────────────────────────────────
    opening_raw  = cv2.bitwise_and(dark, cv2.bitwise_not(wall_mask))
    k_near       = cv2.getStructuringElement(cv2.MORPH_RECT, (12, 12))
    near_wall    = cv2.dilate(wall_mask, k_near)
    opening_mask = cv2.bitwise_and(opening_raw, near_wall)
    k_op = cv2.getStructuringElement(cv2.MORPH_RECT, (3, 3))
    opening_mask = cv2.morphologyEx(opening_mask, cv2.MORPH_OPEN, k_op, iterations=1)

    return wall_mask, room_map, opening_mask


# ══════════════════════════════════════════════════════════════════════════════
#  METHOD B — ML  (hg_furukawa_original, needs trained weights)
# ══════════════════════════════════════════════════════════════════════════════

def ml_segment(img_path: Path, weights_path: Path, device_str: str):
    import torch
    import torch.nn.functional as F
    import torchvision.transforms as T

    CUBICASA_DIR = Path(__file__).parent / "CubiCasa5k-master"
    if str(CUBICASA_DIR) not in sys.path:
        sys.path.insert(0, str(CUBICASA_DIR))
    from floortrans.models.hg_furukawa_original import hg_furukawa_original
    from floortrans.models import model_1427 as m1427_module

    N_CLASSES = 44
    SPLIT     = [21, 12, 11]
    SKIP_ROOM = {0, 1, 2, 8}

    device = (torch.device("cuda") if torch.cuda.is_available()
              else torch.device("cpu")) if device_str == "auto" else torch.device(device_str)
    print(f"  ML device: {device}")

    # Build model
    model = hg_furukawa_original(n_classes=51)
    # Copy backbone
    backbone_path = CUBICASA_DIR / "floortrans" / "models" / "model_1427.pth"
    if backbone_path.exists():
        state = torch.load(str(backbone_path), map_location=device, weights_only=False)
        src = m1427_module.model_1427
        src.load_state_dict(state)
        n = sum(1 for sp, dp in zip(src.parameters(), model.parameters())
                if sp.shape == dp.shape and (dp.data.copy_(sp.data) is not None))
        print(f"  Backbone: {n} tensors from model_1427.pth")

    model.conv4_   = torch.nn.Conv2d(256, N_CLASSES, bias=True, kernel_size=1)
    model.upsample = torch.nn.ConvTranspose2d(N_CLASSES, N_CLASSES, kernel_size=4, stride=4)

    ckpt = torch.load(str(weights_path), map_location=device, weights_only=False)
    state = ckpt.get("model_state", ckpt)
    model.load_state_dict(state, strict=True)
    print("  [OK] Trained CubiCasa weights loaded.")

    model.to(device).eval()

    img = Image.open(str(img_path)).convert("RGB")
    H, W = img.size[1], img.size[0]

    # CubiCasa was trained on 512×512 — resize for inference, upsample outputs back.
    INF_SIZE = 512
    img_resized = img.resize((INF_SIZE, INF_SIZE), Image.LANCZOS)
    tfm = T.Compose([T.ToTensor(),
                     T.Normalize(mean=[0.485,0.456,0.406], std=[0.229,0.224,0.225])])
    tensor = tfm(img_resized).unsqueeze(0).to(device)
    print(f"  Inference at {INF_SIZE}×{INF_SIZE}, output upsampled to {W}×{H}")

    with torch.no_grad():
        out_512 = model(tensor)   # raw output at 512×512

    # ── CubiCasa post-processing at inference resolution (512×512) ────────
    # get_polygons() constants (gap=10, etc.) were tuned for 512×512.
    # Run it BEFORE upsampling so coordinates are correct, then scale up.
    opening_polys: np.ndarray | None = None
    try:
        from floortrans.post_prosessing import get_polygons
        out_sq = out_512.squeeze(0)
        ht, rt, it = torch.split(out_sq, SPLIT, dim=0)
        hm512  = ht.cpu().numpy()
        rm512  = torch.softmax(rt, dim=0).cpu().numpy()
        ic512  = torch.softmax(it, dim=0).cpu().numpy()
        polys, p_types, _, _ = get_polygons(
            (hm512, rm512, ic512),
            threshold=0.5,
            all_opening_types=[1, 2],   # 1=window, 2=door
        )
        scale_x = W / INF_SIZE
        scale_y = H / INF_SIZE
        rows: list[list[float]] = []
        for pol, pt in zip(polys, p_types):
            if pt.get("type") == "icon" and int(pt["class"]) in (1, 2):
                # Scale polygon corners from 512-space to native image space
                cx = float(pol[:, 0].mean()) * scale_x
                cy = float(pol[:, 1].mean()) * scale_y
                width = float(max(
                    (pol[:, 0].max() - pol[:, 0].min()) * scale_x,
                    (pol[:, 1].max() - pol[:, 1].min()) * scale_y,
                ))
                rows.append([cx, cy, width, float(pt["class"])])
        if rows:
            opening_polys = np.array(rows, dtype=np.float32)
            n_d = sum(1 for r in rows if r[3] == 2)
            n_w = sum(1 for r in rows if r[3] == 1)
            print(f"  CubiCasa post-proc: {n_d} door(s), {n_w} window(s) "
                  f"[wall-snapped, scaled {INF_SIZE}→{W}×{H}]")
    except Exception as exc:
        print(f"  [WARN] CubiCasa post-proc skipped: {exc!r} — heatmap fallback active")

    # Upsample full output to native resolution for masks / heatmaps
    out = F.interpolate(out_512, size=(H, W), mode='bilinear', align_corners=False).squeeze(0)
    heatmaps_t, rooms_t, icons_t = torch.split(out, SPLIT, dim=0)
    rooms = torch.softmax(rooms_t, dim=0).cpu().numpy()
    icons = torch.softmax(icons_t, dim=0).cpu().numpy()

    wall_mask = (rooms[2] > 0.3).astype(np.uint8) * 255

    room_argmax = np.argmax(rooms, axis=0)
    room_map = np.zeros((H, W, 3), dtype=np.uint8)
    for c in range(12):
        if c in SKIP_ROOM: continue
        m = room_argmax == c
        if m.any():
            room_map[m] = ROOM_COLORS[c % len(ROOM_COLORS)]

    # icons[1] = window probability, icons[2] = door probability
    door_hmap = icons[2].astype(np.float32)
    win_hmap  = icons[1].astype(np.float32)
    opening_mask = (np.maximum(win_hmap, door_hmap) > 0.2).astype(np.uint8) * 255

    return wall_mask, room_map, opening_mask, door_hmap, win_hmap, opening_polys


# ══════════════════════════════════════════════════════════════════════════════
#  Method C — MitUNet  (smp.Unet + mit_b4 encoder + scSE decoder)
# ══════════════════════════════════════════════════════════════════════════════

def mitunet_segment(img_path: Path, weights_path: Path, device_str: str):
    """
    MitUNet inference: Unet with MIT-B4 (Segformer) encoder and scSE attention.
    Produces a binary wall mask; rooms & openings are derived via classical CV.
    """
    import torch
    import segmentation_models_pytorch as smp
    import albumentations as A
    from albumentations.pytorch import ToTensorV2

    device = (torch.device("cuda") if torch.cuda.is_available()
              else torch.device("cpu")) if device_str == "auto" else torch.device(device_str)
    print(f"  MitUNet device: {device}")

    # Build model — Unet body with MixTransformer-B4 encoder and scSE decoder attention
    model = smp.Unet(
        encoder_name="mit_b4",
        encoder_weights=None,
        in_channels=3,
        classes=1,
        decoder_attention_type="scse",
    )

    state = torch.load(str(weights_path), map_location=device, weights_only=False)
    # Handle both raw state-dict and checkpoint dicts
    if isinstance(state, dict) and "state_dict" in state:
        state = state["state_dict"]
    elif isinstance(state, dict) and "model" in state:
        state = state["model"]
    model.load_state_dict(state, strict=False)
    model.to(device).eval()
    print("  [OK] MitUNet weights loaded.")

    img_pil = Image.open(str(img_path)).convert("RGB")
    H, W = img_pil.size[1], img_pil.size[0]
    img_np = np.array(img_pil)

    tfm = A.Compose([
        A.Resize(512, 512),
        A.Normalize(mean=(0.485, 0.456, 0.406), std=(0.229, 0.224, 0.225)),
        ToTensorV2(),
    ])
    aug    = tfm(image=img_np)
    tensor = aug["image"].unsqueeze(0).to(device)

    with torch.no_grad():
        logits  = model(tensor)
        mask512 = (torch.sigmoid(logits) > 0.5).float().squeeze().cpu().numpy()

    # Upsample back to native resolution
    wall_mask = (cv2.resize(mask512, (W, H), interpolation=cv2.INTER_LINEAR) > 0.5).astype(np.uint8) * 255
    print(f"  Inference: 512×512 → upsampled to {W}×{H}")

    # Rooms via wall dilation (same as CV path)
    room_map = _rooms_from_dilated_walls(wall_mask)

    # Openings: dark pixels near walls that are NOT walls
    img_bgr = cv2.cvtColor(img_np, cv2.COLOR_RGB2BGR)
    gray = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2GRAY)
    _, dark = cv2.threshold(gray, 200, 255, cv2.THRESH_BINARY_INV)
    opening_raw  = cv2.bitwise_and(dark, cv2.bitwise_not(wall_mask))
    k_near        = cv2.getStructuringElement(cv2.MORPH_RECT, (12, 12))
    near_wall     = cv2.dilate(wall_mask, k_near)
    opening_mask  = cv2.bitwise_and(opening_raw, near_wall)
    k_op          = cv2.getStructuringElement(cv2.MORPH_RECT, (3, 3))
    opening_mask  = cv2.morphologyEx(opening_mask, cv2.MORPH_OPEN, k_op)

    return wall_mask, room_map, opening_mask


# ══════════════════════════════════════════════════════════════════════════════
#  Main
# ══════════════════════════════════════════════════════════════════════════════

def _find_mitunet_weights() -> Path | None:
    """Return first *.pth file in the weights/ folder, or None."""
    w_dir = Path("weights")
    if w_dir.exists():
        for p in w_dir.glob("*.pth"):
            return p
    return None


def main():
    parser = argparse.ArgumentParser(description="Stage 2: Segment floor plan image")
    parser.add_argument("input",  help="Preprocessed floor plan image (from Stage 1)")
    parser.add_argument("--weights",    default="weights/model_best_val_loss_var.pkl")
    parser.add_argument("--output-dir", default="output/masks")
    parser.add_argument("--device",     default="auto",
                        choices=["auto", "cuda", "cpu"])
    parser.add_argument("--method",     default="auto",
                        choices=["auto", "cv", "ml", "mitunet"],
                        help="auto = mitunet if *.pth found, ml if *.pkl found, else cv")
    parser.add_argument("--original",   default=None,
                        help="Original (non-preprocessed) input image; used by --method cv "
                             "for sharper wall detection")
    args = parser.parse_args()

    input_path   = Path(args.input)
    weights_path = Path(args.weights)

    if not input_path.exists():
        print(f"[ERROR] Input not found: {input_path}")
        sys.exit(1)

    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = input_path.stem.replace("_preprocessed", "")

    # Auto-select method based on available weights.
    # Prefer CubiCasa ml (pkl) over mitunet (.pth) because CubiCasa outputs
    # separate door/window heatmaps; mitunet only produces wall segmentation.
    method = args.method
    if method == "auto":
        if weights_path.exists():
            method = "ml"
        elif _find_mitunet_weights() is not None:
            method = "mitunet"
        else:
            method = "cv"

    label = {
        "cv":      "Classical CV (no weights)",
        "ml":      "CubiCasa ML",
        "mitunet": "MitUNet (MIT-B4 + scSE)",
    }.get(method, method.upper())
    print(f"[Stage 2] Method: {label}")

    img_bgr = cv2.imread(str(input_path))
    door_hmap: np.ndarray | None = None
    win_hmap:  np.ndarray | None = None

    if method == "cv":
        # Try to load the colour-preserved companion image from stage 1
        color_path = input_path.parent / (input_path.stem.replace("_preprocessed", "")
                                          + "_preprocessed_color.png")
        img_color_bgr = cv2.imread(str(color_path)) if color_path.exists() else None
        if img_color_bgr is not None:
            print(f"  Colour image: {color_path.name}")
        # Load original image for sharper wall detection
        original_bgr = None
        if args.original:
            original_bgr = cv2.imread(args.original)
            if original_bgr is not None:
                print(f"  Original image for wall detection: {Path(args.original).name}")
        wall_mask, room_map, opening_mask = cv_segment(img_bgr, img_color_bgr, original_bgr)
    elif method == "mitunet":
        mitunet_w = _find_mitunet_weights()
        if mitunet_w is None:
            print("[ERROR] --method mitunet requires a *.pth weights file in weights/")
            sys.exit(1)
        print(f"  Weights: {mitunet_w}")
        # Prefer original colour image — MitUNet was trained on colour floor plans;
        # passing the grayscale-enhanced preprocessed image loses colour cues.
        seg_input = Path(args.original) if args.original and Path(args.original).exists() \
                    else input_path
        if seg_input != input_path:
            print(f"  Using original image for MitUNet: {seg_input.name}")
        wall_mask, room_map, opening_mask = mitunet_segment(seg_input, mitunet_w, args.device)
    else:  # ml — CubiCasa5k, returns separate door/window heatmaps + exact polygons
        if not weights_path.exists():
            print(f"[ERROR] --method ml requires trained weights at {weights_path}")
            sys.exit(1)
        wall_mask, room_map, opening_mask, door_hmap, win_hmap, opening_polys = ml_segment(
            input_path, weights_path, args.device)

    # Stats
    H, W = wall_mask.shape[:2]
    wall_pct    = wall_mask.astype(bool).sum() / (H * W) * 100
    rm = room_map.astype(np.int32)
    n_rooms     = len(np.unique(
                    np.where(room_map.any(axis=2),
                             rm[:,:,0] * 65536 + rm[:,:,1] * 256 + rm[:,:,2],
                             0))) - 1   # subtract 0 (background)
    opening_pct = opening_mask.astype(bool).sum() / (H * W) * 100
    print(f"  Wall coverage:    {wall_pct:.1f}%")
    print(f"  Room regions:     {max(0, n_rooms)}")
    print(f"  Opening coverage: {opening_pct:.1f}%")

    wall_out    = out_dir / f"{stem}_walls.png"
    room_out    = out_dir / f"{stem}_rooms.png"
    opening_out = out_dir / f"{stem}_openings.png"
    npz_out     = out_dir / f"{stem}_masks.npz"

    cv2.imwrite(str(wall_out),    wall_mask)
    cv2.imwrite(str(room_out),    cv2.cvtColor(room_map, cv2.COLOR_RGB2BGR))
    cv2.imwrite(str(opening_out), opening_mask)

    # prediction array: label each room region by color index
    pred = np.zeros((H, W), dtype=np.uint8)
    for i, color in enumerate(ROOM_COLORS):
        m = np.all(room_map == color, axis=2)
        pred[m] = i + 2   # offset so 0=bg, 1=wall, 2+=rooms
    pred[wall_mask > 0] = 1

    npz_data: dict = dict(prediction=pred, walls=wall_mask,
                          rooms=room_map, openings=opening_mask)
    if door_hmap is not None:
        npz_data["doors_ml"]   = door_hmap
        npz_data["windows_ml"] = win_hmap
    if opening_polys is not None and len(opening_polys) > 0:
        npz_data["opening_polys"] = opening_polys  # [N,4]: cx,cy,width,class_id
    np.savez(str(npz_out), **npz_data)

    print(f"[Stage 2] Masks saved:")
    print(f"  Wall mask  -> {wall_out}")
    print(f"  Room map   -> {room_out}")
    print(f"  Openings   -> {opening_out}")
    print(f"  Raw arrays -> {npz_out}")
    return str(npz_out)


if __name__ == "__main__":
    main()
