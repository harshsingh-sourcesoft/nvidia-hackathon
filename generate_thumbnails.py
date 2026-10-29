#!/usr/bin/env python3
"""
Batch-generates catalog thumbnails for any .glb under models/ that doesn't
already have a real matching PNG in models/library/thumbs/ (exact filename
match, case-insensitive - the same convention generate_library_manifest.py
uses to wire thumbnails into the catalog). Renders once against the app/
copy via headless Blender (_blender_render_thumbnail.py), then copies the
result into the Next.js copy's thumbs/ folder too, since both copies share
the same model files. Never overwrites a thumbnail you already added by
hand.

Run this after adding new models with no thumbnail of your own, then
re-run generate_library_manifest.py to wire the new thumbnails in (or just
run both - see the bottom of this file).
"""
import shutil
import subprocess
import sys
from pathlib import Path

BLENDER = r"C:\Program Files\Blender Foundation\Blender 4.5\blender.exe"
RENDER_SCRIPT = Path(__file__).parent / "_blender_render_thumbnail.py"

# app/ is the canonical source rendered from; frontend/1_nextjs-app/ mirrors
# the same model files, so its thumbs/ just gets the same PNGs copied over
# rather than re-rendering identical geometry twice.
SOURCE_ROOT = Path(r"D:\PlanLift\app\public\models")
MIRROR_ROOT = Path(r"D:\PlanLift\frontend\1_nextjs-app\public\models")


def has_existing_thumbnail(thumbs_dir, stem):
    if not thumbs_dir.is_dir():
        return False
    return any(
        f.is_file() and f.suffix.lower() == ".png" and f.stem.lower() == stem.lower()
        for f in thumbs_dir.iterdir()
    )


def main():
    thumbs_dir = SOURCE_ROOT / "library" / "thumbs"
    thumbs_dir.mkdir(parents=True, exist_ok=True)
    mirror_thumbs_dir = MIRROR_ROOT / "library" / "thumbs"
    mirror_thumbs_dir.mkdir(parents=True, exist_ok=True)

    glbs = sorted(SOURCE_ROOT.rglob("*.glb"))
    todo = [g for g in glbs if not (g.relative_to(SOURCE_ROOT).parts[0].lower() == "library")
            and not has_existing_thumbnail(thumbs_dir, g.stem)]

    if not todo:
        print("Nothing to do - every model already has a matching thumbnail.")
        return

    print(f"Generating {len(todo)} thumbnail(s):")
    ok, failed = [], []
    for glb in todo:
        out_png = thumbs_dir / f"{glb.stem}.png"
        print(f"  {glb.relative_to(SOURCE_ROOT)} -> {out_png.name} ...", end=" ", flush=True)
        result = subprocess.run(
            [BLENDER, "--background", "--factory-startup", "--python", str(RENDER_SCRIPT),
             "--", str(glb), str(out_png)],
            capture_output=True, text=True, timeout=300,
        )
        if result.returncode == 0 and out_png.exists():
            print("ok")
            ok.append(glb)
            shutil.copy2(out_png, mirror_thumbs_dir / out_png.name)
        else:
            print("FAILED")
            failed.append((glb, result.stdout[-800:] + result.stderr[-800:]))

    print(f"\n{len(ok)} succeeded, {len(failed)} failed.")
    for glb, log in failed:
        print(f"\n--- {glb} ---\n{log}")


if __name__ == "__main__":
    main()
