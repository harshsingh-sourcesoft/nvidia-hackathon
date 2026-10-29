import cv2, numpy as np, math
from pathlib import Path

npz = np.load('output/masks/floor2_masks.npz', allow_pickle=True)
wm = npz['walls'].astype(np.uint8)
op = (npz['openings'] > 128).astype(np.uint8) * 255

dist = cv2.distanceTransform(wm, cv2.DIST_L2, 5)
nz = dist[dist>0]
thick = float(np.median(nz)*2) if len(nz) else 6.0
print('thick:', thick)

min_run = max(int(min(wm.shape)*0.035), int(thick*4), 15)
kl = max(3, int(thick)//3*2+1)
closed = cv2.morphologyEx(wm, cv2.MORPH_CLOSE,
    cv2.getStructuringElement(cv2.MORPH_RECT,(kl,kl)))

walls = []
for is_h in (True, False):
    ks = (min_run,1) if is_h else (1,min_run)
    opened = cv2.morphologyEx(closed, cv2.MORPH_OPEN,
        cv2.getStructuringElement(cv2.MORPH_RECT, ks))
    n,_,stats,cents = cv2.connectedComponentsWithStats(opened, connectivity=8)
    for lbl in range(1,n):
        bw=float(stats[lbl,cv2.CC_STAT_WIDTH])
        bh=float(stats[lbl,cv2.CC_STAT_HEIGHT])
        run = bw if is_h else bh
        if run < min_run:
            continue
        cx=float(cents[lbl,0]); cy=float(cents[lbl,1])
        x0=float(stats[lbl,cv2.CC_STAT_LEFT])
        y0=float(stats[lbl,cv2.CC_STAT_TOP])
        if is_h:
            walls.append({
                'id': 'mw%d' % len(walls),
                'start': {'x': x0, 'y': cy},
                'end': {'x': x0+bw, 'y': cy},
                'horiz': True
            })
        else:
            walls.append({
                'id': 'mw%d' % len(walls),
                'start': {'x': cx, 'y': y0},
                'end': {'x': cx, 'y': y0+bh},
                'horiz': False
            })

print('Walls: %d' % len(walls))
for w in walls:
    s = w['start']; e = w['end']
    print('  %s H=%s (%.0f,%.0f)->(%.0f,%.0f)' % (
        w['id'], w['horiz'], s['x'], s['y'], e['x'], e['y']))

n_cc,_,stats_op,cents_op = cv2.connectedComponentsWithStats(op, connectivity=8)
print('Opening CCs: %d' % (n_cc-1))

matches = []
for i in range(1, n_cc):
    ow = stats_op[i,cv2.CC_STAT_WIDTH]
    oh = stats_op[i,cv2.CC_STAT_HEIGHT]
    span = max(ow,oh)
    if span < 20 or span > 600:
        continue
    cx = float(cents_op[i,0])
    cy = float(cents_op[i,1])
    best_wall = None; best_t = 0.5; best_d = 1e9
    for wall in walls:
        x1=wall['start']['x']; y1=wall['start']['y']
        x2=wall['end']['x'];   y2=wall['end']['y']
        dx=x2-x1; dy=y2-y1; lsq=dx*dx+dy*dy
        if lsq < 1:
            continue
        t = max(0., min(1., ((cx-x1)*dx+(cy-y1)*dy)/lsq))
        px=x1+t*dx; py=y1+t*dy
        d = math.hypot(cx-px, cy-py)
        if d < best_d:
            best_d=d; best_wall=wall; best_t=t
    max_match = max(thick*8, 100)
    if best_wall and best_d < max_match:
        matches.append((i, span, best_wall['id'], best_t, best_d, cx, cy))

print('Matched openings: %d' % len(matches))
for m in matches[:20]:
    i, span, wid, t, d, cx, cy = m
    print('  CC%d span=%dpx wid=%s t=%.2f dist=%.0fpx cx=%.0f cy=%.0f' % (
        i, span, wid, t, d, cx, cy))

# Save debug image
dbg = cv2.imread('output/masks/floor2_preprocessed.png')
if dbg is not None:
    for w in walls:
        s = w['start']; e = w['end']
        cv2.line(dbg, (int(s['x']), int(s['y'])), (int(e['x']), int(e['y'])),
                 (255, 100, 0), 2)
    for m in matches:
        i, span, wid, t, d, cx, cy = m
        cv2.circle(dbg, (int(cx), int(cy)), 10, (0,0,255), 3)
        cv2.putText(dbg, '%dpx' % span, (int(cx)+5, int(cy)),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0,0,200), 1)
    cv2.imwrite('output/debug_openings_match.png', dbg)
    print('Saved: output/debug_openings_match.png')
