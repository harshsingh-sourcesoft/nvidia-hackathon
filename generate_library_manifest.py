#!/usr/bin/env python3
"""
Regenerates models/library/manifest.json from the actual folder structure
under models/ — the Build/Furnish catalog's category tree mirrors your
folder nesting directly, so organizing .glb files on disk is enough; no
manual manifest editing needed.

Run this again any time you add, move, or rename files under models/.

Convention:
  - Top-level catalog bucket (which of the app's Build / Furnish panels a
    model shows up in) is driven by a top-level models/Build/ or
    models/Furnish/ folder — Build for anything architectural (doors,
    windows, arches, stairs, ...), Furnish for furniture/decor. That
    folder segment itself is NOT shown as a redundant subcategory in the
    catalog (the app's Build/Furnish panels already are that split) —
    everything below it becomes the category breadcrumb, e.g.
    models/Furnish/Living Room/Sofas/Round/x.glb -> Furnish panel,
    Living Room > Sofas > Round.
  - Files not yet sorted into Build/ or Furnish/ (old flat layout, or
    mid-reorganization) fall back to the elementType door/window
    heuristic below, filed under Uncategorized, so nothing silently
    disappears from the catalog.
  - elementType (door/window vs. plain furniture — controls the 3D
    wall-opening-cutting behavior, separate from which catalog panel a
    model appears in) is inferred as "door"/"window" if any folder in the
    path is literally named that (case-insensitive, singular or plural) -
    otherwise "furniture". A file already known from the existing
    manifest keeps its previous elementType/footprint/color2D/thumbnail
    regardless of where it's moved to (matched by filename) - only its
    model path and category placement update to reflect its current
    location. A brand-new file gets sensible defaults.
  - Thumbnail: matched by exact filename (case-insensitive) against a
    .png in models/library/thumbs/ - e.g. Corner_Sofa_1.glb matches
    Corner_Sofa_1.png. No match -> no thumbnail (catalog card just shows
    without an image), never guessed/fuzzy-matched.

Both frontend copies (app/ and frontend/1_nextjs-app/) are kept in sync -
this always regenerates both.
"""
import copy
import json
import re
from pathlib import Path

MODELS_ROOTS = [
    Path(r"D:\PlanLift\app\public\models"),
    Path(r"D:\PlanLift\frontend\1_nextjs-app\public\models"),
]

DOOR_HINTS = {"door", "doors"}
WINDOW_HINTS = {"window", "windows"}
BUILD_FOLDER = "build"
FURNISH_FOLDER = "furnish"
# Folders that are a specific model's own support files (e.g. a GLTF export's
# external texture images), not a browsable category of their own — never
# registered even when the directory-walk below would otherwise pick them up
# as an (empty-of-glb) category. Confirmed as a real issue: several models
# across the catalog ship a sibling "textures/" folder, and each one started
# showing up as its own clickable (but empty/pointless) folder card in the
# web app the moment empty directories started being registered at all.
INFRA_FOLDER_NAMES = {"textures"}

DEFAULT_FOOTPRINT = {
    "door":      {"widthM": 0.9, "depthM": 0.1, "heightM": 2.1},
    "window":    {"widthM": 1.2, "depthM": 0.1, "heightM": 1.2},
    "furniture": {"widthM": 1.0, "depthM": 1.0, "heightM": 1.0},
}


def slugify(s):
    s = re.sub(r"[^\w]+", "_", s.strip()).strip("_").lower()
    return s or "item"


def prettify(s):
    return re.sub(r"[_\-]+", " ", s).strip().title()


def element_type_for(parts):
    lower = {p.lower() for p in parts}
    if lower & DOOR_HINTS:
        return "door"
    if lower & WINDOW_HINTS:
        return "window"
    return "furniture"


def category_parts_for(parts, etype):
    """(top-level catalog bucket, sub-category breadcrumb below it)."""
    first = parts[0].lower() if parts else None
    if first == BUILD_FOLDER:
        return "Construction", (parts[1:] or ["Uncategorized"])
    if first == FURNISH_FOLDER:
        return "Furniture", (parts[1:] or ["Uncategorized"])
    # Not yet sorted into Build/Furnish — fall back to the elementType
    # heuristic so the file still shows up somewhere sensible.
    top = "Construction" if etype in ("door", "window") else "Furniture"
    return top, (parts or ["Uncategorized"])


def find_thumbnail(root, stem):
    thumbs_dir = root / "library" / "thumbs"
    if not thumbs_dir.is_dir():
        return None
    for f in thumbs_dir.iterdir():
        if f.is_file() and f.suffix.lower() == ".png" and f.stem.lower() == stem.lower():
            return f"/models/library/thumbs/{f.name}"
    return None


def find_category_thumbnail(root, category_id):
    """Top-level folder cards (Living Room, Bedroom, Decor, ...) get their
    own staged/lifestyle thumbnail, separate from individual model
    thumbnails - matched by category id against models/library/
    category_thumbs/<id>.(jpg|jpeg|png), same filename-match convention
    find_thumbnail() uses for models. Never guessed/fuzzy-matched: no file,
    no thumbnail, category card just falls back to the folder emoji."""
    thumbs_dir = root / "library" / "category_thumbs"
    if not thumbs_dir.is_dir():
        return None
    for ext in (".jpg", ".jpeg", ".png"):
        f = thumbs_dir / f"{category_id}{ext}"
        if f.is_file():
            return f"/models/library/category_thumbs/{f.name}"
    return None


def index_existing_manifest(node, out=None):
    """Maps glb filename -> its previous leaf dict, so metadata (footprint,
    elementType, color2D, thumbnail, id, name) survives being moved into a
    new folder. Keyed by filename, not full path, since the whole point is
    the file's path is about to change."""
    if out is None:
        out = {}
    for child in node.get("children", []):
        if "asset" in child:
            model = child["asset"].get("model")
            if model:
                out[Path(model).name] = child
        else:
            index_existing_manifest(child, out)
    return out


def build_tree(root, existing_by_name):
    tree = {"id": "root", "name": "Build", "children": []}
    category_index = {}

    def get_category(parts):
        if not parts:
            return tree
        key = tuple(parts)
        if key in category_index:
            return category_index[key]
        parent = get_category(parts[:-1])
        node = {"id": slugify(parts[-1]), "name": parts[-1], "children": []}
        cat_thumb = find_category_thumbnail(root, node["id"])
        if cat_thumb:
            node["thumbnail"] = cat_thumb
        parent["children"].append(node)
        category_index[key] = node
        return node

    # Register every directory under Build/Furnish as a category, even one
    # with no .glb files in it yet — without this, a freshly-created but
    # still-empty folder is invisible to the generator entirely, since it
    # otherwise only ever discovers a category by walking an actual .glb
    # file into existence. Mirrors the script's own stated goal ("the
    # catalog's category tree mirrors your folder nesting directly") for
    # the folder structure itself, not just populated ones. Reuses
    # element_type_for/category_parts_for exactly as the file loop below
    # does (both only ever look at path segment NAMES, not file content,
    # so they work identically given a bare directory path) — same
    # category placement either way, whichever is processed first.
    for d in sorted(root.rglob("*")):
        if not d.is_dir():
            continue
        rel = d.relative_to(root)
        parts = list(rel.parts)
        if not parts or parts[0].lower() == "library":
            continue
        if len(parts) == 1 and parts[0].lower() in (BUILD_FOLDER, FURNISH_FOLDER):
            continue  # the bare top-level bucket itself isn't a subcategory
        if any(p.lower() in INFRA_FOLDER_NAMES for p in parts):
            continue  # a model's own support files, not a browsable category (see INFRA_FOLDER_NAMES)
        etype = element_type_for(parts)
        top, sub_parts = category_parts_for(parts, etype)
        get_category([top] + sub_parts)

    for glb in sorted(root.rglob("*.glb")):
        rel = glb.relative_to(root)
        parts = list(rel.parts[:-1])
        if parts and parts[0].lower() == "library":
            continue  # library/ is manifest+thumbs infrastructure, not asset content

        model_path = "/models/" + rel.as_posix()
        prior = existing_by_name.get(glb.name)

        if prior:
            leaf = copy.deepcopy(prior)
            leaf["asset"]["model"] = model_path  # may have moved - always point at current location
            etype = leaf["asset"].get("elementType", "furniture")
        else:
            etype = element_type_for(parts)
            leaf = {
                "id": slugify(glb.stem),
                "name": prettify(glb.stem),
                "asset": {
                    "elementType": etype,
                    "model": model_path,
                    "footprint": DEFAULT_FOOTPRINT[etype],
                },
            }

        # Always re-check disk for a matching thumbnail, live, whether this
        # entry is brand new or preserved from a prior run - a real file
        # found now always wins over a stale/absent thumbnail field, so a
        # thumbnail added later (e.g. via generate_thumbnails.py) gets
        # picked up on the next run even for an already-known model instead
        # of being stuck with whatever thumbnail field it had before.
        thumb = find_thumbnail(root, glb.stem)
        if thumb:
            leaf["thumbnail"] = thumb

        # Always nest under the existing top-level "Construction"/"Furniture"
        # branch - those are the only two ids the catalog UI's Build/Furnish
        # panels actually look at (rootId:'construction'/'furniture',
        # editor.html _buildCatalog/_furnishCatalog). A folder path becomes a
        # SUB-category inside that branch, never a new top-level sibling of
        # it, or the panels would never see it.
        top, sub_parts = category_parts_for(parts, etype)
        category_parts = [top] + sub_parts

        get_category(category_parts)["children"].append(leaf)

    return tree


def main():
    for root in MODELS_ROOTS:
        manifest_path = root / "library" / "manifest.json"
        existing = {}
        if manifest_path.exists():
            existing = index_existing_manifest(json.loads(manifest_path.read_text(encoding="utf-8")))
        tree = build_tree(root, existing)
        manifest_path.write_text(json.dumps(tree, indent=2) + "\n", encoding="utf-8")
        print(f"Wrote {manifest_path}  ({len(existing)} model(s) matched to existing entries)")


if __name__ == "__main__":
    main()
