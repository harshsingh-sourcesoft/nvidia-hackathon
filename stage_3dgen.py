"""
Stage 3D-Gen — AI Image-to-3D Mesh Generation
Takes the floor plan image and generates a 3D mesh via Hunyuan3D-2.1.

Active backend : Hunyuan3D-2.1  (tencent/Hunyuan3D-2.1)
Commented out  : TRELLIS        (JeffreyXiang/TRELLIS-image-large)

Outputs saved to <output_dir>/<stem>_ai.glb
"""

import argparse
import os
import sys
from pathlib import Path

import torch
from PIL import Image

# ── Hunyuan3D-2.1 local repo path ────────────────────────────────────────────
HUNYUAN_DIR = Path(__file__).parent / "Hunyuan3D-2.1-main"
sys.path.insert(0, str(HUNYUAN_DIR))
sys.path.insert(0, str(HUNYUAN_DIR / "hy3dshape"))
sys.path.insert(0, str(HUNYUAN_DIR / "hy3dpaint"))
sys.path.insert(0, str(HUNYUAN_DIR / "hy3dpaint" / "hunyuanpaintpbr" / "unet"))

try:
    from torchvision_fix import apply_fix
    apply_fix()
except Exception:
    pass

from hy3dshape.rembg import BackgroundRemover
from hy3dshape.pipelines import Hunyuan3DDiTFlowMatchingPipeline

# ── Inactive backend: TRELLIS ─────────────────────────────────────────────────
# from trellis.pipelines import TrellisImageTo3DPipeline
# from trellis.utils import postprocessing_utils

HUNYUAN_MODEL_ID = "tencent/Hunyuan3D-2.1"
# TRELLIS_MODEL_ID = "JeffreyXiang/TRELLIS-image-large"


def _load_image(path: str) -> Image.Image:
    img = Image.open(path).convert("RGBA")
    alpha = img.split()[-1]
    if alpha.getextrema()[0] == 255:
        rembg = BackgroundRemover()
        img = rembg(img)
    return img


# ── Hunyuan3D-2.1 ─────────────────────────────────────────────────────────────

def run_hunyuan3d(image: Image.Image, image_path: str, output_path: str, with_texture: bool = False):
    print(f"  Loading Hunyuan3D-2.1 shape model ({HUNYUAN_MODEL_ID})...")
    shape_pipe = Hunyuan3DDiTFlowMatchingPipeline.from_pretrained(HUNYUAN_MODEL_ID)

    print("  Generating 3D shape...")
    mesh = shape_pipe(image=image)[0]

    if with_texture:
        import tempfile
        from textureGenPipeline import Hunyuan3DPaintConfig, Hunyuan3DPaintPipeline

        print("  Exporting untextured mesh for texture pass...")
        tmp = tempfile.NamedTemporaryFile(suffix=".glb", delete=False)
        tmp.close()
        mesh.export(tmp.name)

        print("  Loading PBR texture pipeline...")
        conf = Hunyuan3DPaintConfig(max_num_view=6, resolution=512)
        conf.realesrgan_ckpt_path = str(HUNYUAN_DIR / "hy3dpaint" / "ckpt" / "RealESRGAN_x4plus.pth")
        conf.multiview_cfg_path   = str(HUNYUAN_DIR / "hy3dpaint" / "cfgs" / "hunyuan-paint-pbr.yaml")
        conf.custom_pipeline      = str(HUNYUAN_DIR / "hy3dpaint" / "hunyuanpaintpbr")
        tex_pipe = Hunyuan3DPaintPipeline(conf)

        print("  Applying PBR texture...")
        tex_pipe(
            mesh_path=tmp.name,
            image_path=image_path,
            output_mesh_path=output_path,
        )
        os.unlink(tmp.name)
    else:
        mesh.export(output_path)


# ── TRELLIS (inactive) ────────────────────────────────────────────────────────

# def run_trellis(image: Image.Image, device: str, **_):
#     print(f"  Loading TRELLIS pipeline ({TRELLIS_MODEL_ID})...")
#     pipeline = TrellisImageTo3DPipeline.from_pretrained(TRELLIS_MODEL_ID)
#     pipeline = pipeline.to(device)
#     outputs = pipeline.run(image)
#     return postprocessing_utils.to_glb(
#         outputs["gaussian"][0], outputs["mesh"][0],
#         simplify=0.95, texture_size=1024,
#     )


def main():
    parser = argparse.ArgumentParser(
        description="Stage 3D-Gen: AI image-to-3D mesh generation"
    )
    parser.add_argument("input", help="Path to floor plan image (PNG/JPG/WEBP)")
    parser.add_argument("--output-dir", default="output/models",
                        help="Directory to save .glb output (default: output/models)")
    parser.add_argument("--device", default=None,
                        help="PyTorch device (default: cuda if available, else cpu)")
    parser.add_argument("--texture", action="store_true",
                        help="Also run Hunyuan3D-2.1 PBR texture generation pass")
    args = parser.parse_args()

    if args.device is None:
        args.device = "cuda" if torch.cuda.is_available() else "cpu"

    img_path = Path(args.input)
    if not img_path.exists():
        print(f"[ERROR] Input file not found: {img_path}")
        sys.exit(1)

    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    print(f"[Stage 3D-Gen] Input  : {img_path}")
    print(f"[Stage 3D-Gen] Device : {args.device}")
    print(f"[Stage 3D-Gen] Backend: Hunyuan3D-2.1")

    image = _load_image(str(img_path))
    glb_path = str(out_dir / f"{img_path.stem}_ai.glb")

    run_hunyuan3d(image, str(img_path), glb_path, with_texture=args.texture)

    print(f"\n[Stage 3D-Gen] Done!")
    print(f"  GLB (AI-generated) -> {glb_path}")
    return glb_path


if __name__ == "__main__":
    main()
