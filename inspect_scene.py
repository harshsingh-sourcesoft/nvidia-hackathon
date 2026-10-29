import json
with open("output/vectors/floor9_scene.json") as f:
    s = json.load(f)
walls = s["walls"]
print("Walls:", len(walls))
for w in walls:
    print("  id=%d  poly_pts=%d  holes=%d  area=%d" % (w["id"], len(w["polygon"]), len(w["holes"]), w["area_px"]))
    for i, h in enumerate(w["holes"]):
        from shapely.geometry import Polygon
        p = Polygon(h)
        bb = p.bounds
        print("    hole[%d]: %d pts  area=%.0f  bbox_wh=%.0fx%.0f" % (
            i, len(h), p.area, bb[2]-bb[0], bb[3]-bb[1]))
