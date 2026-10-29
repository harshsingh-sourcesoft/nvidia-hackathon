import type {
  Point2D, Wall, Room, Floor, SnapConfig, SnapState, WindowItem, Door,
} from '@/types';

// ─── Constants ────────────────────────────────────────────────────────────────

/** Design-units per metre at zoom 1. 100 du = 1 m. */
export const PIXELS_PER_METER = 100;
/** Minimum wall length (design-units) to be accepted. */
export const MIN_WALL_LENGTH = 10;
/** Palette for auto-assigned room colours. */
const ROOM_COLORS = [
  '#dbeafe','#fef9c3','#dcfce7','#fce7f3',
  '#ede9fe','#ffedd5','#ccfbf1','#fef3c7',
];

// ─── Basic geometry ───────────────────────────────────────────────────────────

export function distance(a: Point2D, b: Point2D): number {
  return Math.hypot(b.x - a.x, b.y - a.y);
}

export function angleBetween(a: Point2D, b: Point2D): number {
  return Math.atan2(b.y - a.y, b.x - a.x);
}

export function angleDeg(a: Point2D, b: Point2D): number {
  return (angleBetween(a, b) * 180) / Math.PI;
}

export function midpoint(a: Point2D, b: Point2D): Point2D {
  return { x: (a.x + b.x) / 2, y: (a.y + b.y) / 2 };
}

export function addPoints(a: Point2D, b: Point2D): Point2D {
  return { x: a.x + b.x, y: a.y + b.y };
}

export function subtractPoints(a: Point2D, b: Point2D): Point2D {
  return { x: a.x - b.x, y: a.y - b.y };
}

export function scalePoint(p: Point2D, s: number): Point2D {
  return { x: p.x * s, y: p.y * s };
}

/** Rotate point `p` around `centre` by `radians`. */
export function rotatePoint(p: Point2D, centre: Point2D, radians: number): Point2D {
  const cos = Math.cos(radians);
  const sin = Math.sin(radians);
  const dx = p.x - centre.x;
  const dy = p.y - centre.y;
  return {
    x: centre.x + dx * cos - dy * sin,
    y: centre.y + dx * sin + dy * cos,
  };
}

// ─── Snapping ─────────────────────────────────────────────────────────────────

export function snapToGrid(p: Point2D, gridSize: number): Point2D {
  return {
    x: Math.round(p.x / gridSize) * gridSize,
    y: Math.round(p.y / gridSize) * gridSize,
  };
}

/** Nearest wall endpoint within `threshold`. Returns null if none close enough. */
export function snapToNearestEndpoint(
  p: Point2D,
  walls: Wall[],
  threshold: number,
): { point: Point2D; wallId: string } | null {
  let best: { point: Point2D; wallId: string; dist: number } | null = null;
  for (const w of walls) {
    for (const ep of [w.start, w.end]) {
      const d = distance(p, ep);
      if (d < threshold && (!best || d < best.dist)) {
        best = { point: ep, wallId: w.id, dist: d };
      }
    }
  }
  return best ?? null;
}

/** Nearest wall midpoint within `threshold`. */
export function snapToNearestMidpoint(
  p: Point2D,
  walls: Wall[],
  threshold: number,
): { point: Point2D; wallId: string } | null {
  let best: { point: Point2D; wallId: string; dist: number } | null = null;
  for (const w of walls) {
    const mp = midpoint(w.start, w.end);
    const d = distance(p, mp);
    if (d < threshold && (!best || d < best.dist)) {
      best = { point: mp, wallId: w.id, dist: d };
    }
  }
  return best ?? null;
}

/** Lock `end` so the wall direction snaps to 0 / 45 / 90 / 135 degree multiples. */
export function lockToAngle(start: Point2D, end: Point2D, stepDeg = 45): Point2D {
  const raw = angleBetween(start, end);
  const rawDeg = (raw * 180) / Math.PI;
  const snapped = Math.round(rawDeg / stepDeg) * stepDeg;
  const snappedRad = (snapped * Math.PI) / 180;
  const len = distance(start, end);
  return {
    x: start.x + len * Math.cos(snappedRad),
    y: start.y + len * Math.sin(snappedRad),
  };
}

/** Master snap function — applies all enabled snapping rules. */
export function snapPoint(
  raw: Point2D,
  walls: Wall[],
  config: SnapConfig,
  drawingStart?: Point2D | null,
  shiftHeld = false,
): { point: Point2D; snapType: SnapState['type'] } {
  if (!config.enabled) return { point: raw, snapType: null };

  // 1. Angle snap (highest priority when Shift is held or config.angleSnap)
  if (drawingStart && (shiftHeld || config.angleSnap)) {
    const locked = lockToAngle(drawingStart, raw);
    // After angle-locking, try endpoint/grid snaps on the locked point
    const ep = config.snapToEndpoints
      ? snapToNearestEndpoint(locked, walls, config.snapDistance)
      : null;
    if (ep) return { point: ep.point, snapType: 'endpoint' };
    if (config.snapToGrid) {
      return { point: snapToGrid(locked, config.gridSize), snapType: 'angle' };
    }
    return { point: locked, snapType: 'angle' };
  }

  // 2. Endpoint snap
  if (config.snapToEndpoints) {
    const ep = snapToNearestEndpoint(raw, walls, config.snapDistance);
    if (ep) return { point: ep.point, snapType: 'endpoint' };
  }

  // 3. Midpoint snap
  if (config.snapToMidpoints) {
    const mp = snapToNearestMidpoint(raw, walls, config.snapDistance);
    if (mp) return { point: mp.point, snapType: 'midpoint' };
  }

  // 4. Grid snap
  if (config.snapToGrid) {
    return { point: snapToGrid(raw, config.gridSize), snapType: 'grid' };
  }

  return { point: raw, snapType: null };
}

// ─── Wall geometry ────────────────────────────────────────────────────────────

/** Wall length in design-units. */
export function wallLength(wall: Wall): number {
  return distance(wall.start, wall.end);
}

/** Wall length in metres. */
export function wallLengthMeters(wall: Wall): number {
  return wallLength(wall) / PIXELS_PER_METER;
}

/** Wall angle in radians. */
export function wallAngle(wall: Wall): number {
  return angleBetween(wall.start, wall.end);
}

/** The four corners of the wall rectangle. */
export function wallPolygon(wall: Wall): Point2D[] {
  const angle = wallAngle(wall);
  const half = wall.thickness / 2;
  const dx = Math.sin(angle) * half;
  const dy = Math.cos(angle) * half;
  return [
    { x: wall.start.x - dx, y: wall.start.y + dy },
    { x: wall.end.x   - dx, y: wall.end.y   + dy },
    { x: wall.end.x   + dx, y: wall.end.y   - dy },
    { x: wall.start.x + dx, y: wall.start.y - dy },
  ];
}

/** Point at `ratio` (0–1) along the wall centreline. */
export function pointOnWall(wall: Wall, ratio: number): Point2D {
  return {
    x: wall.start.x + (wall.end.x - wall.start.x) * ratio,
    y: wall.start.y + (wall.end.y - wall.start.y) * ratio,
  };
}

/** Nearest 0–1 ratio on wall centreline from arbitrary point `p`. */
export function wallRatioFromPoint(wall: Wall, p: Point2D): number {
  const dx = wall.end.x - wall.start.x;
  const dy = wall.end.y - wall.start.y;
  const lenSq = dx * dx + dy * dy;
  if (lenSq === 0) return 0;
  const t = ((p.x - wall.start.x) * dx + (p.y - wall.start.y) * dy) / lenSq;
  return Math.max(0, Math.min(1, t));
}

/** Closest point on a wall segment to point `p`. */
export function closestPointOnWall(wall: Wall, p: Point2D): Point2D {
  return pointOnWall(wall, wallRatioFromPoint(wall, p));
}

/** Distance from point `p` to the wall centreline segment. */
export function distanceToWall(wall: Wall, p: Point2D): number {
  return distance(p, closestPointOnWall(wall, p));
}

// ─── Intersection ─────────────────────────────────────────────────────────────

/** Segment-segment intersection. Returns null if parallel or no crossing. */
export function segmentIntersection(
  a1: Point2D, a2: Point2D,
  b1: Point2D, b2: Point2D,
): Point2D | null {
  const r = subtractPoints(a2, a1);
  const s = subtractPoints(b2, b1);
  const rxs = r.x * s.y - r.y * s.x;
  if (Math.abs(rxs) < 1e-10) return null;
  const t = ((b1.x - a1.x) * s.y - (b1.y - a1.y) * s.x) / rxs;
  const u = ((b1.x - a1.x) * r.y - (b1.y - a1.y) * r.x) / rxs;
  if (t >= 0 && t <= 1 && u >= 0 && u <= 1) {
    return { x: a1.x + t * r.x, y: a1.y + t * r.y };
  }
  return null;
}

export function detectWallIntersection(w1: Wall, w2: Wall): Point2D | null {
  return segmentIntersection(w1.start, w1.end, w2.start, w2.end);
}

// ─── Room detection ───────────────────────────────────────────────────────────

/** Are two points "close enough" to be the same node? */
function sameNode(a: Point2D, b: Point2D, tol = 8): boolean {
  return Math.abs(a.x - b.x) <= tol && Math.abs(a.y - b.y) <= tol;
}

function normalise(p: Point2D, nodes: Point2D[], tol: number): number {
  const idx = nodes.findIndex(n => sameNode(n, p, tol));
  if (idx !== -1) return idx;
  nodes.push({ ...p });
  return nodes.length - 1;
}

/**
 * Lightweight room detection.
 * Builds a wall-endpoint graph and finds the shortest closed loops (faces)
 * using a greedy angular-sweep approach (similar to DCEL face extraction).
 */
export function detectRooms(walls: Wall[]): Room[] {
  if (walls.length < 3) return [];

  const TOL = 8;
  const nodes: Point2D[] = [];
  // adjacency: nodeIndex → list of neighbour nodeIndices
  const adj = new Map<number, number[]>();

  const addEdge = (a: number, b: number) => {
    if (!adj.has(a)) adj.set(a, []);
    if (!adj.has(b)) adj.set(b, []);
    const la = adj.get(a)!;
    const lb = adj.get(b)!;
    if (!la.includes(b)) la.push(b);
    if (!lb.includes(a)) lb.push(a);
  };

  for (const w of walls) {
    const si = normalise(w.start, nodes, TOL);
    const ei = normalise(w.end,   nodes, TOL);
    if (si !== ei) addEdge(si, ei);
  }

  const edgeUsed = new Set<string>();
  const rooms: Room[] = [];

  const key = (a: number, b: number) => `${a}_${b}`;

  for (const [startNode, neighbours] of adj.entries()) {
    for (const firstNeighbour of neighbours) {
      const edgeKey = key(startNode, firstNeighbour);
      if (edgeUsed.has(edgeKey)) continue;

      // Walk the loop: always turn as-left-as-possible (smallest left turn)
      const loop: number[] = [startNode, firstNeighbour];
      edgeUsed.add(edgeKey);
      let prev = startNode;
      let cur  = firstNeighbour;

      for (let step = 0; step < nodes.length + 1; step++) {
        const ns = (adj.get(cur) ?? []).filter(n => n !== prev);
        if (ns.length === 0) break;

        // Pick neighbour with smallest counter-clockwise turn from incoming dir
        const inAngle = angleBetween(nodes[prev], nodes[cur]);
        let bestN = ns[0];
        let bestTurn = Infinity;
        for (const n of ns) {
          const outAngle = angleBetween(nodes[cur], nodes[n]);
          let turn = outAngle - inAngle - Math.PI;
          while (turn < -Math.PI) turn += 2 * Math.PI;
          while (turn >  Math.PI) turn -= 2 * Math.PI;
          if (turn < bestTurn) { bestTurn = turn; bestN = n; }
        }

        const nextKey = key(cur, bestN);
        if (edgeUsed.has(nextKey)) {
          // Only register if we closed back to startNode
          if (bestN === startNode && loop.length >= 3) {
            edgeUsed.add(nextKey);
            // Check it's a valid (non-degenerate) polygon
            const poly = loop.map(i => nodes[i]);
            const area = polygonArea(poly);
            if (area > 400) { // min ~20×20 du = ~0.04 m²
              rooms.push({
                id: `room_${Date.now()}_${rooms.length}`,
                name: `Room ${rooms.length + 1}`,
                polygon: poly,
                area: area / (PIXELS_PER_METER * PIXELS_PER_METER),
                color: ROOM_COLORS[rooms.length % ROOM_COLORS.length],
                type: 'room',
              });
            }
          }
          break;
        }

        edgeUsed.add(nextKey);
        loop.push(bestN);
        prev = cur;
        cur  = bestN;

        if (bestN === startNode) {
          if (loop.length >= 3) {
            const poly = loop.map(i => nodes[i]);
            const area = polygonArea(poly);
            if (area > 400) {
              rooms.push({
                id: `room_${Date.now()}_${rooms.length}`,
                name: `Room ${rooms.length + 1}`,
                polygon: poly,
                area: area / (PIXELS_PER_METER * PIXELS_PER_METER),
                color: ROOM_COLORS[rooms.length % ROOM_COLORS.length],
                type: 'room',
              });
            }
          }
          break;
        }
      }
    }
  }

  // Deduplicate by polygon centroid proximity
  const unique: Room[] = [];
  for (const r of rooms) {
    const c = polygonCentroid(r.polygon);
    const dup = unique.some(u => distance(c, polygonCentroid(u.polygon)) < 20);
    if (!dup) unique.push(r);
  }
  return unique;
}

// ─── Polygon maths ────────────────────────────────────────────────────────────

/** Signed area (shoelace). Positive = CCW winding. */
export function signedPolygonArea(polygon: Point2D[]): number {
  let area = 0;
  const n = polygon.length;
  for (let i = 0; i < n; i++) {
    const j = (i + 1) % n;
    area += polygon[i].x * polygon[j].y;
    area -= polygon[j].x * polygon[i].y;
  }
  return area / 2;
}

/** Absolute area in design-units². */
export function polygonArea(polygon: Point2D[]): number {
  return Math.abs(signedPolygonArea(polygon));
}

/** Centroid of a polygon. */
export function polygonCentroid(polygon: Point2D[]): Point2D {
  let cx = 0, cy = 0;
  for (const p of polygon) { cx += p.x; cy += p.y; }
  return { x: cx / polygon.length, y: cy / polygon.length };
}

// ─── Area-based rescale (Total Floor Area override) ────────────────────────────
// A pipeline-detected floor plan's absolute scale comes from the source
// image's pixel dimensions, which has no reliable way to know the real-world
// size of an arbitrary uploaded photo/scan. Letting the user type the true
// total area lets us correct that after the fact by uniformly rescaling the
// plan geometry so its area matches.

/** ft² → m². */
export function sqFtToSqM(ft2: number): number {
  return ft2 * 0.09290304;
}

/** Scale factor that would make `rooms`' total area equal `targetAreaM2`.
 * Returns null if there's no usable current area to scale from (all rooms
 * empty/zero-area, or a non-positive target), so the caller can fall back
 * to "no scaling" instead of dividing by zero or applying a nonsense factor. */
export function areaScaleFactor(rooms: Room[], targetAreaM2: number): number | null {
  const currentM2 = rooms.reduce((sum, r) => sum + (r.area || 0), 0);
  if (!(currentM2 > 0) || !(targetAreaM2 > 0)) return null;
  return Math.sqrt(targetAreaM2 / currentM2);
}

/** Uniformly rescale a floor's wall and room geometry by `factor`. Door and
 * window widths/heights are deliberately left untouched — those are
 * real-world fixed sizes (a standard door is ~0.9 m regardless of whether
 * the building is 100 m² or 10,000 m²), so scaling them by the same factor
 * as a large area correction would make them absurdly oversized. Their
 * `positionOnWall` ratio needs no adjustment either, since it stays
 * correctly proportional automatically once the wall itself is rescaled.
 * Each room's `area` is recomputed from its scaled polygon (via
 * `polygonArea`, converted from design-units² to m²) rather than just
 * multiplied by `factor²`, so it can't compound any rounding/unit
 * assumption baked into whatever the backend originally computed. */
export function scaleFloorGeometry(
  walls: Wall[], rooms: Room[], factor: number,
): { walls: Wall[]; rooms: Room[] } {
  if (!Number.isFinite(factor) || factor <= 0 || factor === 1) {
    return { walls, rooms };
  }
  const scaledWalls = walls.map(w => ({
    ...w,
    start: scalePoint(w.start, factor),
    end: scalePoint(w.end, factor),
    thickness: w.thickness * factor,
  }));
  const scaledRooms = rooms.map(r => {
    const polygon = r.polygon.map(p => scalePoint(p, factor));
    return { ...r, polygon, area: polygonArea(polygon) / (PIXELS_PER_METER ** 2) };
  });
  return { walls: scaledWalls, rooms: scaledRooms };
}

// ─── 3-D helpers ──────────────────────────────────────────────────────────────

export interface Wall3DTransform {
  position: [number, number, number];
  /** Y-axis rotation in radians */
  rotationY: number;
  scale: [number, number, number];
}

/**
 * Convert a 2D wall into a Three.js transform.
 * Origin is centre of the wall box.
 * X-axis = wall length, Y-axis = wall height, Z-axis = wall thickness.
 */
export function wallTo3DTransform(wall: Wall): Wall3DTransform {
  const M = PIXELS_PER_METER;
  const cx = (wall.start.x + wall.end.x) / 2 / M;
  const cz = (wall.start.y + wall.end.y) / 2 / M;
  const len = wallLength(wall) / M;
  const h   = wall.height    / M;
  const t   = wall.thickness / M;
  const angle = wallAngle(wall);
  return {
    position: [cx, h / 2, cz],
    rotationY: -angle,
    scale:    [len, h, t],
  };
}

/** Flat floor polygon for a room (3D, y = 0). */
export function roomFloorPoints3D(room: Room): [number, number, number][] {
  const M = PIXELS_PER_METER;
  return room.polygon.map(p => [p.x / M, 0, p.y / M]);
}

// ─── Display helpers ──────────────────────────────────────────────────────────

/** Format a length in design-units to a human-readable string. */
export function formatLength(du: number, pixelsPerMeter = PIXELS_PER_METER): string {
  const m = du / pixelsPerMeter;
  if (m >= 1) return `${m.toFixed(2)} m`;
  return `${(m * 100).toFixed(0)} cm`;
}

/** Find the wall nearest to point `p`, within `threshold` design-units. */
export function nearestWall(
  p: Point2D,
  walls: Wall[],
  threshold: number,
): Wall | null {
  let best: Wall | null = null;
  let bestD = Infinity;
  for (const w of walls) {
    const d = distanceToWall(w, p);
    if (d < threshold && d < bestD) { bestD = d; best = w; }
  }
  return best;
}

/** Unique ID generator (no external dep). */
let _idCounter = 0;
export function genId(prefix = 'id'): string {
  return `${prefix}_${Date.now()}_${(++_idCounter).toString(36)}`;
}
