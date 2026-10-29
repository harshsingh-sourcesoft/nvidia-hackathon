"""
Stage 1 — Input Preprocessing
Accepts: PNG, JPG, BMP, TIFF, PDF (first page), SVG
Outputs: normalised 512x512 or 1024x1024 RGB PNG ready for segmentation
"""

import argparse
import sys
from pathlib import Path

import cv2
import numpy as np
from PIL import Image


TARGET_SIZE = 0  # 0 = native resolution (recommended); set to 512/1024 for fixed size


def load_image(path: Path) -> np.ndarray:
    """Load image from any supported format."""
    suffix = path.suffix.lower()

    if suffix == ".svg":
        try:
            import cairosvg, io
            # Render at 3× scale for high-detail rasterisation
            png_bytes = cairosvg.svg2png(url=str(path), scale=3.0)
            img_pil = Image.open(io.BytesIO(png_bytes)).convert("RGB")
            return np.array(img_pil)
        except ImportError:
            print("[WARN] cairosvg not installed. Install with: pip install cairosvg")
            print("       Falling back to Pillow (may fail for SVG)...")

    if suffix == ".pdf":
        try:
            import fitz  # PyMuPDF
            doc = fitz.open(str(path))
            page = doc[0]
            mat = fitz.Matrix(3.0, 3.0)  # 3× zoom = ~216 DPI
            pix = page.get_pixmap(matrix=mat)
            img = np.frombuffer(pix.samples, dtype=np.uint8)
            img = img.reshape(pix.height, pix.width, pix.n)
            if pix.n == 4:
                img = cv2.cvtColor(img, cv2.COLOR_RGBA2RGB)
            return img
        except ImportError:
            print("[WARN] PyMuPDF not installed. Install with: pip install pymupdf")
            print("       Trying to open PDF as image directly...")

    img_pil = Image.open(str(path)).convert("RGB")
    return np.array(img_pil)


def pad_and_resize(img: np.ndarray, target_size: int) -> np.ndarray:
    """Pad to square and resize. If target_size == 0, return image unchanged."""
    if target_size == 0:
        return img
    h, w = img.shape[:2]
    side  = max(h, w)
    channels = img.shape[2] if img.ndim == 3 else 1
    padded = np.full((side, side, channels) if channels > 1 else (side, side),
                     255, dtype=np.uint8)
    y_off, x_off = (side - h) // 2, (side - w) // 2
    padded[y_off:y_off + h, x_off:x_off + w] = img
    return cv2.resize(padded, (target_size, target_size), interpolation=cv2.INTER_AREA)


def preprocess(img: np.ndarray, target_size: int = TARGET_SIZE) -> np.ndarray:
    """CLAHE grayscale enhancement + optional resize. Returns 3-channel image."""
    gray    = cv2.cvtColor(img, cv2.COLOR_RGB2GRAY)
    clahe   = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
    gray_eq = clahe.apply(gray)
    enhanced = cv2.cvtColor(gray_eq, cv2.COLOR_GRAY2RGB)
    return pad_and_resize(enhanced, target_size)


def preprocess_color(img: np.ndarray, target_size: int = TARGET_SIZE) -> np.ndarray:
    """Colour-preserving optional resize. Used by stage2 for room detection."""
    return pad_and_resize(img, target_size)


def main():
    parser = argparse.ArgumentParser(description="Stage 1: Preprocess floor plan image")
    parser.add_argument("input", help="Path to input floor plan image")
    parser.add_argument("--output", default=None, help="Output path (default: output/masks/<stem>_preprocessed.png)")
    parser.add_argument("--size", type=int, default=TARGET_SIZE, help="Target resolution (0 = native, default)")
    args = parser.parse_args()

    input_path = Path(args.input)
    if not input_path.exists():
        print(f"[ERROR] File not found: {input_path}")
        sys.exit(1)

    output_path = Path(args.output) if args.output else Path("output/masks") / f"{input_path.stem}_preprocessed.png"
    output_path.parent.mkdir(parents=True, exist_ok=True)

    print(f"[Stage 1] Loading: {input_path}")
    img = load_image(input_path)
    print(f"  Original size: {img.shape[1]}x{img.shape[0]}")

    # Grayscale-enhanced output (for wall detection in stage 2)
    processed = preprocess(img, args.size)
    cv2.imwrite(str(output_path), cv2.cvtColor(processed, cv2.COLOR_RGB2BGR))

    # Color-preserved output (for room detection in stage 2)
    color_path = output_path.parent / (output_path.stem + "_color.png")
    color_out  = preprocess_color(img, args.size)
    cv2.imwrite(str(color_path), cv2.cvtColor(color_out, cv2.COLOR_RGB2BGR))

    size_label = "native" if args.size == 0 else f"{args.size}x{args.size}"
    print(f"  Preprocessed: {size_label}")
    print(f"[Stage 1] Saved -> {output_path}")
    print(f"[Stage 1] Color -> {color_path}")
    return str(output_path)


if __name__ == "__main__":
    main()
