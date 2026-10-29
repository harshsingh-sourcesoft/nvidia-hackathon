import { create } from 'zustand';
import { immer } from 'zustand/middleware/immer';
import type {
  Project, Floor, Wall, Room, Door, WindowItem, FurnitureItem, Material,
  EditorTool, EditorSelection, DrawingState, SnapState, SnapConfig,
  ViewState, HistoryAction, Point2D,
} from '@/types';
import {
  genId, detectRooms, MIN_WALL_LENGTH, distance,
} from '@/utils/geometry';

// ─── Defaults ─────────────────────────────────────────────────────────────────

const DEFAULT_FLOOR: Floor = {
  id: 'floor_1',
  name: 'Ground Floor',
  level: 0,
  floorHeight: 270,
  walls: [],
  rooms: [],
  doors: [],
  windows: [],
  furniture: [],
};

const DEFAULT_PROJECT: Project = {
  id: genId('project'),
  name: 'Untitled Project',
  version: '1.0.0',
  createdAt: new Date().toISOString(),
  updatedAt: new Date().toISOString(),
  floors: [DEFAULT_FLOOR],
  materials: [],
  activeFloorId: 'floor_1',
  camera3d: { position: [10, 8, 10], target: [0, 0, 0] },
  metadata: { units: 'm', pixelsPerMeter: 100 },
};

const DEFAULT_SNAP: SnapConfig = {
  enabled: true,
  gridSize: 25,        // 25 du = 25 cm
  snapDistance: 15,    // 15 du proximity threshold
  snapToGrid: true,
  snapToEndpoints: true,
  snapToMidpoints: true,
  angleSnap: false,
};

// ─── Store interface ───────────────────────────────────────────────────────────

export interface EditorStore {
  // ── State ─────────────────────────────────────
  project:    Project;
  tool:       EditorTool;
  selection:  EditorSelection;
  view:       ViewState;
  drawing:    DrawingState;
  snap:       SnapState;
  snapConfig: SnapConfig;
  history:    HistoryAction[];
  historyIndex: number;
  showDebug:  boolean;

  // ── Derived ───────────────────────────────────
  activeFloor: () => Floor;
  selectedWall: () => Wall | null;
  selectedRoom: () => Room | null;
  selectedDoor: () => Door | null;
  selectedWindow: () => WindowItem | null;
  selectedFurniture: () => FurnitureItem | null;

  // ── Tool ──────────────────────────────────────
  setTool: (tool: EditorTool) => void;

  // ── View ──────────────────────────────────────
  setViewMode: (mode: '2d' | '3d') => void;
  setZoom:     (zoom: number) => void;
  setOffset:   (x: number, y: number) => void;

  // ── Drawing ───────────────────────────────────
  startWallDraw:    (point: Point2D) => void;
  updateWallPreview:(point: Point2D) => void;
  finalizeWall:     (endPoint: Point2D) => void;
  cancelWallDraw:   () => void;

  // ── Wall CRUD ─────────────────────────────────
  addWall:    (wall: Wall) => void;
  updateWall: (id: string, updates: Partial<Omit<Wall,'id'|'type'>>) => void;
  deleteWall: (id: string) => void;
  moveWall:   (id: string, dx: number, dy: number) => void;
  importPipelineWalls: (walls: Omit<Wall, 'type'>[], doors?: Door[], windows?: WindowItem[], rooms?: Room[]) => void;
  importPipelineWallsToFloor: (floorId: string, walls: Omit<Wall, 'type'>[], doors?: Door[], windows?: WindowItem[], rooms?: Room[]) => void;

  // ── Floor management ──────────────────────────
  setActiveFloor: (id: string) => void;
  addFloor: (floor: Floor) => void;
  updateFloor: (id: string, updates: Partial<Pick<Floor, 'name' | 'level' | 'floorHeight'>>) => void;

  // ── Door CRUD ─────────────────────────────────
  addDoor:    (door: Door) => void;
  updateDoor: (id: string, updates: Partial<Omit<Door,'id'|'type'>>) => void;
  deleteDoor: (id: string) => void;

  // ── Window CRUD ───────────────────────────────
  addWindow:    (win: WindowItem) => void;
  updateWindow: (id: string, updates: Partial<Omit<WindowItem,'id'|'type'>>) => void;
  deleteWindow: (id: string) => void;

  // ── Furniture CRUD ────────────────────────────
  addFurniture:    (item: FurnitureItem) => void;
  updateFurniture: (id: string, updates: Partial<Omit<FurnitureItem,'id'|'type'>>) => void;
  deleteFurniture: (id: string) => void;

  // ── Room ──────────────────────────────────────
  runRoomDetection: () => void;
  updateRoom:       (id: string, updates: Partial<Omit<Room,'id'|'type'>>) => void;

  // ── Selection ─────────────────────────────────
  select:        (type: EditorSelection['type'], id: string) => void;
  deselect:      () => void;
  deleteSelected:() => void;

  // ── Snap ──────────────────────────────────────
  updateSnap:      (point: Point2D | null, type: SnapState['type']) => void;
  setSnapConfig:   (cfg: Partial<SnapConfig>) => void;

  // ── History ───────────────────────────────────
  pushHistory: (action: HistoryAction) => void;
  undo:        () => void;
  redo:        () => void;

  // ── Project meta ──────────────────────────────
  setProjectName: (name: string) => void;

  // ── Persistence ───────────────────────────────
  saveToLocalStorage:   () => void;
  loadFromLocalStorage: () => void;
  exportJSON:           () => string;
  importJSON:           (json: string) => void;
  newProject:           () => void;

  // ── Debug ─────────────────────────────────────
  toggleDebug: () => void;

  // Imported GLB (transient – not persisted)
  importedGlbUrl: string | null;
  setImportedGlbUrl: (url: string | null) => void;

  // 2D background trace image (transient, per-floor keyed by floorId)
  floorBgImages: Record<string, string>;
  bgImageDataUrl: string | null;
  bgImageOpacity: number;
  setBgImage: (url: string | null) => void;
  setBgImageOpacity: (v: number) => void;
  setFloorBgImage: (floorId: string, url: string | null) => void;
}

// ─── Store implementation ─────────────────────────────────────────────────────

export const useEditorStore = create<EditorStore>()(
  immer((set, get) => {

    // ── Helpers ─────────────────────────────────

    const getFloor = (draft: { project: Project }): Floor => {
      const f = draft.project.floors.find(
        fl => fl.id === draft.project.activeFloorId,
      );
      if (!f) throw new Error('Active floor not found');
      return f;
    };

    const touch = (draft: { project: Project }) => {
      draft.project.updatedAt = new Date().toISOString();
    };

    const pushHistory = (draft: EditorStore, action: HistoryAction) => {
      // Truncate forward history
      draft.history = draft.history.slice(0, draft.historyIndex + 1);
      draft.history.push(action);
      draft.historyIndex = draft.history.length - 1;
    };

    return {
      // ── Initial state ──────────────────────────
      project:      DEFAULT_PROJECT,
      tool:         'select',
      selection:    { type: null, id: null },
      view:         { zoom: 1, offsetX: 0, offsetY: 0, mode: '2d' },
      drawing:      { isDrawing: false, startPoint: null, currentPoint: null },
      snap:         { active: false, point: null, type: null },
      snapConfig:   DEFAULT_SNAP,
      history:      [],
      historyIndex: -1,
      showDebug:    false,

      // ── Derived ────────────────────────────────
      activeFloor: () => {
        const { project } = get();
        return project.floors.find(f => f.id === project.activeFloorId)
          ?? project.floors[0];
      },

      selectedWall: () => {
        const { selection } = get();
        if (selection.type !== 'wall' || !selection.id) return null;
        return get().activeFloor().walls.find(w => w.id === selection.id) ?? null;
      },

      selectedRoom: () => {
        const { selection } = get();
        if (selection.type !== 'room' || !selection.id) return null;
        return get().activeFloor().rooms.find(r => r.id === selection.id) ?? null;
      },

      selectedDoor: () => {
        const { selection } = get();
        if (selection.type !== 'door' || !selection.id) return null;
        return get().activeFloor().doors.find(d => d.id === selection.id) ?? null;
      },

      selectedWindow: () => {
        const { selection } = get();
        if (selection.type !== 'window' || !selection.id) return null;
        return get().activeFloor().windows.find(w => w.id === selection.id) ?? null;
      },

      selectedFurniture: () => {
        const { selection } = get();
        if (selection.type !== 'furniture' || !selection.id) return null;
        return get().activeFloor().furniture.find(f => f.id === selection.id) ?? null;
      },

      // ── Tool ───────────────────────────────────
      setTool: tool => set(s => {
        s.tool = tool;
        s.drawing = { isDrawing: false, startPoint: null, currentPoint: null };
        if (tool !== 'select') s.selection = { type: null, id: null };
      }),

      // ── View ───────────────────────────────────
      setViewMode: mode => set(s => { s.view.mode = mode; }),
      setZoom:     zoom => set(s => { s.view.zoom   = zoom; }),
      setOffset:   (x, y) => set(s => { s.view.offsetX = x; s.view.offsetY = y; }),

      // ── Drawing ────────────────────────────────
      startWallDraw: point => set(s => {
        s.drawing = { isDrawing: true, startPoint: point, currentPoint: point };
      }),

      updateWallPreview: point => set(s => {
        s.drawing.currentPoint = point;
      }),

      finalizeWall: endPoint => set(s => {
        const { drawing } = s;
        if (!drawing.isDrawing || !drawing.startPoint) return;

        const len = distance(drawing.startPoint, endPoint);
        if (len < MIN_WALL_LENGTH) return; // ignore accidental tiny walls

        const floor = getFloor(s);
        const wall: Wall = {
          id: genId('wall'),
          start: { ...drawing.startPoint },
          end:   { ...endPoint },
          thickness: 15,
          height:    floor.floorHeight ?? 270,
          type:      'wall',
        };

        floor.walls.push(wall);
        touch(s);
        pushHistory(s, { type: 'ADD_WALL', wall, floorId: s.project.activeFloorId });

        // Continue drawing from the new endpoint
        s.drawing.startPoint  = { ...endPoint };
        s.drawing.currentPoint = { ...endPoint };
      }),

      cancelWallDraw: () => set(s => {
        s.drawing = { isDrawing: false, startPoint: null, currentPoint: null };
      }),

      // ── Wall CRUD ──────────────────────────────
      addWall: wall => set(s => {
        getFloor(s).walls.push(wall);
        touch(s);
        pushHistory(s, { type: 'ADD_WALL', wall, floorId: s.project.activeFloorId });
      }),

      updateWall: (id, updates) => set(s => {
        const floor = getFloor(s);
        const idx   = floor.walls.findIndex(w => w.id === id);
        if (idx === -1) return;
        const prev = { ...floor.walls[idx] };
        Object.assign(floor.walls[idx], updates);
        const next = { ...floor.walls[idx] };
        touch(s);
        pushHistory(s, { type: 'UPDATE_WALL', prev, next, floorId: s.project.activeFloorId });
      }),

      deleteWall: id => set(s => {
        const floor = getFloor(s);
        const wall  = floor.walls.find(w => w.id === id);
        if (!wall) return;
        floor.walls = floor.walls.filter(w => w.id !== id);
        // Remove orphan doors/windows on this wall
        floor.doors   = floor.doors.filter(d => d.wallId !== id);
        floor.windows = floor.windows.filter(w => w.wallId !== id);
        touch(s);
        pushHistory(s, { type: 'DELETE_WALL', wall, floorId: s.project.activeFloorId });
        if (s.selection.id === id) s.selection = { type: null, id: null };
      }),

      moveWall: (id, dx, dy) => set(s => {
        const floor = getFloor(s);
        const wall  = floor.walls.find(w => w.id === id);
        if (!wall) return;

        // Snap original endpoint positions before any mutation
        const TOL = 15;
        const os  = { x: wall.start.x, y: wall.start.y };
        const oe  = { x: wall.end.x,   y: wall.end.y   };

        // Capture before-state of all walls that will be affected
        const prevWalls: Wall[] = [];
        const affectedIds = new Set<string>();
        for (const w of floor.walls) {
          const sNearOs = Math.abs(w.start.x - os.x) < TOL && Math.abs(w.start.y - os.y) < TOL;
          const sNearOe = Math.abs(w.start.x - oe.x) < TOL && Math.abs(w.start.y - oe.y) < TOL;
          const eNearOs = Math.abs(w.end.x   - os.x) < TOL && Math.abs(w.end.y   - os.y) < TOL;
          const eNearOe = Math.abs(w.end.x   - oe.x) < TOL && Math.abs(w.end.y   - oe.y) < TOL;
          if (sNearOs || sNearOe || eNearOs || eNearOe) {
            prevWalls.push({ ...w, start: { ...w.start }, end: { ...w.end } });
            affectedIds.add(w.id);
          }
        }

        // Apply rubber-band movement — compare against prevWalls snapshots to avoid
        // double-moving an endpoint that was already shifted by a prior comparison.
        for (const pw of prevWalls) {
          const w = floor.walls.find(fw => fw.id === pw.id);
          if (!w) continue;
          if ((Math.abs(pw.start.x - os.x) < TOL && Math.abs(pw.start.y - os.y) < TOL) ||
              (Math.abs(pw.start.x - oe.x) < TOL && Math.abs(pw.start.y - oe.y) < TOL)) {
            w.start.x += dx; w.start.y += dy;
          }
          if ((Math.abs(pw.end.x - os.x) < TOL && Math.abs(pw.end.y - os.y) < TOL) ||
              (Math.abs(pw.end.x - oe.x) < TOL && Math.abs(pw.end.y - oe.y) < TOL)) {
            w.end.x += dx; w.end.y += dy;
          }
        }

        const nextWalls = prevWalls.map(pw => {
          const nw = floor.walls.find(w => w.id === pw.id);
          return nw ? { ...nw, start: { ...nw.start }, end: { ...nw.end } } : pw;
        });

        touch(s);
        pushHistory(s, { type: 'MOVE_WALLS', prevWalls, nextWalls, floorId: s.project.activeFloorId });
      }),

      importPipelineWalls: (rawWalls, rawDoors = [], rawWindows = [], rawRooms = []) => set(s => {
        const floor = getFloor(s);
        floor.walls   = rawWalls.map(w => ({ ...w, type: 'wall' as const }));
        floor.doors   = rawDoors;
        floor.windows = rawWindows;
        floor.rooms   = rawRooms.map(r => ({ ...r, type: 'room' as const })) as Room[];
        s.drawing     = { isDrawing: false, startPoint: null, currentPoint: null };
        s.selection   = { type: null, id: null };
        touch(s);
      }),

      importPipelineWallsToFloor: (floorId, rawWalls, rawDoors = [], rawWindows = [], rawRooms = []) => set(s => {
        const floor = s.project.floors.find(f => f.id === floorId);
        if (!floor) return;
        floor.walls   = rawWalls.map(w => ({ ...w, type: 'wall' as const }));
        floor.doors   = rawDoors;
        floor.windows = rawWindows;
        floor.rooms   = rawRooms.map(r => ({ ...r, type: 'room' as const })) as Room[];
        s.drawing     = { isDrawing: false, startPoint: null, currentPoint: null };
        s.selection   = { type: null, id: null };
        touch(s);
      }),

      // ── Floor management ───────────────────────
      setActiveFloor: (id) => set(s => {
        s.project.activeFloorId = id;
        s.bgImageDataUrl = s.floorBgImages[id] ?? null;
        s.selection = { type: null, id: null };
        s.drawing   = { isDrawing: false, startPoint: null, currentPoint: null };
      }),

      addFloor: (floor) => set(s => {
        s.project.floors.push(floor);
        touch(s);
      }),

      updateFloor: (id, updates) => set(s => {
        const f = s.project.floors.find(fl => fl.id === id);
        if (f) { Object.assign(f, updates); touch(s); }
      }),

      // ── Door CRUD ──────────────────────────────
      addDoor: door => set(s => {
        getFloor(s).doors.push(door);
        touch(s);
        pushHistory(s, { type: 'ADD_DOOR', door, floorId: s.project.activeFloorId });
      }),

      updateDoor: (id, updates) => set(s => {
        const d = getFloor(s).doors.find(x => x.id === id);
        if (d) { Object.assign(d, updates); touch(s); }
      }),

      deleteDoor: id => set(s => {
        const floor = getFloor(s);
        const door  = floor.doors.find(d => d.id === id);
        if (!door) return;
        floor.doors = floor.doors.filter(d => d.id !== id);
        touch(s);
        pushHistory(s, { type: 'DELETE_DOOR', door, floorId: s.project.activeFloorId });
        if (s.selection.id === id) s.selection = { type: null, id: null };
      }),

      // ── Window CRUD ────────────────────────────
      addWindow: win => set(s => {
        getFloor(s).windows.push(win);
        touch(s);
        pushHistory(s, { type: 'ADD_WINDOW', win, floorId: s.project.activeFloorId });
      }),

      updateWindow: (id, updates) => set(s => {
        const w = getFloor(s).windows.find(x => x.id === id);
        if (w) { Object.assign(w, updates); touch(s); }
      }),

      deleteWindow: id => set(s => {
        const floor = getFloor(s);
        const win   = floor.windows.find(w => w.id === id);
        if (!win) return;
        floor.windows = floor.windows.filter(w => w.id !== id);
        touch(s);
        pushHistory(s, { type: 'DELETE_WINDOW', win, floorId: s.project.activeFloorId });
        if (s.selection.id === id) s.selection = { type: null, id: null };
      }),

      // ── Furniture CRUD ─────────────────────────
      addFurniture: item => set(s => {
        getFloor(s).furniture.push(item);
        touch(s);
        pushHistory(s, { type: 'ADD_FURNITURE', item, floorId: s.project.activeFloorId });
      }),

      updateFurniture: (id, updates) => set(s => {
        const floor = getFloor(s);
        const idx   = floor.furniture.findIndex(f => f.id === id);
        if (idx === -1) return;
        const prev = { ...floor.furniture[idx] };
        Object.assign(floor.furniture[idx], updates);
        const next = { ...floor.furniture[idx] };
        touch(s);
        pushHistory(s, { type: 'UPDATE_FURNITURE', prev, next, floorId: s.project.activeFloorId });
      }),

      deleteFurniture: id => set(s => {
        const floor = getFloor(s);
        const item  = floor.furniture.find(f => f.id === id);
        if (!item) return;
        floor.furniture = floor.furniture.filter(f => f.id !== id);
        touch(s);
        pushHistory(s, { type: 'DELETE_FURNITURE', item, floorId: s.project.activeFloorId });
        if (s.selection.id === id) s.selection = { type: null, id: null };
      }),

      // ── Room ───────────────────────────────────
      runRoomDetection: () => set(s => {
        const floor   = getFloor(s);
        const detected = detectRooms(floor.walls);
        // Preserve user-renamed rooms if IDs match
        const renamed = new Map(floor.rooms.map(r => [r.id, r.name]));
        floor.rooms = detected.map(r => ({
          ...r,
          name: renamed.get(r.id) ?? r.name,
        }));
        touch(s);
      }),

      updateRoom: (id, updates) => set(s => {
        const r = getFloor(s).rooms.find(x => x.id === id);
        if (!r) return;
        const prevName = r.name;
        Object.assign(r, updates);
        touch(s);
        if (updates.name !== undefined) {
          pushHistory(s, {
            type: 'RENAME_ROOM', roomId: id,
            prevName, nextName: updates.name,
            floorId: s.project.activeFloorId,
          });
        }
      }),

      // ── Selection ──────────────────────────────
      select: (type, id) => set(s => {
        s.selection = { type, id };
      }),

      deselect: () => set(s => {
        s.selection = { type: null, id: null };
      }),

      deleteSelected: () => {
        const { selection } = get();
        if (!selection.id) return;
        switch (selection.type) {
          case 'wall':      get().deleteWall(selection.id);      break;
          case 'door':      get().deleteDoor(selection.id);      break;
          case 'window':    get().deleteWindow(selection.id);    break;
          case 'furniture': get().deleteFurniture(selection.id); break;
        }
      },

      // ── Snap ───────────────────────────────────
      updateSnap: (point, type) => set(s => {
        s.snap = { active: point !== null, point, type };
      }),

      setSnapConfig: cfg => set(s => {
        Object.assign(s.snapConfig, cfg);
      }),

      // ── History ────────────────────────────────
      pushHistory: action => set(s => { pushHistory(s, action); }),

      undo: () => set(s => {
        if (s.historyIndex < 0) return;
        const action = s.history[s.historyIndex];
        s.historyIndex--;
        const floor = s.project.floors.find(f => f.id === action.floorId);
        if (!floor) return;
        switch (action.type) {
          case 'ADD_WALL':
            floor.walls = floor.walls.filter(w => w.id !== action.wall.id); break;
          case 'DELETE_WALL':
            floor.walls.push(action.wall); break;
          case 'UPDATE_WALL': {
            const idx = floor.walls.findIndex(w => w.id === action.prev.id);
            if (idx !== -1) floor.walls[idx] = action.prev;
            break;
          }
          case 'MOVE_WALLS': {
            for (const pw of action.prevWalls) {
              const idx = floor.walls.findIndex(w => w.id === pw.id);
              if (idx !== -1) floor.walls[idx] = pw;
            }
            break;
          }
          case 'ADD_DOOR':
            floor.doors = floor.doors.filter(d => d.id !== action.door.id); break;
          case 'DELETE_DOOR':
            floor.doors.push(action.door); break;
          case 'ADD_WINDOW':
            floor.windows = floor.windows.filter(w => w.id !== action.win.id); break;
          case 'DELETE_WINDOW':
            floor.windows.push(action.win); break;
          case 'ADD_FURNITURE':
            floor.furniture = floor.furniture.filter(f => f.id !== action.item.id); break;
          case 'DELETE_FURNITURE':
            floor.furniture.push(action.item); break;
          case 'UPDATE_FURNITURE': {
            const idx = floor.furniture.findIndex(f => f.id === action.prev.id);
            if (idx !== -1) floor.furniture[idx] = action.prev;
            break;
          }
          case 'RENAME_ROOM': {
            const r = floor.rooms.find(x => x.id === action.roomId);
            if (r) r.name = action.prevName;
            break;
          }
        }
      }),

      redo: () => set(s => {
        if (s.historyIndex >= s.history.length - 1) return;
        s.historyIndex++;
        const action = s.history[s.historyIndex];
        const floor  = s.project.floors.find(f => f.id === action.floorId);
        if (!floor) return;
        switch (action.type) {
          case 'ADD_WALL':
            floor.walls.push(action.wall); break;
          case 'DELETE_WALL':
            floor.walls = floor.walls.filter(w => w.id !== action.wall.id); break;
          case 'UPDATE_WALL': {
            const idx = floor.walls.findIndex(w => w.id === action.next.id);
            if (idx !== -1) floor.walls[idx] = action.next;
            break;
          }
          case 'MOVE_WALLS': {
            for (const nw of action.nextWalls) {
              const idx = floor.walls.findIndex(w => w.id === nw.id);
              if (idx !== -1) floor.walls[idx] = nw;
            }
            break;
          }
          case 'ADD_DOOR':
            floor.doors.push(action.door); break;
          case 'DELETE_DOOR':
            floor.doors = floor.doors.filter(d => d.id !== action.door.id); break;
          case 'ADD_WINDOW':
            floor.windows.push(action.win); break;
          case 'DELETE_WINDOW':
            floor.windows = floor.windows.filter(w => w.id !== action.win.id); break;
          case 'ADD_FURNITURE':
            floor.furniture.push(action.item); break;
          case 'DELETE_FURNITURE':
            floor.furniture = floor.furniture.filter(f => f.id !== action.item.id); break;
          case 'UPDATE_FURNITURE': {
            const idx = floor.furniture.findIndex(f => f.id === action.next.id);
            if (idx !== -1) floor.furniture[idx] = action.next;
            break;
          }
          case 'RENAME_ROOM': {
            const r = floor.rooms.find(x => x.id === action.roomId);
            if (r) r.name = action.nextName;
            break;
          }
        }
      }),

      // ── Project meta ───────────────────────────
      setProjectName: name => set(s => {
        s.project.name = name; touch(s);
      }),

      // ── Persistence ────────────────────────────
      saveToLocalStorage: () => {
        try {
          localStorage.setItem('planlift_project', JSON.stringify(get().project));
        } catch { /* ignore quota errors */ }
      },

      loadFromLocalStorage: () => {
        try {
          const raw = localStorage.getItem('planlift_project');
          if (raw) get().importJSON(raw);
        } catch { /* ignore */ }
      },

      exportJSON: () => JSON.stringify(get().project, null, 2),

      importJSON: json => {
        try {
          const data = JSON.parse(json) as Project;
          // Basic validation
          if (!data.floors || !data.version) throw new Error('Invalid project JSON');
          set(s => {
            s.project      = data;
            s.selection    = { type: null, id: null };
            s.drawing      = { isDrawing: false, startPoint: null, currentPoint: null };
            s.history      = [];
            s.historyIndex = -1;
          });
        } catch (e) {
          console.error('[importJSON]', e);
        }
      },

      newProject: () => set(s => {
        const fresh = {
          ...DEFAULT_PROJECT,
          id:        genId('project'),
          createdAt: new Date().toISOString(),
          updatedAt: new Date().toISOString(),
          floors:    [{ ...DEFAULT_FLOOR, id: genId('floor') }],
        };
        s.project      = fresh;
        s.project.activeFloorId = fresh.floors[0].id;
        s.selection    = { type: null, id: null };
        s.drawing      = { isDrawing: false, startPoint: null, currentPoint: null };
        s.history      = [];
        s.historyIndex = -1;
      }),

      // ── Debug ──────────────────────────────────
      toggleDebug: () => set(s => { s.showDebug = !s.showDebug; }),

      importedGlbUrl: null,
      setImportedGlbUrl: (url: string | null) => set(s => { s.importedGlbUrl = url; }),

      floorBgImages: {},
      bgImageDataUrl: null,
      bgImageOpacity: 0.3,
      setBgImage: (url: string | null) => set(s => {
        s.bgImageDataUrl = url;
        const id = s.project.activeFloorId;
        if (url) s.floorBgImages[id] = url;
        else delete s.floorBgImages[id];
      }),
      setBgImageOpacity: (v: number) => set(s => { s.bgImageOpacity = v; }),
      setFloorBgImage: (floorId, url) => set(s => {
        if (url) s.floorBgImages[floorId] = url;
        else delete s.floorBgImages[floorId];
        if (floorId === s.project.activeFloorId) s.bgImageDataUrl = url;
      }),
    };
  }),
);
