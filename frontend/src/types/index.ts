// ─── Primitives ───────────────────────────────────────────────────────────────

export interface Point2D {
  x: number;
  y: number;
}

// ─── Editor ───────────────────────────────────────────────────────────────────

export type EditorTool =
  | 'select'
  | 'wall'
  | 'door'
  | 'window'
  | 'furniture'
  | 'pan'
  | 'delete'
  | 'room';

export interface EditorSelection {
  type: 'wall' | 'room' | 'door' | 'window' | 'furniture' | null;
  id: string | null;
}

export interface DrawingState {
  isDrawing: boolean;
  startPoint: Point2D | null;
  currentPoint: Point2D | null;
}

export interface SnapState {
  active: boolean;
  point: Point2D | null;
  type: 'grid' | 'endpoint' | 'midpoint' | 'wall' | 'angle' | null;
}

export interface SnapConfig {
  enabled: boolean;
  gridSize: number;       // design-units per grid cell (100 = 1 m)
  snapDistance: number;   // design-units threshold for proximity snaps
  snapToGrid: boolean;
  snapToEndpoints: boolean;
  snapToMidpoints: boolean;
  angleSnap: boolean;     // lock to 0 / 45 / 90 °
}

export interface ViewState {
  zoom: number;
  offsetX: number;
  offsetY: number;
  mode: '2d' | '3d';
}

// ─── Floor-plan entities ──────────────────────────────────────────────────────

export interface Wall {
  id: string;
  start: Point2D;
  end: Point2D;
  /** Wall thickness in design-units (150 = 15 cm at 100 du/m) */
  thickness: number;
  /** Wall height in design-units (270 = 2.7 m) */
  height: number;
  type: 'wall';
  materialId?: string;
  /** Direct 3-D display colour (hex) – overrides materialId colour */
  color?: string;
  /** 0–1 PBR roughness for the 3-D view */
  roughness?: number;
  /** PolyHaven PBR material applied in the 3-D view */
  polyhavenMaterial?: PolyhavenMaterial;
}

export interface Room {
  id: string;
  name: string;
  /** Ordered vertices of the room outline */
  polygon: Point2D[];
  /** Area in m² */
  area: number;
  /** Display fill colour (hex) */
  color: string;
  type: 'room';
  floorMaterialId?: string;
}

export interface Door {
  id: string;
  wallId: string;
  /** 0–1 position along the wall */
  positionOnWall: number;
  /** mm width */
  width: number;
  /** mm height */
  height: number;
  type: 'door';
  doorType?: 'swing' | 'sliding';
  /** Opening angle in degrees (0 = closed) */
  swingAngle?: number;
}

export interface WindowItem {
  id: string;
  wallId: string;
  positionOnWall: number;
  width: number;
  height: number;
  sillHeight: number;
  type: 'window';
}

export interface FurnitureItem {
  id: string;
  furnitureType: string;
  position: Point2D;
  /** Degrees */
  rotation: number;
  width: number;
  depth: number;
  height: number;
  type: 'furniture';
  label?: string;
  materialId?: string;
}

// ─── Material ─────────────────────────────────────────────────────────────────

export interface Material {
  id: string;
  name: string;
  color: string;
  textureUrl?: string;
  roughness?: number;
  metalness?: number;
}

/** Resolved PBR texture URLs from PolyHaven, stored directly on the entity. */
export interface PolyhavenMaterial {
  /** PolyHaven asset slug, e.g. "brick_wall_003" */
  id: string;
  name: string;
  thumbnailUrl: string;
  diffuseUrl: string;
  roughnessUrl?: string;
  normalUrl?: string;
  resolution: '1k' | '2k';
}

// ─── Floor / Project ──────────────────────────────────────────────────────────

export interface Floor {
  id: string;
  name: string;
  level: number;
  /** Default wall height for this floor in design-units (270 = 2.7 m) */
  floorHeight: number;
  walls: Wall[];
  rooms: Room[];
  doors: Door[];
  windows: WindowItem[];
  furniture: FurnitureItem[];
}

export interface CameraSettings {
  position: [number, number, number];
  target: [number, number, number];
}

export interface Project {
  id: string;
  name: string;
  /** Schema version — bump when data model changes */
  version: string;
  createdAt: string;
  updatedAt: string;
  floors: Floor[];
  materials: Material[];
  activeFloorId: string;
  camera3d: CameraSettings;
  metadata: {
    description?: string;
    author?: string;
    /** Physical units used for display */
    units: 'mm' | 'cm' | 'm' | 'ft';
    /** Design-units per metre (default 100) */
    pixelsPerMeter: number;
  };
}

// ─── Undo / Redo ──────────────────────────────────────────────────────────────

export type HistoryAction =
  | { type: 'ADD_WALL';        wall: Wall;          floorId: string }
  | { type: 'DELETE_WALL';     wall: Wall;          floorId: string }
  | { type: 'UPDATE_WALL';     prev: Wall;   next: Wall;          floorId: string }
  | { type: 'MOVE_WALLS';      prevWalls: Wall[];   nextWalls: Wall[]; floorId: string }
  | { type: 'ADD_DOOR';        door: Door;          floorId: string }
  | { type: 'DELETE_DOOR';     door: Door;          floorId: string }
  | { type: 'ADD_WINDOW';      win: WindowItem;     floorId: string }
  | { type: 'DELETE_WINDOW';   win: WindowItem;     floorId: string }
  | { type: 'ADD_FURNITURE';   item: FurnitureItem; floorId: string }
  | { type: 'DELETE_FURNITURE';item: FurnitureItem; floorId: string }
  | { type: 'UPDATE_FURNITURE';prev: FurnitureItem; next: FurnitureItem; floorId: string }
  | { type: 'RENAME_ROOM';     roomId: string; prevName: string; nextName: string; floorId: string };
