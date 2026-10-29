"""
PlanLift -- Mask R-CNN API Server  (port 8001)

Wraps the FloorPlanTo3D-API Mask R-CNN model and adds:
  GET  /               -> full FloorPlanTo3D web UI (HTML)
  POST /predict        -> native FloorPlanTo3D JSON (used by the built-in UI)
  POST /analyze        -> PlanLift-compatible JSON  (used by the Next.js editor)
  GET  /health         -> {"status":"ok","model_loaded":bool}
  GET  /textures/search?q=&limit=
  POST /textures/download
  GET  /assets/search?term=&count=
  POST /assets/download

Run:
  cd D:\\PlanLift\\PlanLift
  venv\\Scripts\\python.exe api_server_mrcnn.py

Then open http://localhost:8001 for the full FloorPlanTo3D editor,
or point the PlanLift Next.js app at http://localhost:8001/analyze.
"""

import base64 as _b64
import json
import math
import os
import re
import socket
import subprocess
import sys
import threading
import time
import uuid
import logging
from datetime import datetime
from pathlib import Path

import cv2
import numpy as np
import PIL.Image
import requests
import skimage.color
from numpy import expand_dims

# ── Locate the FloorPlanTo3D-API repo ────────────────────────────────────────
# Default: sibling of the PlanLift folder.  Override with FLOORPLAN3D_ROOT env.
_default_root = Path(__file__).parent.parent / "Real_Estate" / "Real_Estate" / "FloorPlanTo3D-API"
MRCNN_ROOT    = Path(os.environ.get("FLOORPLAN3D_ROOT", str(_default_root)))

if not MRCNN_ROOT.exists():
    raise RuntimeError(
        f"FloorPlanTo3D-API not found at {MRCNN_ROOT}.\n"
        "Set the FLOORPLAN3D_ROOT environment variable to its location."
    )

# Put the FloorPlanTo3D repo on sys.path so mrcnn / polyhaven / sketchfab import
sys.path.insert(0, str(MRCNN_ROOT))

from mrcnn.config import Config          # noqa: E402
from mrcnn.model  import MaskRCNN, mold_image  # noqa: E402
import tensorflow as tf                  # noqa: E402
import polyhaven as _ph                  # noqa: E402
import sketchfab as _sf                  # noqa: E402

from flask import Flask, request, jsonify, send_from_directory, render_template, Response
from flask_cors import CORS

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

WEIGHTS_FILE = MRCNN_ROOT / "weights" / "maskrcnn_15_epochs.h5"
STORAGE_PATH = MRCNN_ROOT / "storage"
TEMPLATES    = MRCNN_ROOT / "templates"

# Saved editor scenes live under the PlanLift repo (NOT the FloorPlanTo3D repo),
# so a project saved on one machine can be dropped into output/projects/ on
# another and picked up by the editor's "Open" dialog.
PROJECTS_DIR = Path(__file__).parent / "output" / "projects"

app = Flask(__name__, template_folder=str(TEMPLATES))
CORS(app, resources={r"/*": {"origins": "*"}})

# ── Model globals ─────────────────────────────────────────────────────────────
_model:        object = None
_graph:        object = None
cfg:           object = None
_model_loaded: bool   = False


class PredictionConfig(Config):
    NAME                     = "floorPlan_cfg"
    NUM_CLASSES              = 1 + 3           # background + wall + window + door
    GPU_COUNT                = 1
    IMAGES_PER_GPU           = 1
    DETECTION_MIN_CONFIDENCE = 0.65
    DETECTION_MAX_INSTANCES  = 200


def load_model() -> None:
    global cfg, _model, _graph, _model_loaded
    if not WEIGHTS_FILE.exists():
        raise FileNotFoundError(
            f"Mask R-CNN weights not found at {WEIGHTS_FILE}.\n"
            "Download 'maskrcnn_15_epochs.h5' from the link in the "
            "FloorPlanTo3D-API README and place it in its weights/ folder."
        )
    cfg    = PredictionConfig()
    _model = MaskRCNN(mode="inference",
                      model_dir=str(MRCNN_ROOT / "mrcnn"),
                      config=cfg)
    _model.keras_model.load_weights(str(WEIGHTS_FILE), by_name=True)
    try:
        _graph = tf.get_default_graph()
    except AttributeError:
        _graph = None
    _model_loaded = True
    logger.info("Mask R-CNN model loaded successfully.")


@app.before_request
def ensure_model() -> None:
    if not _model_loaded and request.endpoint not in (
        "health", "serve_storage", "sketch_to_floorplan", "predict_rv",
        "list_projects", "get_project", "save_project", "delete_project",
    ):
        try:
            load_model()
        except FileNotFoundError as exc:
            logger.warning(str(exc))


# ── Image preprocessing ───────────────────────────────────────────────────────

def _enhance(image: np.ndarray) -> np.ndarray:
    """CLAHE + mild unsharp mask to improve thin wall visibility."""
    bgr       = cv2.cvtColor(image, cv2.COLOR_RGB2BGR)
    lab       = cv2.cvtColor(bgr, cv2.COLOR_BGR2LAB)
    l, a, b   = cv2.split(lab)
    clahe     = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
    enhanced  = cv2.merge([clahe.apply(l), a, b])
    enhanced  = cv2.cvtColor(enhanced, cv2.COLOR_LAB2BGR)
    blur      = cv2.GaussianBlur(enhanced, (0, 0), sigmaX=1.5)
    sharpened = cv2.addWeighted(enhanced, 1.4, blur, -0.4, 0)
    return cv2.cvtColor(sharpened, cv2.COLOR_BGR2RGB)


CLASS_MAP = {1: "wall", 2: "window", 3: "door"}


def _infer(stream):
    """Load image from file stream, preprocess, run Mask R-CNN, return (r, w, h)."""
    img   = PIL.Image.open(stream)
    image = np.asarray(img)
    if image.ndim == 2:
        image = skimage.color.gray2rgb(image)
    if image.shape[-1] == 4:
        image = image[..., :3]
    h, w = image.shape[:2]

    image        = _enhance(image)
    scaled_image = mold_image(image, cfg)
    sample       = expand_dims(scaled_image, 0)

    if _graph is not None:
        with _graph.as_default():
            r = _model.detect(sample, verbose=0)[0]
    else:
        r = _model.detect(sample, verbose=0)[0]

    return r, w, h


# ── BBox -> PlanLift format conversion ─────────────────────────────────────────

def _seg_dist(px, py, x1, y1, x2, y2):
    """Distance from point to segment + t parameter in [0,1]."""
    dx, dy = x2 - x1, y2 - y1
    lsq = dx * dx + dy * dy
    if lsq < 1e-6:
        return math.hypot(px - x1, py - y1), 0.0
    t = max(0.0, min(1.0, ((px - x1) * dx + (py - y1) * dy) / lsq))
    return math.hypot(px - (x1 + t * dx), py - (y1 + t * dy)), t


def _bboxes_to_planlift(rois, class_ids):
    """
    Convert Mask R-CNN bounding boxes to PlanLift walls / doors / windows.

    Mask R-CNN rois are (y1, x1, y2, x2) in pixel coordinates.
    Wall bboxes become centreline segments + thickness (same convention as
    PlanLift's walls_from_npz).  Openings are projected onto the nearest wall.
    """
    walls, doors, windows = [], [], []
    w_idx = d_idx = win_idx = 0

    raw_walls    = []   # wall dicts ready for the response
    raw_openings = []   # (cx, cy, bbox_w, bbox_h, class_name)

    for roi, cid in zip(rois, class_ids):
        y1, x1, y2, x2 = float(roi[0]), float(roi[1]), float(roi[2]), float(roi[3])
        bw   = x2 - x1
        bh   = y2 - y1
        name = CLASS_MAP.get(int(cid), "unknown")

        if name == "wall":
            if bw >= bh:                         # horizontal wall
                mid_y = (y1 + y2) / 2
                entry = {
                    "id":        f"mw{w_idx}",
                    "start":     {"x": round(x1, 1),    "y": round(mid_y, 1)},
                    "end":       {"x": round(x2, 1),    "y": round(mid_y, 1)},
                    "thickness": round(max(bh, 6.0), 1),
                    "height":    270.0,
                    "type":      "wall",
                }
            else:                                # vertical wall
                mid_x = (x1 + x2) / 2
                entry = {
                    "id":        f"mw{w_idx}",
                    "start":     {"x": round(mid_x, 1), "y": round(y1, 1)},
                    "end":       {"x": round(mid_x, 1), "y": round(y2, 1)},
                    "thickness": round(max(bw, 6.0), 1),
                    "height":    270.0,
                    "type":      "wall",
                }
            raw_walls.append(entry)
            w_idx += 1
        else:
            raw_openings.append(((x1 + x2) / 2, (y1 + y2) / 2, bw, bh, name))

    # Project each opening onto its nearest wall
    for cx, cy, bw, bh, name in raw_openings:
        best_wall, best_t, best_d = None, 0.5, float("inf")
        for wall in raw_walls:
            sx, sy = wall["start"]["x"], wall["start"]["y"]
            ex, ey = wall["end"]["x"],   wall["end"]["y"]
            d, t   = _seg_dist(cx, cy, sx, sy, ex, ey)
            if d < best_d:
                best_d, best_t, best_wall = d, t, wall
        if best_wall is None:
            continue

        pos   = round(max(0.05, min(0.95, best_t)), 3)
        width = round(max(bw, bh, 30.0), 1)

        if name == "door":
            doors.append({
                "id":             f"pd{d_idx}",
                "wallId":         best_wall["id"],
                "positionOnWall": pos,
                "width":          width,
                "height":         210.0,
                "type":           "door",
            })
            d_idx += 1
        else:
            windows.append({
                "id":             f"pw{win_idx}",
                "wallId":         best_wall["id"],
                "positionOnWall": pos,
                "width":          width,
                "height":         120.0,
                "sillHeight":     90.0,
                "type":           "window",
            })
            win_idx += 1

    return raw_walls, doors, windows


# ── Routes ────────────────────────────────────────────────────────────────────

@app.route("/storage/<path:filename>")
def serve_storage(filename: str):
    """Serve cached textures and 3D assets."""
    return send_from_directory(str(STORAGE_PATH), filename)


@app.route("/", methods=["GET"])
def index():
    """Serve the full FloorPlanTo3D web UI."""
    return render_template("index.html")


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "model_loaded": _model_loaded})


@app.route("/predict", methods=["POST"])
@app.route("/", methods=["POST"])
def predict():
    """
    Native FloorPlanTo3D format -- consumed by the built-in HTML UI.
    Accepts field name: 'image'.
    """
    if not _model_loaded:
        return jsonify({"error": "Model not loaded. Check weights file."}), 503
    if "image" not in request.files:
        return jsonify({"error": "No image field in request"}), 400

    try:
        r, w, h = _infer(request.files["image"].stream)
    except Exception as exc:
        logger.exception("Inference error")
        return jsonify({"error": str(exc)}), 500

    rois      = r["rois"].tolist()
    class_ids = r["class_ids"]
    door_dims = [
        max(abs(roi[2] - roi[0]), abs(roi[3] - roi[1]))
        for roi, cid in zip(rois, class_ids) if int(cid) == 3
    ]
    avg_door = float(np.mean(door_dims)) if door_dims else 0.0

    return jsonify({
        "points":      [{"x1": roi[1], "y1": roi[0], "x2": roi[3], "y2": roi[2]}
                        for roi in rois],
        "classes":     [{"name": CLASS_MAP.get(int(cid), "unknown")} for cid in class_ids],
        "Width":       w,
        "Height":      h,
        "averageDoor": avg_door,
    })


@app.route("/predict-rv", methods=["POST"])
def predict_rv():
    """
    Raster-to-Vector prediction -- junction detection + integer programming.
    Same input/output format as /predict, drop-in replacement.
    """
    if "image" not in request.files:
        return jsonify({"error": "No image field in request"}), 400
    try:
        from predict_rv import predict_image
        image_bytes = request.files["image"].read()
        result = predict_image(image_bytes)
        return jsonify(result)
    except Exception as exc:
        logger.exception("RV prediction error")
        return jsonify({"error": str(exc)}), 500


@app.route("/analyze", methods=["POST"])
def analyze():
    """
    PlanLift-compatible format -- consumed by the Next.js editor.
    Accepts field name: 'file' (same as PlanLift's existing /analyze endpoint).
    Returns {walls, doors, windows, rooms}.
    """
    if not _model_loaded:
        return jsonify({"error": "Model not loaded. Check weights file."}), 503

    field = "file" if "file" in request.files else "image"
    if field not in request.files:
        return jsonify({"error": "No image file in request (field: 'file' or 'image')"}), 400

    try:
        r, w, h = _infer(request.files[field].stream)
    except Exception as exc:
        logger.exception("Inference error")
        return jsonify({"error": str(exc)}), 500

    walls, doors, windows = _bboxes_to_planlift(r["rois"], r["class_ids"])
    return jsonify({"walls": walls, "doors": doors, "windows": windows, "rooms": []})


# ── Project save / load routes ───────────────────────────────────────────────
# The editor (editor.html) serialises its whole scene — floor plans as data
# URLs, walls, corners, rooms, furniture placements, per-floor zoom/pan — into
# one JSON blob and POSTs it here. Files are named "<projectId>.json" and stored
# flat in PROJECTS_DIR.

_PROJECT_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

# data-URL thumbnail -> (extension, mimetype). Kept tiny on purpose.
_THUMB_MIME_EXT = {
    "image/webp": "webp",
    "image/jpeg": "jpg",
    "image/png":  "png",
}


def _safe_project_id(pid: str) -> str:
    """Reject anything that isn't a plain id so the path can't escape the dir."""
    pid = (pid or "").strip()
    if not _PROJECT_ID_RE.match(pid):
        raise ValueError("invalid projectId")
    return pid


def _thumb_path(pid: str) -> Path:
    return PROJECTS_DIR / f"{pid}.thumb"


def _write_thumb(pid: str, data_url: str) -> str | None:
    """Decode a `data:image/...;base64,...` thumbnail and store it next to the
    project JSON. Returns the stored mimetype, or None if nothing was written."""
    if not isinstance(data_url, str) or not data_url.startswith("data:image/"):
        return None
    try:
        header, b64 = data_url.split(",", 1)
        mime = header[5:].split(";", 1)[0].strip().lower()
        if mime not in _THUMB_MIME_EXT:
            return None
        raw = _b64.b64decode(b64, validate=False)
        if not raw or len(raw) > 4_000_000:      # ~4 MB ceiling, thumbs are ~30-120 KB
            return None
        _thumb_path(pid).write_bytes(raw)
        return mime
    except Exception:
        logger.exception("Failed to store thumbnail for %s", pid)
        return None


@app.route("/projects", methods=["GET"])
def list_projects():
    """List saved projects, newest first, with enough for a visual library:
    [{projectId, name, savedAt, createdAt, hasThumb, elementCount, floorCount}]."""
    PROJECTS_DIR.mkdir(parents=True, exist_ok=True)
    out = []
    for fp in PROJECTS_DIR.glob("*.json"):
        try:
            d = json.loads(fp.read_text(encoding="utf-8"))
        except Exception:
            logger.warning("Skipping unreadable project file: %s", fp.name)
            continue
        pid = d.get("projectId", fp.stem)
        state = d.get("state") or {}
        els = state.get("elements") or []
        fps = state.get("floorPlans") or {}
        out.append({
            "projectId":    pid,
            "name":         d.get("name", fp.stem),
            "savedAt":      d.get("savedAt", ""),
            "createdAt":    d.get("createdAt", d.get("savedAt", "")),
            "hasThumb":     _thumb_path(pid).exists(),
            "elementCount": len(els) if isinstance(els, list) else 0,
            "floorCount":   len(fps) if isinstance(fps, dict) else 0,
        })
    out.sort(key=lambda r: r.get("savedAt", ""), reverse=True)
    return jsonify({"projects": out})


@app.route("/projects/<project_id>/thumb", methods=["GET"])
def get_project_thumb(project_id):
    """Serve the stored thumbnail bytes for a project (404 when there is none)."""
    try:
        pid = _safe_project_id(project_id)
    except ValueError:
        return jsonify({"error": "invalid projectId"}), 400
    tp = _thumb_path(pid)
    if not tp.exists():
        return jsonify({"error": "no thumbnail"}), 404
    mime = "image/webp"
    fp = PROJECTS_DIR / f"{pid}.json"
    if fp.exists():
        try:
            mime = json.loads(fp.read_text(encoding="utf-8")).get("thumbMime") or mime
        except Exception:
            pass
    return Response(tp.read_bytes(), mimetype=mime,
                    headers={"Cache-Control": "no-cache"})


@app.route("/projects/<project_id>/rename", methods=["POST"])
def rename_project(project_id):
    """Body: {name}. Renames the project in place (id and thumbnail unchanged)."""
    try:
        pid = _safe_project_id(project_id)
    except ValueError:
        return jsonify({"error": "invalid projectId"}), 400
    fp = PROJECTS_DIR / f"{pid}.json"
    if not fp.exists():
        return jsonify({"error": "project not found"}), 404
    data = request.get_json(silent=True) or {}
    name = (str(data.get("name") or "").strip() or "Untitled")[:120]
    try:
        rec = json.loads(fp.read_text(encoding="utf-8"))
    except Exception as exc:
        return jsonify({"error": f"corrupt project file: {exc}"}), 500
    rec["name"] = name
    rec["savedAt"] = datetime.now().isoformat(timespec="seconds")
    tmp = PROJECTS_DIR / f".{pid}.json.tmp"
    tmp.write_text(json.dumps(rec, ensure_ascii=False), encoding="utf-8")
    tmp.replace(fp)
    return jsonify({"projectId": pid, "name": name, "savedAt": rec["savedAt"]})


@app.route("/projects/<project_id>", methods=["GET"])
def get_project(project_id):
    """Return the full saved record {projectId, name, savedAt, state}."""
    try:
        pid = _safe_project_id(project_id)
    except ValueError:
        return jsonify({"error": "invalid projectId"}), 400
    fp = PROJECTS_DIR / f"{pid}.json"
    if not fp.exists():
        return jsonify({"error": "project not found"}), 404
    try:
        return jsonify(json.loads(fp.read_text(encoding="utf-8")))
    except Exception as exc:
        logger.exception("Failed to read project %s", pid)
        return jsonify({"error": f"corrupt project file: {exc}"}), 500


@app.route("/projects", methods=["POST"])
def save_project():
    """
    Save a project.
      - projectId given  -> update that file in place (keeps its createdAt).
      - projectId omitted -> mint a fresh uuid, i.e. a new "Save as copy".
    Body: {projectId?, name?, state:{...}, thumbnail?}. `thumbnail` is a
    data:image/(webp|jpeg|png);base64 URL and is stored alongside the JSON.
    Returns {projectId, name, savedAt, createdAt}.
    """
    data = request.get_json(silent=True) or {}
    state = data.get("state")
    if not isinstance(state, dict):
        return jsonify({"error": "body must contain a 'state' object"}), 400

    given_id = data.get("projectId")
    raw_id = given_id or uuid.uuid4().hex
    try:
        pid = _safe_project_id(raw_id)
    except ValueError:
        return jsonify({"error": "invalid projectId"}), 400

    PROJECTS_DIR.mkdir(parents=True, exist_ok=True)
    dst = PROJECTS_DIR / f"{pid}.json"
    now = datetime.now().isoformat(timespec="seconds")

    created_at = now
    if given_id and dst.exists():
        try:
            prev = json.loads(dst.read_text(encoding="utf-8"))
            created_at = prev.get("createdAt", prev.get("savedAt", now))
        except Exception:
            pass

    name = (str(data.get("name") or "").strip() or "Untitled")[:120]

    thumb_mime = _write_thumb(pid, data.get("thumbnail"))

    record = {
        "projectId": pid,
        "name":      name,
        "createdAt": created_at,
        "savedAt":   now,
        "state":     state,
    }
    if thumb_mime:
        record["thumbMime"] = thumb_mime
    elif dst.exists():
        try:
            record["thumbMime"] = json.loads(dst.read_text(encoding="utf-8")).get("thumbMime")
        except Exception:
            pass

    tmp = PROJECTS_DIR / f".{pid}.json.tmp"
    tmp.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")
    tmp.replace(dst)   # atomic on the same volume — no half-written scene files
    logger.info("Saved project %s (%s)%s", pid, name, " +thumb" if thumb_mime else "")
    return jsonify({"projectId": pid, "name": name,
                    "savedAt": now, "createdAt": created_at})


@app.route("/projects/<project_id>", methods=["DELETE"])
def delete_project(project_id):
    try:
        pid = _safe_project_id(project_id)
    except ValueError:
        return jsonify({"error": "invalid projectId"}), 400
    fp = PROJECTS_DIR / f"{pid}.json"
    existed = fp.exists()
    if existed:
        fp.unlink()
    tp = _thumb_path(pid)
    if tp.exists():
        try:
            tp.unlink()
        except Exception:
            pass
    return jsonify({"ok": True, "deleted": existed})


# ── PolyHaven routes (proxied from FloorPlanTo3D-API) ─────────────────────────

@app.route("/textures/search", methods=["GET"])
def textures_search():
    q     = request.args.get("q", "")
    limit = int(request.args.get("limit", 20))
    res   = _ph.search(query=q, limit=limit)
    return jsonify({"results": res, "count": len(res)})


@app.route("/textures/download", methods=["POST"])
def textures_download():
    data       = request.get_json(silent=True) or {}
    name       = data.get("name", "").strip()
    resolution = data.get("resolution", _ph.RESOLUTION)
    maps       = _ph.download(name, STORAGE_PATH / "textures", resolution=resolution)
    if maps is None:
        return jsonify({"error": f"Failed to download texture '{name}'"}), 502
    return jsonify({"name": name, "resolution": resolution, "maps": maps})


# ── Sketchfab routes ──────────────────────────────────────────────────────────

@app.route("/assets/search", methods=["GET"])
def assets_search():
    term         = request.args.get("term", "").strip()
    count        = int(request.args.get("count", 6))
    downloadable = request.args.get("downloadable", "").lower() in ("1", "true", "yes")
    res          = _sf.search(term=term, count=count, downloadable=downloadable)
    return jsonify({"results": res, "count": len(res)})


@app.route("/assets/download", methods=["POST"])
def assets_download():
    data = request.get_json(silent=True) or {}
    uid  = data.get("uid", "").strip()
    name = data.get("name", uid).strip().replace(" ", "_")
    path = _sf.download(uid=uid, cache_dir=STORAGE_PATH / "assets" / name)
    if path is None:
        if not _sf.TOKEN:
            return jsonify({"error": "SKETCHFAB_TOKEN env var not set"}), 401
        return jsonify({"error": f"Failed to download asset uid={uid}"}), 502
    return jsonify({"uid": uid, "name": name, "path": path})


# ── Sketch-to-Floor Plan (Claude Vision) ─────────────────────────────────────

import base64 as _b64

@app.route("/sketch-to-floorplan", methods=["POST"])
def sketch_to_floorplan():
    """
    POST /sketch-to-floorplan
    Multipart body: 'file' -- the hand-drawn sketch image.
    Returns JSON:
      { "parsed": {...},
        "rendered_b64": "data:image/png;base64,...",   (preview with labels)
        "cleaned_b64":  "data:image/png;base64,..." }  (walls-only for MRCNN)
    """
    try:
        from sketch_to_floorplan import clean_sketch, render_preview
    except ImportError as e:
        return jsonify({"error": f"sketch_to_floorplan module not found: {e}"}), 500

    if "file" not in request.files:
        return jsonify({"error": "No file field in request"}), 400

    image_bytes = request.files["file"].read()
    if not image_bytes:
        return jsonify({"error": "Empty file"}), 400

    try:
        cleaned_png = clean_sketch(image_bytes)
    except Exception as e:
        return jsonify({"error": f"Image cleaning error: {e}"}), 500

    try:
        preview_png, enhanced_png, features = render_preview(image_bytes, cleaned_png)
    except Exception as e:
        features = {"rooms": [], "doors": [], "windows": []}
        enhanced_png = cleaned_png
        preview_png  = cleaned_png

    # cleaned_b64 = enhanced image WITH door/window symbols (for Mask R-CNN)
    cleaned_b64  = f"data:image/png;base64,{_b64.b64encode(enhanced_png).decode()}"
    rendered_b64 = f"data:image/png;base64,{_b64.b64encode(preview_png).decode()}"

    return jsonify({
        "parsed":       features,
        "rendered_b64": rendered_b64,
        "cleaned_b64":  cleaned_b64,
    })


# ── AI Furnish (furnishing-plan generation — Claude when a key is
# configured, a local Ollama model otherwise) ────────────────────────────
# Auto-detected, not a flag to remember: if ANTHROPIC_API_KEY is set (env
# var, or the same .env file SKETCHFAB_TOKEN already reads via
# _read_env_var_mrcnn), every request uses Claude — meaningfully better at
# the actual reasoning this task needs (picking a sensible, non-redundant
# furniture list; correctly choosing FLANK vs FACING arrangements) than a
# 7B/14B local model. With no key, nothing changes from before — Ollama
# stays the free/local path, same as it's always been. Either way the
# downstream contract (POST room+prompt, get back
# {"items":[{item,qty,zone,group,role}]}) is identical, so nothing else in
# this file or in editor.html needs to know which provider actually ran.
OLLAMA_BASE_URL = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434")
OLLAMA_MODEL    = os.environ.get("OLLAMA_MODEL", "qwen2.5:14b-instruct")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "") or _read_env_var_mrcnn("ANTHROPIC_API_KEY")
# claude-opus-5 by default — override with CLAUDE_FURNISH_MODEL if you'd
# rather trade some quality for lower cost on this specific task (it's
# plain structured list-generation, not deep multi-step reasoning, so a
# cheaper model is a genuinely reasonable choice here — just your call to
# make, not one to default into).
CLAUDE_FURNISH_MODEL = os.environ.get("CLAUDE_FURNISH_MODEL", "claude-opus-5")

# Nebius Token Factory (NVIDIA Nemotron) — an OPT-IN third provider, never
# auto-selected. The Claude-then-Ollama chain above is already proven and
# stays the default for every existing request unchanged; this only ever
# runs when a request explicitly asks for it (see the "provider" field on
# /ai-furnish below) — added for real evaluation/demo purposes, not because
# it's been shown to beat what's already here. Base URL and model id
# confirmed directly against Nebius's own docs (docs.tokenfactory.nebius.com)
# at integration time; override NEBIUS_FURNISH_MODEL if Nebius ever renames/
# recases it — their own docs note ids can change, so nothing here should be
# treated as a promise it'll always resolve.
NEBIUS_API_KEY = os.environ.get("NEBIUS_API_KEY", "")
NEBIUS_BASE_URL = os.environ.get("NEBIUS_BASE_URL", "https://api.tokenfactory.nebius.com/v1")
NEBIUS_FURNISH_MODEL = os.environ.get("NEBIUS_FURNISH_MODEL", "nvidia/nemotron-3-super-120b-a12b")

# The placement solver (editor.html, client-side) only understands these five
# zone keywords — anything else the model invents gets coerced to "center"
# below rather than silently breaking the solver on an unrecognized value.
_FURNISH_ZONES = {"along_wall", "near_entry", "corner", "center", "cluster"}
# Same idea for group roles — see the prompt below for what each one means
# to the solver (_solveFurnishPlacement's group-layout functions).
_FURNISH_ROLES = {"anchor", "flank", "facing", "between", "workstation_chair",
                   "media_unit", "media_seat", "surround_center", "surround_seat"}

# Verified directly against the live model before shipping (both the
# flank/facing distinction and each individual pattern) — an earlier,
# looser version of this prompt reused role "flank" for a facing-seating
# request too (a real, reproduced failure: asked for "two sofas facing
# each other", got back an anchor+flank pair instead, which the solver
# would have arranged side-by-side, not across from one another). The
# explicit contrast below ("FLANK... not across from it") is what fixed it.
_FURNISH_SYSTEM_PROMPT = """You are a furnishing planner for an architectural floor-plan tool. Your goal is a room that looks like a real, lived-in space — composed, not scattered — and still leaves room to walk through it.
Output ONLY a JSON object of this exact shape:
{"items": [{"item": "<short lowercase name>", "qty": <integer>, "zone": "<along_wall|near_entry|corner|center|cluster>", "group": "<short id, or null>", "role": "<anchor|flank|facing|between|workstation_chair|media_unit|media_seat|surround_center|surround_seat, or null>"}]}

Most items are standalone: group null, role null, just item/qty/zone as before. But use group+role generously wherever a real arrangement is possible — an ungrouped item is placed independently with no relationship to anything else in the room, which is exactly what makes a plan look scattered instead of composed. There are FIVE different patterns, pick whichever the request actually describes:

1. FLANK — one central piece against a wall with companions beside it, both facing the SAME direction (into the room), not across from each other. Example: a bed with a nightstand on each side. The central piece gets role "anchor", zone "along_wall". The side pieces get role "flank", qty 2, the SAME group id as the anchor.

2. FACING — two matching seats placed ACROSS FROM each other, facing INWARD toward one another (opposite directions), with a table at the midpoint between them. Example: "two sofas facing each other", "armchairs facing each other across a table". BOTH seats get role "facing", qty 2, the SAME group id, zone "cluster". The table between them gets role "between", the SAME group id, zone "cluster".

3. WORKSTATION — a wall-hugging work surface with ONE seat tucked in front of it, facing BACK toward it (not into the room like flank, not toward a matching twin like facing). Example: "a desk and chair", "furnish this as an office" (desk + chair is the default working area for an office/study). The desk (or table/counter being worked at) gets role "anchor", zone "along_wall". The single chair gets role "workstation_chair", the SAME group id, qty 1 — never qty 2 and never "flank" for this chair, since it must face the desk, not sit beside it facing the same way.

4. MEDIA_FACING — a wall-hugging media console with one or more seats facing it from across the room, like a TV. Example: "a sofa facing the tv", "living room with a tv". The console gets role "media_unit", zone "along_wall", item name exactly "tv stand" (not "tv", "television", or "entertainment center" — "tv stand" is what actually resolves to a real catalog model). The seat(s) get role "media_seat", the SAME group id, zone "cluster" — qty 1 for a single sofa, or more (e.g. 2-3 armchairs) if the request wants several seats all facing the same screen.

5. SURROUND — a table with chairs evenly spaced all the way around it, all facing inward toward the table. Example: "a dining table for 6", "furnish this dining room", "a conference table for 8". The table gets role "surround_center", zone "cluster" — item name MUST match the actual room/furniture type, not always "dining table": "dining table" for a dining room, "conference table" for an office/meeting/conference/boardroom. Getting this wrong means searching for entirely the wrong kind of table. The chairs get role "surround_seat", the SAME group id, zone "cluster", qty = however many people the request wants seated (e.g. 6 for "table for 6").

Use FACING whenever the request describes seats looking at / facing / opposite one another (another seat). Use MEDIA_FACING whenever seats face a screen/console instead. Use WORKSTATION whenever ONE seat belongs to and faces a single work surface (desk, counter, reception desk with a chair behind it). Use SURROUND whenever a table is meant to be sat AROUND rather than worked at or looked across a gap at. Use FLANK only for a companion sitting BESIDE a central piece, same facing direction. Do not default a desk+chair pair to independent standalone placement or to flank — a desk and its chair are always a workstation group when both are requested; a dining table and its chairs are always a surround group. You may use several separate groups (different ids) for different arrangements in the same room (e.g. a workstation AND a separate facing-seating set).

Composition guidance for whatever stays standalone (decor, extra seating, anything not in a group above):
- Leave a walking path from the door toward the middle of the room — don't push everything into one half of the room and leave the rest completely empty.
- A rug (if requested) belongs under/near a seating arrangement, not floating on its own in open floor space — it will automatically be sized to the nearest facing/media/surround group, so just include it as a plain standalone item when a room has real seating.
- Small decor (plants, lamps, small pieces) reads as intentional when placed near a wall or near a piece of furniture already in the room, not floating in the middle of open floor space.

Other rules:
- If the request names specific items ("a sofa, table, chair, and plant"), use exactly those items — do not add extras it didn't ask for.
- If the request only names a room type with no specific items ("furnish the reception"), infer a small, sensible set of 4-8 items typical for that room type, using group/role for any real arrangement among them. Use this as a guide (not an exhaustive list — use judgment for a room type not listed here), and DO NOT mix categories across room types — a workspace never gets lounge/waiting-area seating, and vice versa:
  - office / study: desk, chair (workstation group), bookshelf, filing cabinet, lamp, plant. NEVER a sofa, couch, or coffee table — those belong to a reception/lounge/living room, not a workspace.
  - reception / lobby / front desk: reception desk, chair (workstation group), sofa or armchairs for guest seating (facing group with a coffee table between them), plant, decor.
  - bedroom: bed, nightstand(s) (flank group), wardrobe, lamp.
  - living room / lounge: sofa, armchairs, coffee table, rug, plant, and (when the request mentions watching/tv/media, or just as a sensible default for this room type) a tv stand with the sofa in a media_facing group facing it — otherwise a facing group with the seating instead.
  - dining room: dining table with chairs around it (surround group, qty = seats requested or a sensible default like 6), plus a sideboard/cabinet or plant if there's room for more than just the table.
  - conference / meeting / boardroom: conference table with chairs around it (surround group, item name "conference table", qty = seats requested or a sensible default like 8), plus a plant or extra chair along a wall if there's room for more than just the table.
- Keep "item" to 1-3 words, lowercase, singular ("sofa", "coffee table", "plant") - not "sofas" or "Sofa Set".
- Output ONLY the JSON object - no prose, no markdown code fences, no explanation."""


# Appended to _FURNISH_SYSTEM_PROMPT when the request includes a catalog
# summary (editor.html's _aiFurnCatalogSummary — the actual contents of
# the user's My Library, deduplicated down to one entry per real base
# item). Grounds the plan in real inventory instead of the model guessing
# generic names blind, which used to mean retrieval had to reconcile an
# invented name against reality after the fact (a real source of earlier
# bugs: sizing guesses that disagreed with what actually got retrieved,
# generic names that missed a perfectly good local match). Explicitly
# asks for the item's OWN short name, not the full "Category > Name"
# breadcrumb it's shown with — that breadcrumb is context for the model,
# not the value it should echo back; the existing local-match/variant-
# rotation logic already picks well among same-named numbered variants
# ("Armchair 1" vs "Armchair 2"), so this deliberately does not ask the
# model to hand-pick a specific numbered one.
def _build_furnish_catalog_note(catalog_lines):
    if not catalog_lines:
        return ""
    listing = "\n".join(f"- {line}" for line in catalog_lines[:300])
    return f"""

Furniture actually available in this project's local asset library (shown as "Category > ... > Item Name"). When an item the request needs reasonably matches one of these, use that item's OWN short name (the part after the last " > ", not the category path) as your "item" value — it will be placed directly from the local library, faster and more reliable than an online search. If nothing here reasonably fits what's needed, use a short generic descriptive name instead — that gets searched for online.

{listing}"""


class _AiFurnishProviderError(Exception):
    """Wraps a provider-specific failure with the HTTP status /ai-furnish should return."""
    def __init__(self, message, status_code=502):
        super().__init__(message)
        self.status_code = status_code


def _ai_furnish_via_claude(user_msg, system_prompt):
    """Returns (raw_json_text, model_name). Raises _AiFurnishProviderError on failure."""
    try:
        import anthropic
    except ImportError:
        raise _AiFurnishProviderError(
            "ANTHROPIC_API_KEY is set but the 'anthropic' package isn't installed — pip install anthropic", 500)
    try:
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        response = client.messages.create(
            model=CLAUDE_FURNISH_MODEL,
            max_tokens=2000,
            system=system_prompt,
            messages=[{"role": "user", "content": user_msg}],
        )
    except anthropic.APIStatusError as e:
        raise _AiFurnishProviderError(f"Claude request failed: {e.message}", e.status_code)
    except anthropic.APIConnectionError as e:
        raise _AiFurnishProviderError(f"Couldn't reach the Anthropic API: {e}", 502)
    raw = next((b.text for b in response.content if getattr(b, "type", None) == "text"), "")
    return raw, CLAUDE_FURNISH_MODEL


def _ai_furnish_via_ollama(user_msg, system_prompt):
    """Returns (raw_json_text, model_name). Raises _AiFurnishProviderError on failure."""
    try:
        resp = requests.post(
            f"{OLLAMA_BASE_URL}/api/chat",
            json={
                "model": OLLAMA_MODEL,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_msg},
                ],
                "format": "json",
                "stream": False,
                "options": {"temperature": 0.4},
            },
            timeout=60,
        )
        resp.raise_for_status()
    except requests.exceptions.ConnectionError:
        raise _AiFurnishProviderError(f"Can't reach Ollama at {OLLAMA_BASE_URL} — is it running? (ollama serve)", 503)
    except requests.exceptions.Timeout:
        raise _AiFurnishProviderError("Ollama took too long to respond", 504)
    except Exception as e:
        raise _AiFurnishProviderError(f"Ollama request failed: {e}", 502)
    raw = (resp.json().get("message") or {}).get("content", "")
    return raw, OLLAMA_MODEL


def _ai_furnish_via_nebius(user_msg, system_prompt):
    """Returns (raw_json_text, model_name). Raises _AiFurnishProviderError on failure.
    Nebius Token Factory's API is OpenAI-chat-completions-compatible — same
    messages shape as the two providers above, different endpoint/auth."""
    if not NEBIUS_API_KEY:
        raise _AiFurnishProviderError("NEBIUS_API_KEY is not set", 500)
    try:
        resp = requests.post(
            f"{NEBIUS_BASE_URL}/chat/completions",
            headers={"Authorization": f"Bearer {NEBIUS_API_KEY}"},
            json={
                "model": NEBIUS_FURNISH_MODEL,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_msg},
                ],
                "temperature": 0.4,
            },
            timeout=60,
        )
        resp.raise_for_status()
    except requests.exceptions.ConnectionError:
        raise _AiFurnishProviderError(f"Can't reach Nebius Token Factory at {NEBIUS_BASE_URL}", 503)
    except requests.exceptions.Timeout:
        raise _AiFurnishProviderError("Nebius Token Factory took too long to respond", 504)
    except Exception as e:
        raise _AiFurnishProviderError(f"Nebius request failed: {e}", 502)
    data = resp.json()
    raw = ((data.get("choices") or [{}])[0].get("message") or {}).get("content", "")
    return raw, NEBIUS_FURNISH_MODEL


@app.route("/ai-furnish", methods=["POST"])
def ai_furnish():
    """
    POST /ai-furnish
    Body: {"roomName": str, "widthM": float|null, "depthM": float|null, "prompt": str,
           "provider": "nebius"|"ollama"|"claude"|null (optional — forces that
           provider for this one request; omitted/unrecognized falls through to
           the default Nebius/Nemotron-then-Ollama chain)}
    Returns: {"items": [{"item": str, "qty": int, "zone": str}, ...], "model": str}
    """
    data      = request.get_json(silent=True) or {}
    room_name = str(data.get("roomName") or "room").strip()
    width_m   = data.get("widthM")
    depth_m   = data.get("depthM")
    prompt    = str(data.get("prompt") or "").strip()
    requested_provider = str(data.get("provider") or "").strip().lower()
    if not prompt:
        return jsonify({"error": "prompt is required"}), 400

    dims = f"{width_m:.1f}m x {depth_m:.1f}m" if isinstance(width_m, (int, float)) and isinstance(depth_m, (int, float)) else "an unspecified size"
    user_msg = f'Room: "{room_name}" ({dims}). Request: {prompt}'

    # Optional: editor.html's _aiFurnCatalogSummary — what's actually in
    # My Library, so the plan gets grounded in real inventory instead of
    # generic guesses (see _build_furnish_catalog_note). Missing/malformed
    # degrades to the plain prompt, same as an older client that never
    # sent this field at all.
    catalog_raw = data.get("catalog")
    catalog_lines = [str(x) for x in catalog_raw if str(x).strip()] if isinstance(catalog_raw, list) else []
    system_prompt = _FURNISH_SYSTEM_PROMPT + _build_furnish_catalog_note(catalog_lines)

    # Nebius/Nemotron is the default provider: a side-by-side comparison
    # (compare_furnish_providers.py, run live across Office/Reception/
    # Conference Room/Dining Room/Living Room) showed it correctly used
    # every group/role pattern — WORKSTATION, FACING, SURROUND, MEDIA_FACING
    # — matching or exceeding what the prior default (Claude, with Ollama
    # fallback) was actually producing. Claude is no longer tried
    # automatically: it costs money per call, and is currently nonfunctional
    # anyway (Anthropic account out of credit — confirmed live, a "credit
    # balance too low" 400 on every request). Ollama stays as a silent
    # last-resort fallback — free, local, already proven to work — so a
    # Nebius outage/rate-limit during a live demo degrades gracefully
    # instead of failing the request outright.
    fallback_note = None
    try:
        # requested_provider is an explicit per-request OPT-IN override only
        # — e.g. re-testing Claude once billing is sorted, or forcing Ollama
        # for an offline/no-network demo. Leaving "provider" out of the
        # request body (every existing caller) uses the default chain below.
        if requested_provider == "nebius":
            raw, model_used = _ai_furnish_via_nebius(user_msg, system_prompt)
        elif requested_provider == "ollama":
            raw, model_used = _ai_furnish_via_ollama(user_msg, system_prompt)
        elif requested_provider == "claude":
            raw, model_used = _ai_furnish_via_claude(user_msg, system_prompt)
        else:
            try:
                raw, model_used = _ai_furnish_via_nebius(user_msg, system_prompt)
            except _AiFurnishProviderError as nebius_err:
                raw, model_used = _ai_furnish_via_ollama(user_msg, system_prompt)
                fallback_note = f"Nebius/Nemotron unavailable ({nebius_err}) — used {model_used} instead."
    except _AiFurnishProviderError as e:
        return jsonify({"error": str(e)}), e.status_code

    try:
        parsed = json.loads(raw)
        raw_items = parsed.get("items", [])
        if not isinstance(raw_items, list):
            raise ValueError("'items' is not a list")
    except Exception:
        return jsonify({"error": "Model returned invalid JSON", "raw": raw}), 502

    # A local 7B/14B model is far less reliable than a hosted frontier model
    # about strictly honoring a requested schema — validate/coerce every
    # field defensively rather than trusting it and letting a malformed
    # entry break the placement solver downstream.
    items = []
    for it in raw_items:
        if not isinstance(it, dict):
            continue
        name = str(it.get("item", "")).strip().lower()
        if not name:
            continue
        try:
            qty = max(1, min(20, int(it.get("qty", 1))))
        except (TypeError, ValueError):
            qty = 1
        zone = str(it.get("zone", "")).strip().lower()
        if zone not in _FURNISH_ZONES:
            zone = "center"
        group = it.get("group")
        group = str(group).strip() if isinstance(group, (str, int)) and str(group).strip() else None
        role = str(it.get("role", "")).strip().lower()
        # A role with no group (or vice versa) is meaningless to the
        # solver's group-layout logic — drop both rather than pass through
        # a half-formed pairing it would have to guess at.
        if role not in _FURNISH_ROLES or not group:
            group, role = None, None
        items.append({"item": name, "qty": qty, "zone": zone, "group": group, "role": role})

    resp = {"items": items, "model": model_used}
    if fallback_note:
        resp["note"] = fallback_note
    return jsonify(resp)


# ── Isaac Sim integration (shared with api_server.py) ────────────────────────

_isaac_tasks: dict[str, dict] = {}
_isaac_lock  = threading.Lock()
# task_id -> live Popen handle for the launched Kit process, so a later
# POST /isaac-sim/task/<id>/close can actually kill it. Kept separate from
# _isaac_tasks because set_status() below replaces that dict wholesale on
# every call (not a merge), so a Popen stashed in there would get silently
# dropped by the very next status update.
_isaac_procs: dict[str, "subprocess.Popen"] = {}

BASE_DIR_MRCNN = Path(__file__).parent   # D:\PlanLift\PlanLift


def _read_env_var_mrcnn(name: str) -> str:
    val = os.environ.get(name, "")
    if val:
        return val
    for env_file in [BASE_DIR_MRCNN / ".env", BASE_DIR_MRCNN.parent / ".env"]:
        if env_file.exists():
            for line in env_file.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                if k.strip() == name:
                    return v.strip().strip('"').strip("'")
    return ""


def _isaac_worker_mrcnn(task_id: str, glb_path: Path, floorplan_path: Path = None,
                         world_scale: float = 0.0, floors: list = None, rooms: list = None) -> None:
    def set_status(status: str, message: str = "", **extra) -> None:
        with _isaac_lock:
            task = {"status": status, "message": message}
            task.update(extra)
            _isaac_tasks[task_id] = task

    script_path = None
    usd_path    = None

    try:
        set_status("checking", "Checking installation…")

        isaac_exe = _read_env_var_mrcnn("ISAAC_SIM_PATH")
        if not isaac_exe:
            set_status("error", "ISAAC_SIM_PATH not set in .env file.")
            return
        isaac_path = Path(isaac_exe)
        use_pip_launch = False
        kit_exe = None
        if isaac_path.name.startswith("python") and isaac_path.exists():
            isaacsim_pkg = isaac_path.parent.parent / "Lib" / "site-packages" / "isaacsim"
            if isaacsim_pkg.is_dir():
                use_pip_launch = True
                kit_exe = isaac_path
            else:
                set_status("error", f"isaacsim package not found at {isaacsim_pkg}")
                return
        elif isaac_path.name == "kit.exe" and isaac_path.exists():
            kit_exe = isaac_path
        else:
            for c in [isaac_path.parent/"kit"/"kit.exe", isaac_path/"kit"/"kit.exe"]:
                if c.exists(): kit_exe = c; break
        if not kit_exe:
            set_status("error", f"kit.exe / python not found at {isaac_path}")
            return

        kit_app_dir = Path(_read_env_var_mrcnn("KIT_APP_PATH") or r"D:\PlanLift\Kit_App")
        kit_file    = kit_app_dir / "apps" / "planlift_timelapse.kit"
        ext_folder  = kit_app_dir / "exts"
        startup_py  = kit_app_dir / "startup.py"
        if not kit_file.exists():
            set_status("error", f"Kit file not found: {kit_file}")
            return

        glb_uri  = "file:///" + glb_path.as_posix()
        usd_path = glb_path.with_suffix(".usd")
        usd_uri  = "file:///" + usd_path.as_posix()
        usd_abs  = str(usd_path)

        usd_ref = usd_path.as_posix()

        debug_log = (glb_path.parent / "inject_debug.txt").as_posix()

        # Embedded into the generated Python script below as Python literals
        # (repr), NOT via json.dumps — json.dumps emits `null`/`true`/`false`,
        # which are NameErrors in Python and broke the whole --exec script the
        # moment a floor had no wallHeightM override or a room carried a null
        # field ("NameError: name 'null' is not defined").
        _floors_meta_py = [
            {"idx": f["idx"], "label": f["label"], "yBase": f["yBase"],
             "image": Path(f["image"]).as_posix(), "wallHeightM": f.get("wallHeightM")}
            for f in (floors or [])
        ]
        _rooms_py = rooms or []

        script_path = glb_path.parent / f"inject_{task_id}.py"
        script_body = f"""\
import asyncio, sys, threading, time as _t, carb, omni.usd, omni.kit.app
from pxr import UsdGeom

GLB_URI  = "{glb_uri}"
USD_URI  = "{usd_uri}"
GLB_PATH = "{glb_path.as_posix()}"
USD_PATH = "{usd_path.as_posix()}"
USD_REF = "{usd_ref}"
FLOORPLAN_PATH = "{floorplan_path.as_posix() if floorplan_path else ''}"
# World units per floor-plan-image pixel — lets the exact-footprint sizing
# below match editor.html's true image extent instead of only ever
# guessing from the converted mesh's bounding box.
WORLD_SCALE = {world_scale if world_scale else 0}
# One entry per floor: {{"idx":int,"label":str,"yBase":float,"image":path}}.
# Empty when the browser sent no floors_meta (older client) — the
# floor-plane loop below then falls back to the single FLOORPLAN_PATH pair
# above as a synthetic one-floor list.
FLOORS_META = {_floors_meta_py!r}
# Labeled rooms -> real-world (x,y,z), for the Isaac Sim AI Agent panel to
# resolve a typed room name against. Empty when the browser sent no
# rooms_data (nothing labeled yet, or an older client).
ROOMS_DATA = {_rooms_py!r}
_DEBUG  = r"{debug_log}"

def _log(msg):
    import datetime
    line = datetime.datetime.now().strftime("%H:%M:%S") + " " + msg + "\\n"
    try:
        with open(_DEBUG, "a", encoding="utf-8") as f:
            f.write(line)
    except Exception:
        pass
    print("[PlanLift] " + msg, flush=True)

_log("script loaded by Kit --exec")
_app = omni.kit.app.get_app()

async def _convert_and_inject():
    ctx = omni.usd.get_context()
    stage = ctx.get_stage()
    if not stage:
        _log("ERROR: no stage")
        print("[PlanLift] injection-failed: no stage", flush=True)
        return
    _log("converting GLB to USD...")
    print("[PlanLift] converting-glb", flush=True)
    try:
        import omni.kit.asset_converter as ac
        task = ac.get_instance().create_converter_task(
            GLB_PATH, USD_PATH, None, ac.AssetConverterContext())
        ok = await task.wait_until_finished()
        if not ok:
            _log("ERROR: converter returned False")
            print("[PlanLift] injection-failed: conversion error", flush=True)
            return
        _log("conversion OK")
    except Exception as e:
        _log(f"ERROR: converter: {{e}}")
        print(f"[PlanLift] injection-failed: {{e}}", flush=True)
        return

    # ── Pre-flatten Xform > Looks + inner Mesh -> flat Mesh ──────────────────
    # Clear all instanceable flags first so GetChildren() returns real children,
    # then flatten every Xform+Looks+inner-Mesh prim and bind OmniGlass to it.
    _fl_paths = []
    try:
        from pxr import Usd as _Usd, UsdGeom as _UsdGeom, UsdShade as _UsdShade, Sdf as _Sdf
        _fl_st = _Usd.Stage.Open(USD_PATH)
        if not _fl_st:
            _log("ERROR: pre-flatten: could not open stage")
        else:
            _fl_rl = _fl_st.GetRootLayer()
            _GATTRS = ["points","faceVertexCounts","faceVertexIndices",
                       "normals","doubleSided","subdivisionScheme","orientation","extent"]
            # Step A: collect all instanceable prim paths
            _inst_paths = [p.GetPath() for p in _fl_st.Traverse() if p.IsInstance()]
            _log(f"pre-flatten: {{len(_inst_paths)}} instanceable prims")
            # Step B: clear instanceable -> USD recomposes, GetChildren returns real children
            with _Usd.EditContext(_fl_st, _fl_rl):
                for _ip in _inst_paths:
                    _ipr = _fl_st.GetPrimAtPath(_ip)
                    if _ipr and _ipr.IsValid():
                        _ipr.SetInstanceable(False)
            # Step C: collect Xform+Looks+inner-Mesh candidates that are
            # ACTUALLY glass -- matched the same way api_server.py's own
            # building-glass scan does (material name prefix
            # 'PlanLift_Glass', set client-side by editor.html's
            # pbrGlass()). This used to match on prim SHAPE alone (any
            # Xform wrapping one Mesh + a Looks scope), with no material
            # check at all -- that shape is generic enough that an
            # ordinary furniture sub-mesh (e.g. a wood door's edge/frame
            # piece) matches it too, so it got its real material
            # force-replaced with OmniGlass below, rendering near-black
            # with no strong reflections in the scene. Reported as "wrong
            # black material on the door."
            _fcands = []
            for _fp in _fl_st.Traverse():
                if _fp.GetTypeName() != "Xform":
                    continue
                _ckids = {{c.GetName(): c.GetTypeName() for c in _fp.GetChildren()}}
                _imname = next((n for n,t in _ckids.items() if t == "Mesh"), None)
                if not (_imname and "Looks" in _ckids):
                    continue
                _finner_chk = _fl_st.GetPrimAtPath(_fp.AppendChild(_imname))
                _mat_path = _UsdShade.MaterialBindingAPI(_finner_chk).GetDirectBinding().GetMaterialPath()
                if _mat_path and _mat_path.name.startswith("PlanLift_Glass"):
                    _fcands.append((_fp.GetPath(), _imname))
            _log(f"pre-flatten: {{len(_fcands)}} Xform+Looks+Mesh glass candidates")
            # Step D: flatten + create OmniGlass + bind
            if _fcands:
                with _Usd.EditContext(_fl_st, _fl_rl):
                    # Material must live inside the defaultPrim so USD reference
                    # composition includes it when Kit loads this file as a reference.
                    _root_prim = _fl_st.GetDefaultPrim()
                    _gm_parent = (
                        _root_prim.GetPath() if (_root_prim and _root_prim.IsValid())
                        else next(
                            (p.GetPath() for p in _fl_st.GetPseudoRoot().GetChildren()),
                            _Sdf.Path("/World")
                        )
                    )
                    _gm_path = _gm_parent.AppendChild("PlanLift_GlassMat")
                    _gm = _UsdShade.Material.Define(_fl_st, _gm_path)
                    _gs = _UsdShade.Shader.Define(_fl_st, str(_gm_path) + "/Shader")
                    _gs.SetSourceAsset(_Sdf.AssetPath("OmniGlass.mdl"), "mdl")
                    _gs.SetSourceAssetSubIdentifier("OmniGlass", "mdl")
                    _gs.CreateInput("glass_color", _Sdf.ValueTypeNames.Color3f).Set((0.9,0.95,1.0))
                    _gs.CreateInput("glass_ior", _Sdf.ValueTypeNames.Float).Set(1.491)
                    _gs.CreateInput("frosting_roughness", _Sdf.ValueTypeNames.Float).Set(0.0)
                    _gs.CreateInput("enable_opacity", _Sdf.ValueTypeNames.Bool).Set(True)
                    _gs.CreateInput("cutout_opacity", _Sdf.ValueTypeNames.Float).Set(0.15)
                    _gm.CreateSurfaceOutput("mdl").ConnectToSource(_gs.ConnectableAPI(), "out")
                    _gm.CreateDisplacementOutput("mdl").ConnectToSource(_gs.ConnectableAPI(), "out")
                    _gm.CreateVolumeOutput("mdl").ConnectToSource(_gs.ConnectableAPI(), "out")
                    for (_fpath, _imname) in _fcands:
                        _fxp    = _fl_st.GetPrimAtPath(_fpath)
                        _finner = _fl_st.GetPrimAtPath(_fpath.AppendChild(_imname))
                        if not (_fxp and _fxp.IsValid() and _finner and _finner.IsValid()):
                            _log(f"  pre-flatten: skip {{_fpath}}")
                            continue
                        for _an in _GATTRS:
                            _sa = _finner.GetAttribute(_an)
                            if _sa and _sa.IsValid():
                                _sv = _sa.Get()
                                if _sv is not None:
                                    try:
                                        _fxp.CreateAttribute(_an,_sa.GetTypeName(),custom=False).Set(_sv)
                                    except Exception: pass
                        for _pv in _UsdGeom.PrimvarsAPI(_finner).GetPrimvars():
                            try:
                                _pval = _pv.Get()
                                if _pval is None: continue
                                _npv = _UsdGeom.PrimvarsAPI(_fxp).CreatePrimvar(
                                    _pv.GetPrimvarName(),_pv.GetTypeName(),_pv.GetInterpolation())
                                _npv.Set(_pval)
                                if _pv.IsIndexed(): _npv.SetIndices(_pv.GetIndices())
                            except Exception: pass
                        _fxp.SetTypeName("Mesh")
                        _finner.SetActive(False)
                        _UsdShade.MaterialBindingAPI(_fxp).Bind(_gm)
                        _fl_paths.append(str(_fpath))
                        _log(f"  pre-flatten: {{_fpath}}")
                _fl_rl.Save()
                _log(f"pre-flatten: saved {{len(_fcands)}} flattened Mesh prims")
    except Exception as _fe:
        _log(f"ERROR: pre-flatten: {{_fe}}")

    try:
        from pxr import Gf, UsdGeom
        xform = UsdGeom.Xform.Define(stage, "/PlanLift_Furniture")
        prim  = xform.GetPrim()
        prim.GetReferences().AddReference(USD_REF)
        xform.AddRotateXOp().Set(90.0)
        _log("AddReference + axis correction OK -- furniture injected!")
        print("[PlanLift] model-injected", flush=True)
    except Exception as e:
        _log(f"ERROR: AddReference: {{e}}")
        print(f"[PlanLift] injection-failed: {{e}}", flush=True)

    # ── 2D floorplan image(s) as horizontal floor-plane textures ──────────
    # One plane per floor (FLOORS_META), each at that floor's own yBase, so
    # Floor Plan View and the camera/zone tools can resolve position and
    # height against the RIGHT floor instead of one shared ground plane.
    # Floor idx 0 keeps the original unsuffixed prim path for backward
    # compatibility with any code still looking for a single
    # /PlanLift_FloorPlanImage; floors 1+ get an indexed sibling.
    _floors_list = FLOORS_META if FLOORS_META else (
        [{{"idx": 0, "label": "Floor 1", "yBase": 0.0, "image": FLOORPLAN_PATH}}] if FLOORPLAN_PATH else []
    )
    for _fp_entry in _floors_list:
        _fp_idx   = _fp_entry.get("idx", 0)
        _fp_ybase = float(_fp_entry.get("yBase", 0.0))
        _fp_img   = _fp_entry.get("image", "")
        if not _fp_img:
            continue
        try:
            from pxr import Gf as _Gf2, Sdf as _Sdf2, Usd as _Usd2
            from pxr import UsdGeom as _UG2, UsdShade as _US2

            # Exact footprint when the browser sent a world_scale: pixel-
            # perfect to editor.html's true image extent, same formula
            # api_server.py uses. Falls back to a bbox guess (this file's
            # original-only method) when no world_scale was sent.
            #
            # WORLD_SCALE*pixels gives a REAL-WORLD METERS footprint, but
            # this stage's own geometry (the GLB-converted walls/furniture)
            # lands at 1 unit = 1cm with no compensating scale anywhere in
            # this conversion — confirmed live: a wall's own Translate read
            # back as (461.56, 274.32, -386.93) in a floor plan whose real
            # footprint is ~15.7x11.8m, a clean ~100x ratio. Without scaling
            # this plane to match, it renders ~100x too small relative to
            # the actual walls, which is exactly what made it look like "the
            # wrong floor plan" even when the right file was loading fine —
            # it was dwarfed by the real geometry, not actually wrong. The
            # bbox-fallback branch below does NOT need this: it reads the
            # real stage geometry directly, which is already in this same
            # (correct) stage-unit scale.
            _STAGE_UNITS_PER_METER = 100.0
            _fp_cx = _fp_cy = _fp_hw = _fp_hd = None
            if WORLD_SCALE and WORLD_SCALE > 0:
                try:
                    from PIL import Image as _FPImg
                    with _FPImg.open(_fp_img) as _fpim:
                        _fp_iw, _fp_ih = _fpim.size
                    _fp_cx, _fp_cy = 0.0, 0.0
                    _fp_hw = (_fp_iw * WORLD_SCALE) / 2.0 * _STAGE_UNITS_PER_METER
                    _fp_hd = (_fp_ih * WORLD_SCALE) / 2.0 * _STAGE_UNITS_PER_METER
                    _log(f"FloorPlanImage[{{_fp_idx}}]: exact footprint from WORLD_SCALE={{WORLD_SCALE}} "
                         f"image={{_fp_iw}}x{{_fp_ih}}")
                except Exception as _fpwe:
                    _log(f"FloorPlanImage[{{_fp_idx}}]: WORLD_SCALE path failed ({{_fpwe}}), falling back to bbox")

            if _fp_hw is None:
                # Compute footprint from the converted USD (GLB Y-up space)
                _fp_stg = _Usd2.Stage.Open(USD_PATH)
                _fp_bc  = _UG2.BBoxCache(_Usd2.TimeCode.Default(),
                             [_UG2.Tokens.default_, _UG2.Tokens.render],
                             useExtentsHint=True)
                _fp_rng = _Gf2.Range3d()
                for _fp_p in _fp_stg.Traverse():
                    if _fp_p.IsA(_UG2.Boundable):
                        try:
                            _fp_bb = _fp_bc.ComputeWorldBound(_fp_p)
                            _fp_rng = _Gf2.Range3d.GetUnion(_fp_rng, _fp_bb.ComputeAlignedRange())
                        except Exception:
                            pass
                if _fp_rng.IsEmpty():
                    _log("FloorPlan: bbox empty, using 20x20 default")
                    _fp_cx, _fp_cy, _fp_hw, _fp_hd = 0.0, 0.0, 10.0, 10.0
                else:
                    _fp_mn = _fp_rng.GetMin()
                    _fp_mx = _fp_rng.GetMax()
                    # RotateX+90 maps: GLB-X->Stage-X, GLB-Z->Stage-(-Y), GLB-Y->Stage-Z
                    _fp_cx = (_fp_mn[0] + _fp_mx[0]) / 2.0
                    _fp_cy = -((_fp_mn[2] + _fp_mx[2]) / 2.0)
                    _fp_hw = abs(_fp_mx[0] - _fp_mn[0]) / 2.0 * 1.08
                    _fp_hd = abs(_fp_mx[2] - _fp_mn[2]) / 2.0 * 1.08
            # _fp_ybase (each floor's yBase, from editor.html's build3D) is
            # real METERS regardless of which branch above ran — the bbox
            # branch's X/Y came pre-scaled from real stage geometry, but Z
            # here is a SEPARATE value that still needs the same conversion.
            _fp_z  = (_fp_ybase - 0.005) * _STAGE_UNITS_PER_METER
            _fpp   = "/PlanLift_FloorPlanImage" if _fp_idx == 0 else f"/PlanLift_FloorPlanImage_{{_fp_idx}}"
            _fmesh = _UG2.Mesh.Define(stage, _fpp)
            _fmesh.CreatePointsAttr().Set([
                _Gf2.Vec3f(_fp_cx - _fp_hw, _fp_cy - _fp_hd, _fp_z),
                _Gf2.Vec3f(_fp_cx + _fp_hw, _fp_cy - _fp_hd, _fp_z),
                _Gf2.Vec3f(_fp_cx + _fp_hw, _fp_cy + _fp_hd, _fp_z),
                _Gf2.Vec3f(_fp_cx - _fp_hw, _fp_cy + _fp_hd, _fp_z),
            ])
            _fmesh.CreateFaceVertexCountsAttr().Set([4])
            _fmesh.CreateFaceVertexIndicesAttr().Set([0, 1, 2, 3])
            _fmesh.CreateNormalsAttr().Set([_Gf2.Vec3f(0, 0, 1)] * 4)
            _fmesh.SetNormalsInterpolation(_UG2.Tokens.faceVarying)
            _fst = _UG2.PrimvarsAPI(_fmesh.GetPrim()).CreatePrimvar(
                "st", _Sdf2.ValueTypeNames.TexCoord2fArray, _UG2.Tokens.faceVarying)
            _fst.Set([_Gf2.Vec2f(0,0), _Gf2.Vec2f(1,0), _Gf2.Vec2f(1,1), _Gf2.Vec2f(0,1)])
            _fmat  = _US2.Material.Define(stage, _fpp + "/Mat")
            _fsh   = _US2.Shader.Define(stage, _fpp + "/Mat/Sh")
            _fsh.SetSourceAsset(_Sdf2.AssetPath("OmniPBR.mdl"), "mdl")
            _fsh.SetSourceAssetSubIdentifier("OmniPBR", "mdl")
            _ftex  = "file:///" + _fp_img
            _fsh.CreateInput("diffuse_texture",               _Sdf2.ValueTypeNames.Asset).Set(_Sdf2.AssetPath(_ftex))
            _fsh.CreateInput("metallic_constant",             _Sdf2.ValueTypeNames.Float).Set(0.0)
            _fsh.CreateInput("reflection_roughness_constant", _Sdf2.ValueTypeNames.Float).Set(1.0)
            _fmat.CreateSurfaceOutput("mdl").ConnectToSource(_fsh.ConnectableAPI(), "out")
            _fmat.CreateDisplacementOutput("mdl").ConnectToSource(_fsh.ConnectableAPI(), "out")
            _fmat.CreateVolumeOutput("mdl").ConnectToSource(_fsh.ConnectableAPI(), "out")
            _US2.MaterialBindingAPI(_fmesh.GetPrim()).Bind(_fmat)
            _log(f"FloorPlanImage[{{_fp_idx}}]: {{_fp_hw*2:.1f}}x{{_fp_hd*2:.1f}} stage units plane at "
                 f"({{_fp_cx:.1f}}, {{_fp_cy:.1f}}, {{_fp_z:.3f}})")
        except Exception as _fpe:
            _log(f"ERROR: FloorPlanImage[{{_fp_idx}}]: {{_fpe}}")

    # ── Floor Plan View image pinning ────────────────────────────────────
    # Without this call, planlift_floorview's 2D window has no way to know
    # which floor-plan image belongs to THIS launch — its own on-update loop
    # falls back to "whichever *_floorplan.png in isaac_tmp/ has the newest
    # mtime" (see its _on_update), which silently shows a stale/unrelated
    # floor plan from a previous run whenever any leftover PNG in that
    # never-cleaned folder (or a file-copy timestamp quirk) ends up looking
    # newer than this run's own file. Pin it explicitly with the floor data
    # this script already built _floors_list from above (for the 3D
    # floor-plane textures) — floorKey uses the same "floor_N index + 1"
    # string convention set_floor_images/_floor_z_ranges both already
    # document. Same fix as api_server.py's _convert_and_inject — see the
    # AI Agent panel context comment just below for why this needs to exist
    # in BOTH backends' own separately-generated injected script (this is
    # the one the "Mask R-CNN" backend selector actually routes through).
    try:
        from planlift_floorview.extension import set_floor_images as _set_floor_imgs
        _floor_image_map = {{str(_e.get("idx", 0) + 1): _e.get("image", "") for _e in _floors_list if _e.get("image")}}
        _set_floor_imgs(_floor_image_map)
        _log(f"Floor Plan View image(s) pinned — {{len(_floor_image_map)}} floor(s)")
    except Exception as _fie:
        _log(f"ERROR: set_floor_images failed: {{_fie}}")

    # ── AI Agent panel context ──────────────────────────────────────────
    # Hands the labeled-room list (ROOMS_DATA, already real-world wx/wy/wz
    # — see editor.html's _buildRoomsDataForAgent) to planlift_ui_setup's
    # AI Agent panel, which creates itself on first call. img_w/img_h are
    # passed as 0 — the panel's actual room resolution uses each room's
    # own precomputed wx/wy/wz directly, never re-derives them from image
    # pixel size, so these two are vestigial/kept only for the function's
    # documented signature. This mirrors api_server.py's OWN identical
    # call in its _convert_and_inject — the two backends (Mask R-CNN here,
    # CubiCasa there) each generate their own separate injected Kit
    # script, so this needs to exist in BOTH or Isaac Sim's AI Agent panel
    # only works when launched from whichever ONE backend happens to have
    # it (confirmed live: the "Mask R-CNN" backend selector routes here,
    # not to api_server.py, so this file's own script generation was the
    # one actually missing it).
    try:
        from planlift_ui_setup.extension import set_agent_context as _set_agent_ctx
        _set_agent_ctx(ROOMS_DATA, WORLD_SCALE, 0, 0)
        _log(f"AI Agent panel context set — {{len(ROOMS_DATA)}} room(s)")
    except Exception as _ae:
        _log(f"ERROR: set_agent_context failed: {{_ae}}")

def _inject_worker(kit_loop):
    _log("worker thread started -- sleeping 15s for Kit startup")
    _t.sleep(15)
    ctx = omni.usd.get_context()
    _log("polling stage (max 60s real time)...")
    deadline = _t.monotonic() + 60
    while _t.monotonic() < deadline:
        _t.sleep(1)
        try:
            stage = ctx.get_stage()
            if stage and not ctx.is_stage_loading():
                _log("stage ready!")
                break
        except Exception:
            pass
    else:
        _log("WARNING: stage poll timed out (60s), injecting anyway")
    future = asyncio.run_coroutine_threadsafe(_convert_and_inject(), kit_loop)
    try:
        future.result(timeout=120)
    except Exception as e:
        _log(f"ERROR: async part failed: {{e}}")
        print(f"[PlanLift] injection-failed: {{e}}", flush=True)

def _kickoff(e):
    _log("kickoff fired -- launching worker thread")
    sys._planlift_sub.unsubscribe()
    loop = asyncio.get_event_loop()
    sys._planlift_loop   = loop
    sys._planlift_thread = threading.Thread(
        target=_inject_worker, args=(loop,), daemon=True)
    sys._planlift_thread.start()

sys._planlift_sub = _app.get_update_event_stream().create_subscription_to_pop(
    _kickoff, name="planlift_inject_kickoff")
_log(f"subscription created: {{sys._planlift_sub}}")
"""
        script_path.write_text(script_body, encoding="utf-8")

        set_status("launching", "Launching Kit application…")
        # Streams the whole Kit app (not just the 3D viewport) over WebRTC
        # instead of opening a local window on this machine — "Open in Isaac
        # Sim" now means a new browser tab, not a window popping up on the
        # server. Fixed ports: confirmed live (this session) that
        # omni.kit.livestream.app only supports ONE client per instance
        # anyway, so there's no concurrent-session port conflict to solve
        # yet — if that changes, these need to become per-task_id allocated
        # ports instead of two fixed constants.
        STREAM_SIGNAL_PORT = 49100
        STREAM_MEDIA_PORT  = 47998
        stream_args = [
            "--no-window",
            "--enable", "omni.kit.livestream.app",
            f"--/exts/omni.kit.livestream.app/primaryStream/signalPort={STREAM_SIGNAL_PORT}",
            f"--/exts/omni.kit.livestream.app/primaryStream/streamPort={STREAM_MEDIA_PORT}",
            "--/exts/omni.kit.livestream.app/primaryStream/streamType=webrtc",
            # NOT overriding targetFps anymore — it was lowered to 30 in an
            # earlier attempt, but omni.kit.livestream.core's own
            # extension.toml hardcodes the actual main/render/present loop
            # rate limiters (app.runLoops.*.rateLimitFrequency) to 60 as
            # part of its shipped, tested streaming defaults. Lowering only
            # the stream's own targetFps to 30 without touching those loop
            # rates created a 60-vs-30 mismatch: Kit kept producing frames
            # at 60/sec while the stream layer only targeted 30/sec,
            # and the backlog from that mismatch is what grew into the
            # multi-second input-to-viewport delay reported live — not a
            # GPU/encoder capacity problem (NVENC never exceeded ~13%
            # confirmed via Task Manager). Leaving targetFps at the
            # extension's own default keeps it matched to the loop rates.
            "--/exts/omni.services.livestream.session/quitOnSessionEnded=true",
            # Force RTX Real-Time (rasterized) rendering instead of Path
            # Tracing. The .kit app's own [settings] already requests
            # "RaytracedLighting" (confirmed = Real-Time mode, vs.
            # "PathTracing" = Interactive, via omni.kit.viewport.menubar.render's
            # hd_renderer_list.py), but a previously-saved viewport/stage
            # preference was overriding it back to Path Tracing at runtime —
            # confirmed live by the stream HUD showing "PathTracing: 512/512
            # spp: 6.55 sec", which re-accumulates from scratch on every
            # camera move and was the actual cause of the severe navigation
            # lag (not streaming/encoder capacity). A CLI setting override
            # always wins over both the app file and any saved preference
            # in Kit's settings precedence, so this is the one place that's
            # guaranteed to stick regardless of what's clobbering it.
            "--/rtx/rendermode=RaytracedLighting",
        ]
        if use_pip_launch:
            isaacsim_exe = kit_exe.parent / "isaacsim.exe"
            cmd = [
                str(isaacsim_exe),
                str(kit_file),
                "--ext-folder", str(ext_folder),
                "--enable",     "planlift.ui_setup",
                *stream_args,
                "--exec",       str(startup_py),
                "--exec",       str(script_path),
            ]
        else:
            cmd = [
                str(kit_exe), str(kit_file),
                "--ext-folder", str(ext_folder),
                "--enable",     "planlift.ui_setup",
                *stream_args,
                "--exec",       str(startup_py),
                "--exec",       str(script_path),
            ]
        launch_env = os.environ.copy()
        launch_env["OMNI_KIT_ACCEPT_EULA"] = "YES"
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, encoding="utf-8", errors="replace", env=launch_env)
        with _isaac_lock:
            _isaac_procs[task_id] = proc

        import queue as _queue
        _q: _queue.Queue = _queue.Queue()

        def _kit_reader():
            try:
                for raw in proc.stdout:
                    line = raw.rstrip()
                    print(f"[Kit] {line}", flush=True)
                    _q.put(line)
            except Exception:
                pass
            finally:
                _q.put(None)

        threading.Thread(target=_kit_reader, daemon=True).start()

        set_status("waiting",
            "Kit app loading -- shader compilation takes 2–5 min on first launch. "
            "'Not Responding' in the title bar is normal. Please wait…")

        deadline = time.time() + 900
        injected = False

        while time.time() < deadline:
            try:
                line = _q.get(timeout=1)
            except _queue.Empty:
                if proc.poll() is not None:
                    break
                continue
            if line is None:
                break
            low = line.lower()
            if "[planlift] converting-glb" in low:
                set_status("converting", "Converting GLB -> USD inside Kit…")
            elif "[planlift] model-injected" in low:
                injected = True
                break
            elif "[planlift] injection-failed" in low:
                set_status("error", f"Injection failed: {line}")
                return

        if proc.poll() is not None and proc.returncode not in (0, None):
            set_status("error", f"Kit app exited with code {proc.returncode}.")
            return

        # omni.kit.livestream.app starts very early in the extension-load
        # order (confirmed live: well before the custom PlanLift extensions,
        # long before the --exec injection script even runs), so the
        # signaling port should already be listening by this point — this
        # is a cheap confirmation, not a long wait, before handing the
        # frontend a URL it's about to try opening.
        stream_ready = False
        for _ in range(10):
            try:
                with socket.create_connection(("127.0.0.1", STREAM_SIGNAL_PORT), timeout=1):
                    stream_ready = True
                    break
            except OSError:
                time.sleep(1)

        set_status("done",
            "Furniture model loaded — opening Isaac Sim stream…" if (injected and stream_ready)
            else "Furniture model loaded into the timelapse scene!" if injected
            else "Kit app is open. Furniture is loading into the scene…",
            stream_ready=stream_ready, signal_port=STREAM_SIGNAL_PORT)

    except Exception as exc:
        set_status("error", str(exc))
    finally:
        def _cleanup():
            time.sleep(3600)   # keep USD for 1 h so Kit can reference it
            # floorplan_path/per-floor floorplan PNGs/{task_id}_floors.json
            # are deliberately NOT included here: Floor Plan View keeps
            # polling isaac_tmp for the newest *_floors.json (or
            # *_floorplan.png for a session with no manifest) for as long
            # as that Isaac Sim session runs, which is routinely much
            # longer than this 1h window. Deleting them on the same timer
            # as the transient glb/usd/script left the newest-by-mtime scan
            # falling back to whatever older files happened to still be on
            # disk, showing a stale floorplan even though the current
            # session's model was correct.
            for p in filter(None, [glb_path, script_path, usd_path]):
                try:
                    p.unlink(missing_ok=True)
                except Exception:
                    pass
        threading.Thread(target=_cleanup, daemon=True).start()


@app.route("/isaac-sim/launch-and-open", methods=["POST"])
def isaac_launch_and_open():
    if "file" not in request.files:
        return jsonify({"error": "No GLB file provided"}), 400
    task_id  = uuid.uuid4().hex
    glb_dir  = BASE_DIR_MRCNN / "output" / "isaac_tmp"
    glb_dir.mkdir(parents=True, exist_ok=True)
    glb_path = glb_dir / f"{task_id}.glb"
    request.files["file"].save(str(glb_path))
    try:
        world_scale = float(request.form.get("world_scale", 0) or 0)
    except (TypeError, ValueError):
        world_scale = 0.0

    # Per-floor floorplan images: floorplan_image_<idx> fields + a
    # floors_meta JSON array of {idx, label, yBase}, one entry per floor of
    # the exported building. Falls back to the single legacy
    # floorplan_image field (pre-multi-floor client) as one synthetic
    # Floor-1 entry, so an un-updated frontend still works.
    floors_meta = []
    floors_meta_raw = request.form.get("floors_meta", "")
    if floors_meta_raw:
        try:
            floors_meta = json.loads(floors_meta_raw)
        except (TypeError, ValueError):
            floors_meta = []

    floors = []  # [{idx, label, yBase, image: <saved absolute path>}]
    if floors_meta:
        for entry in floors_meta:
            idx = int(entry.get("idx", 0))
            field = f"floorplan_image_{idx}"
            if field not in request.files:
                continue
            img_path = glb_dir / f"{task_id}_floorplan_{idx}.png"
            request.files[field].save(str(img_path))
            wall_height_m = entry.get("wallHeightM")
            floors.append({
                "idx": idx,
                "label": entry.get("label") or f"Floor {idx + 1}",
                "yBase": float(entry.get("yBase", 0.0)),
                "image": str(img_path),
                "wallHeightM": float(wall_height_m) if wall_height_m is not None else None,
            })
    elif "floorplan_image" in request.files:
        img_path = glb_dir / f"{task_id}_floorplan.png"
        request.files["floorplan_image"].save(str(img_path))
        floors.append({"idx": 0, "label": "Floor 1", "yBase": 0.0, "image": str(img_path), "wallHeightM": None})

    floorplan_path = Path(floors[0]["image"]) if floors else None
    if floors:
        logger.info("[Isaac] %d floorplan image(s) saved for task %s", len(floors), task_id)
        floors_json_path = glb_dir / f"{task_id}_floors.json"
        floors_json_path.write_text(
            json.dumps({"task_id": task_id, "world_scale": world_scale, "floors": floors}),
            encoding="utf-8",
        )
    else:
        logger.info("[Isaac] no floorplan_image field(s) in request")

    # Labeled rooms -> real-world coordinates, for the Isaac Sim AI Agent to
    # resolve room names against. Optional -- an un-labeled or un-updated
    # client simply sends nothing.
    rooms = []
    rooms_data_raw = request.form.get("rooms_data", "")
    if rooms_data_raw:
        try:
            rooms = json.loads(rooms_data_raw)
        except (TypeError, ValueError):
            rooms = []
    if rooms:
        rooms_json_path = glb_dir / f"{task_id}_rooms.json"
        rooms_json_path.write_text(
            json.dumps({"task_id": task_id, "rooms": rooms}),
            encoding="utf-8",
        )

    with _isaac_lock:
        _isaac_tasks[task_id] = {"status": "starting", "message": ""}
    threading.Thread(
        target=_isaac_worker_mrcnn,
        args=(task_id, glb_path, floorplan_path, world_scale, floors, rooms),
        daemon=True,
    ).start()
    return jsonify({"task_id": task_id})


@app.route("/isaac-sim/task/<task_id>", methods=["GET"])
def isaac_task_status(task_id: str):
    with _isaac_lock:
        task = _isaac_tasks.get(task_id)
    if task is None:
        return jsonify({"error": "Task not found"}), 404
    return jsonify(task)


@app.route("/isaac-sim/task/<task_id>/close", methods=["POST"])
def isaac_task_close(task_id: str):
    """
    Kills the Kit process launched for this task. The browser already POSTs
    here when the Isaac Sim tab/window closes (editor.html's
    _isaacStreamCloseTimer) — this route just never existed, so that POST
    was a silent 404 and the Kit process (and the whole Isaac Sim session
    state baked into it: the AI Agent panel, any code it was built from
    before a later extension edit, etc.) kept running in the background
    forever. Uses `taskkill /T` rather than proc.terminate() because Kit is
    launched via a wrapper exe (isaacsim.exe or kit.exe) that spawns the
    real engine as a CHILD process — terminate() only signals the wrapper,
    leaving the actual Isaac Sim process as an orphan, which is exactly the
    "next launch picks up where the previous one left off" symptom this
    was reported as.
    """
    with _isaac_lock:
        proc = _isaac_procs.get(task_id)
    if proc is None:
        return jsonify({"error": "Task not found or already closed"}), 404
    if proc.poll() is not None:
        with _isaac_lock:
            _isaac_procs.pop(task_id, None)
        return jsonify({"status": "already-exited"})
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                            capture_output=True, text=True, timeout=15)
        else:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
    except Exception as e:
        logger.warning("[Isaac] close task %s: %s", task_id, e)
        return jsonify({"error": str(e)}), 500
    finally:
        with _isaac_lock:
            _isaac_procs.pop(task_id, None)
    return jsonify({"status": "closed"})


if __name__ == "__main__":
    print("=" * 62)
    print("  PlanLift -- Mask R-CNN API  ->  http://localhost:8001")
    print()
    print("  GET  /                        -> FloorPlanTo3D web UI")
    print("  POST /predict                 -> native FloorPlanTo3D JSON")
    print("  POST /analyze                 -> PlanLift-compatible JSON")
    print("  GET  /health                  -> model status")
    print("  GET  /textures/search?q=      -> PolyHaven texture search")
    print("  POST /textures/download       -> download & cache texture")
    print("  GET  /assets/search?term=     -> Sketchfab model search")
    print("  POST /assets/download         -> download & cache 3D model")
    print("  POST /isaac-sim/launch-and-open -> launch Isaac Sim")
    print("  GET  /isaac-sim/task/<id>     -> poll task status")
    print("=" * 62)
    # Pre-load Mask R-CNN so the server is ready immediately
    try:
        load_model()
    except Exception as e:
        logger.warning("Model pre-load failed (will retry on first request): %s", e)
    app.run(host="0.0.0.0", port=8001, debug=False, threaded=True)
