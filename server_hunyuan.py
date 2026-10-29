"""
Hunyuan3D-2.1 API Server — port 8002
Matches the existing editor.html contract:

  POST /generate          multipart: files=<image(s)>  →  {"job_id": "..."}
  GET  /stream/{job_id}   SSE progress:  {stage, pct, done?, error?}
  GET  /download/{job_id} →  GLB binary
  GET  /health            →  {"status":"ok"}

Run:
  uvicorn server_hunyuan:app --host 0.0.0.0 --port 8002
"""

import asyncio
import io
import json
import os
import shutil
import sys
import tempfile
import threading

# Windows: os.symlink requires Developer Mode or admin; patch it to copy instead.
# HuggingFace hub passes src as a path RELATIVE TO dst's parent (not to CWD),
# so we must resolve it before checking existence.
_orig_symlink = os.symlink
def _safe_symlink(src, dst, **kw):
    try:
        _orig_symlink(src, dst, **kw)
    except OSError:
        abs_src = src if os.path.isabs(src) else os.path.normpath(
            os.path.join(os.path.dirname(os.path.abspath(dst)), src)
        )
        os.makedirs(os.path.dirname(os.path.abspath(dst)), exist_ok=True)
        if os.path.isfile(abs_src):
            shutil.copy2(abs_src, dst)
        elif os.path.isdir(abs_src):
            if os.path.exists(dst):
                shutil.rmtree(dst)
            shutil.copytree(abs_src, dst)
os.symlink = _safe_symlink
import uuid
from pathlib import Path

import torch
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
from PIL import Image
from sse_starlette.sse import EventSourceResponse

# ── Hunyuan3D-2.1 working copy (local weights, no HuggingFace download) ──────
HUNYUAN_DIR = Path(__file__).parent / "HunyuanHunyuan3D-2.1.git" / "HunyuanHunyuan3D-2.1.git" / "Hunyuan3D-2.1"
HUNYUAN_WEIGHTS = HUNYUAN_DIR / "weights"

os.environ["HY3DGEN_MODELS"] = str(HUNYUAN_DIR)

sys.path.insert(0, str(HUNYUAN_DIR))
sys.path.insert(0, str(HUNYUAN_DIR / "hy3dshape"))
sys.path.insert(0, str(HUNYUAN_DIR / "hy3dpaint"))
sys.path.insert(0, str(HUNYUAN_DIR / "hy3dpaint" / "hunyuanpaintpbr" / "unet"))
sys.path.insert(0, str(HUNYUAN_DIR / "hy3dpaint" / "custom_rasterizer"))

# Apply torchvision compatibility patch shipped with the repo
try:
    from torchvision_fix import apply_fix
    apply_fix()
except Exception:
    pass

from hy3dshape.rembg import BackgroundRemover
from hy3dshape.pipelines import Hunyuan3DDiTFlowMatchingPipeline
from textureGenPipeline import Hunyuan3DPaintConfig, Hunyuan3DPaintPipeline

app = FastAPI(title="PlanLift 3D Generator", version="2.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── In-memory job store ───────────────────────────────────────────────────────
_jobs: dict[str, dict] = {}
_jobs_lock = threading.Lock()

# ── Dual-GPU: shape on cuda:1, texture on cuda:0 ─────────────────────────────
# The custom rasterizer is hardcoded to device_id=0, so texture must stay on cuda:0.
# Shape has no such constraint — put it on cuda:1 so both get a full 16 GB.
os.environ.setdefault("PYTORCH_CUDA_ALLOC_CONF", "expandable_segments:True")

_n_gpus     = torch.cuda.device_count() if torch.cuda.is_available() else 0
_device     = "cuda:0" if _n_gpus >= 1 else "cpu"          # texture + rasterizer
_shape_dev  = "cuda:1" if _n_gpus >= 2 else _device        # shape model

_gpu0_name = torch.cuda.get_device_name(0) if _n_gpus >= 1 else "n/a"
_gpu1_name = torch.cuda.get_device_name(1) if _n_gpus >= 2 else "n/a"

print()
print("=" * 60)
print("  PlanLift  |  3D Generation Server")
print("=" * 60)
print(f"  Shape GPU  : {_shape_dev}  —  {_gpu1_name}")
print(f"  Texture GPU: {_device}   —  {_gpu0_name}  (rasterizer pinned to device 0)")
print(f"  Repo       : {HUNYUAN_DIR}")
print("=" * 60)
print()

print(f"[1/3] Loading shape model on {_shape_dev}...")
_shape_pipeline = Hunyuan3DDiTFlowMatchingPipeline.from_pretrained(
    str(HUNYUAN_WEIGHTS),
    subfolder="hunyuan3d-dit-v2-1",
)
_shape_pipeline.to(_shape_dev)
print("[1/3] Shape model ready.")

print(f"[2/3] Initialising PBR texture pipeline on {_device} (resolution=384)...")
_tex_conf = Hunyuan3DPaintConfig(max_num_view=6, resolution=384)
_tex_conf.device                    = _device
_tex_conf.multiview_pretrained_path = str(HUNYUAN_WEIGHTS)
_tex_conf.realesrgan_ckpt_path      = str(HUNYUAN_DIR / "hy3dpaint" / "ckpt" / "RealESRGAN_x4plus.pth")
_tex_conf.multiview_cfg_path        = str(HUNYUAN_DIR / "hy3dpaint" / "cfgs" / "hunyuan-paint-pbr.yaml")
_tex_conf.custom_pipeline           = str(HUNYUAN_DIR / "hy3dpaint" / "hunyuanpaintpbr")
_tex_pipeline = Hunyuan3DPaintPipeline(_tex_conf)
print("[2/3] Texture pipeline ready.")

print("[3/3] Loading background remover...")
_rembg = BackgroundRemover()
print("[3/3] Background remover ready.")

print()
print("  All models loaded. Server starting on port 8002.")
print("=" * 60)
print()


# ── Helpers ───────────────────────────────────────────────────────────────────

import numpy as np

def _remove_floor_mesh(mesh):
    """Remove rug/floor from the generated mesh.

    Strategy 1: split into connected components and discard only clearly floor-like
    parts (flat, wide, bottom-positioned). Furniture arms/legs that are disconnected
    from the main body are preserved as long as they have significant height.
    Strategy 2: fallback — remove any remaining faces with nearly-horizontal normals
    in the bottom 20 % of the mesh height.
    """
    try:
        if not hasattr(mesh, "faces") or len(mesh.faces) == 0:
            return mesh

        # Hunyuan3D outputs Y-up (GLTF convention). Never infer the up axis from
        # argmax(ranges) — for a bed (2 m long, 1 m tall) that picks the Z length
        # axis and then incorrectly removes the foot-end of the bed.
        UP = 1  # Y axis

        # ── Strategy 1: connected components ─────────────────────────────────
        components = mesh.split(only_watertight=False)
        if len(components) > 1:
            import trimesh as _trimesh

            stats = []
            for comp in components:
                b = comp.bounds
                height = float(b[1][UP] - b[0][UP])  # Y extent = true height
                stats.append((height, len(comp.faces), comp))

            max_height = max(s[0] for s in stats)
            max_faces  = max(s[1] for s in stats)

            # Keep components with meaningful height (>15 % of tallest) AND
            # at least 2 % as many faces as the largest component.
            # This preserves sofa arms/legs while discarding a flat floor slab.
            keep = [comp for h, nf, comp in stats
                    if h > max_height * 0.15 and nf > max_faces * 0.02]

            if not keep:
                keep = [max(stats, key=lambda s: s[0] * s[1])[2]]

            mesh = _trimesh.util.concatenate(keep) if len(keep) > 1 else keep[0]

        # ── Strategy 2: remove remaining flat-bottom faces ────────────────────
        if len(mesh.faces) == 0:
            return mesh
        normals = mesh.face_normals
        centers = mesh.triangles_center
        b = mesh.bounds
        up_axis = UP
        min_up  = b[0][up_axis]
        height  = float(b[1][up_axis] - b[0][up_axis])

        flat       = np.abs(normals[:, up_axis]) > 0.55
        low        = centers[:, up_axis] < (min_up + height * 0.20)
        remove     = flat & low
        if remove.sum() > 0:
            mesh.update_faces(~remove)
            mesh.remove_unreferenced_vertices()

    except Exception:
        pass
    return mesh


def _keep_main_object(image: Image.Image) -> Image.Image:
    """After rembg, keep only the main object (tallest + largest connected component).
    Removes flat rugs/floors which are wide but short, preserving tall furniture."""
    if image.mode != "RGBA":
        return image
    try:
        from scipy import ndimage
        alpha   = np.array(image.split()[-1])
        binary  = (alpha > 30).astype(np.uint8)
        labeled, n = ndimage.label(binary)
        if n <= 1:
            return image
        best, best_score = 1, -1
        for i in range(1, n + 1):
            comp   = labeled == i
            size   = int(comp.sum())
            rows   = np.where(comp.any(axis=1))[0]
            if len(rows) == 0:
                continue
            height = int(rows[-1] - rows[0]) + 1
            score  = height * size  # tall × large wins over flat rug
            if score > best_score:
                best_score, best = score, i
        arr = np.array(image, dtype=np.uint8)
        arr[:, :, 3] = np.where(labeled == best, arr[:, :, 3], 0)
        return Image.fromarray(arr, "RGBA")
    except Exception:
        return image


def _remove_floor_from_image(image: Image.Image):
    """Zero out floor/rug pixels in a rembg RGBA image before shape generation.

    Key insight: a floor/rug bottom edge is a single CONTINUOUS horizontal band
    (one long unbroken run of opaque pixels). Furniture legs end in isolated thin
    columns with gaps between them. We detect the floor by checking both width
    AND continuity, then remove everything below the first non-floor row.
    """
    if image.mode != "RGBA":
        return image

    arr   = np.array(image, dtype=np.uint8)
    alpha = arr[:, :, 3]
    h, w  = alpha.shape
    mask  = alpha > 30  # True = opaque

    row_fill = mask.sum(axis=1).astype(float)
    content_rows = np.where(row_fill > 0)[0]
    if len(content_rows) < 10:
        return image

    top    = int(content_rows[0])
    bottom = int(content_rows[-1])

    # Longest continuous horizontal run of opaque pixels per row (vectorised)
    def _max_run(row_mask: np.ndarray) -> int:
        padded  = np.concatenate([[False], row_mask, [False]])
        starts  = np.where(~padded[:-1] & padded[1:])[0]
        ends    = np.where(padded[:-1] & ~padded[1:])[0]
        if len(starts) == 0:
            return 0
        return int((ends - starts).max())

    # A row is "floor-like" if it is both wide AND continuous
    fill_thresh = w * 0.35   # row must be at least 35 % full
    cont_thresh = w * 0.30   # longest run must span at least 30 % of width

    def _is_floor(row: int) -> bool:
        if row_fill[row] < fill_thresh:
            return False
        return _max_run(mask[row]) >= cont_thresh

    # Quick check — if the very bottom row is not floor-like, nothing to remove
    if not _is_floor(bottom):
        return image

    # Scan upward from the bottom to find where the floor region ends
    cut_row = bottom + 1
    search_top = max(top, bottom - (bottom - top) // 2)  # search bottom half only
    for row in range(bottom, search_top - 1, -1):
        if not _is_floor(row):
            cut_row = row + 1
            break

    if cut_row <= bottom:
        arr[cut_row:, 3] = 0
        return Image.fromarray(arr, "RGBA")

    return image


# Real-world heights (metres) matched to the viewer's primitive build() dimensions.
FURNITURE_HEIGHTS: dict[str, float] = {
    "chair":        0.93,
    "sofa":         0.74,
    "bed_single":   0.91,
    "bed_double":   0.97,
    "dining_table": 0.77,
    "table":        0.77,
    "desk":         0.76,
    "bathtub":      0.61,
    "toilet":       0.55,
    "wardrobe":     2.20,
    "bed":          0.91,
}
_DEFAULT_HEIGHT = 0.85  # sensible fallback for unknown types


def _normalize_scale(mesh, furniture_type: str):
    """Scale mesh uniformly so its vertical (Y) extent matches real-world height,
    then translate it so its base sits on Y = 0."""
    try:
        target_h = FURNITURE_HEIGHTS.get(furniture_type, _DEFAULT_HEIGHT)
        b = mesh.bounds                        # [[xmin,ymin,zmin],[xmax,ymax,zmax]]
        current_h = float(b[1][1] - b[0][1])  # Y is up in GLTF/trimesh
        if current_h < 0.001:
            return mesh
        mesh.apply_scale(target_h / current_h)
        # ground the mesh — translate so lowest Y vertex is at 0
        mesh.apply_translation([0.0, -float(mesh.bounds[0][1]), 0.0])
        # clip any geometry that ended up below the floor (pymeshfix / floor
        # remnants can create faces that dip below Y=0 after grounding)
        verts = mesh.vertices
        faces = mesh.faces
        face_max_y = verts[faces, 1].max(axis=1)
        good = face_max_y > -0.01
        if not good.all():
            mesh.update_faces(good)
            mesh.remove_unreferenced_vertices()
    except Exception as e:
        print(f"[WARN] Scale normalisation skipped: {e}")
    return mesh


def _set_job(job_id: str, **kwargs):
    with _jobs_lock:
        if job_id in _jobs:
            _jobs[job_id].update(kwargs)


def _run_generation(job_id: str, images_bytes: list[bytes], furniture_type: str = "chair"):
    try:
        _set_job(job_id, stage="Removing background…", pct=5)

        processed = []
        for raw in images_bytes:
            img = Image.open(io.BytesIO(raw)).convert("RGBA")
            img = _rembg(img)
            img = _remove_floor_from_image(img)
            img = _keep_main_object(img)
            processed.append(img)

        # Pass all uploaded views to the shape model — multi-view input
        # significantly improves reconstruction of large/complex furniture.
        shape_input = processed if len(processed) > 1 else processed[0]

        _set_job(job_id, stage="Generating 3D shape…", pct=15)
        mesh = _shape_pipeline(
            image=shape_input,
            num_inference_steps=50,
            octree_resolution=768,
            num_chunks=40000,
        )[0]

        _set_job(job_id, stage="Removing floor from mesh…", pct=65)
        mesh = _remove_floor_mesh(mesh)

        _set_job(job_id, stage="Repairing mesh…", pct=68)
        try:
            import trimesh.repair as _repair
            _repair.fix_normals(mesh)

            # Only run pymeshfix when there are actual holes — and cap it to
            # meshes whose largest boundary loop is small (< 200 edges).
            # Large-hole filling creates flat triangle fans that punch through
            # the floor and cause the spike artifacts seen on beds/sofas.
            if not mesh.is_watertight:
                boundaries = mesh.outline()
                max_loop = max((len(e) for e in boundaries.entities), default=0) if boundaries else 0
                if 0 < max_loop < 200:
                    import trimesh as _trimesh
                    import pymeshfix as _pmf
                    mf = _pmf.MeshFix(mesh.vertices, mesh.faces)
                    mf.repair(verbose=False)
                    mesh = _trimesh.Trimesh(vertices=mf.v, faces=mf.f, process=False)
                    _repair.fix_normals(mesh)
        except Exception as _he:
            print(f"[WARN] Mesh repair skipped: {_he}")

        _set_job(job_id, stage="Scaling to real-world size…", pct=71)
        mesh = _normalize_scale(mesh, furniture_type)

        _set_job(job_id, stage="Exporting untextured mesh…", pct=72)
        tmp_mesh = tempfile.NamedTemporaryFile(suffix=".glb", delete=False)
        tmp_mesh.close()
        mesh.export(tmp_mesh.name)

        _set_job(job_id, stage="Saving reference image…", pct=74)
        tmp_img = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
        tmp_img.close()
        processed[0].save(tmp_img.name)

        _set_job(job_id, stage="Applying PBR texture…", pct=76)
        torch.cuda.empty_cache()   # free shape-pass fragments before texture pass
        try:
            tmp_obj = tempfile.NamedTemporaryFile(suffix=".obj", delete=False)
            tmp_obj.close()
            _tex_pipeline(
                mesh_path=tmp_mesh.name,
                image_path=tmp_img.name,
                output_mesh_path=tmp_obj.name,
            )
            glb_bytes = Path(tmp_obj.name.replace(".obj", ".glb")).read_bytes()
        except Exception as tex_exc:
            print(f"[WARN] Texture pass failed: {tex_exc}")
            print("[WARN] Serving untextured mesh instead.")
            glb_bytes = Path(tmp_mesh.name).read_bytes()
        finally:
            import time; time.sleep(0.5)  # let pipeline release file handles
            for f in [tmp_mesh.name, tmp_img.name,
                      getattr(tmp_obj, "name", None),
                      tmp_obj.name.replace(".obj", ".glb") if hasattr(tmp_obj, "name") else None]:
                if f:
                    try: Path(f).unlink(missing_ok=True)
                    except OSError: pass

        _set_job(job_id, stage="Done", pct=100, done=True, glb_bytes=glb_bytes)

    except Exception as exc:
        _set_job(job_id, error=str(exc), done=True)
    finally:
        torch.cuda.empty_cache()


# ── Endpoints ─────────────────────────────────────────────────────────────────

@app.get("/health")
def health():
    return {"status": "ok", "backend": "hunyuan3d-2.1", "device": _device}


@app.post("/generate")
async def generate(
    files: list[UploadFile] = File(...),
    furniture_type: str = Form("chair"),
):
    if not files:
        raise HTTPException(status_code=400, detail="No files uploaded")

    images_bytes = [await f.read() for f in files]
    job_id = str(uuid.uuid4())

    with _jobs_lock:
        _jobs[job_id] = {
            "stage": "Queued…", "pct": 0,
            "done": False, "error": None, "glb_bytes": None,
        }

    threading.Thread(
        target=_run_generation, args=(job_id, images_bytes, furniture_type), daemon=True
    ).start()

    return {"job_id": job_id}


@app.get("/stream/{job_id}")
async def stream(job_id: str):
    async def event_generator():
        while True:
            with _jobs_lock:
                job = dict(_jobs.get(job_id, {}))

            if not job:
                yield {"data": json.dumps({"error": "Job not found"})}
                break

            payload = {"stage": job["stage"], "pct": job["pct"]}

            if job.get("error"):
                payload["error"] = job["error"]
                yield {"data": json.dumps(payload)}
                break

            if job.get("done"):
                payload["done"] = True
                yield {"data": json.dumps(payload)}
                break

            yield {"data": json.dumps(payload)}
            await asyncio.sleep(0.5)

    return EventSourceResponse(event_generator())


@app.get("/download/{job_id}")
async def download(job_id: str):
    with _jobs_lock:
        job = dict(_jobs.get(job_id, {}))

    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if not job.get("done"):
        raise HTTPException(status_code=425, detail="Job not ready yet")
    if job.get("error"):
        raise HTTPException(status_code=500, detail=job["error"])

    data = job.get("glb_bytes")
    if not data:
        raise HTTPException(status_code=404, detail="Output file missing")

    return Response(
        content=data,
        media_type="model/gltf-binary",
        headers={"Content-Disposition": "attachment; filename=generated.glb"},
    )
