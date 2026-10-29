import { NextResponse } from 'next/server';
import fs from 'fs';
import path from 'path';
import crypto from 'crypto';

// Shared, server-side furnishing presets — "set up a room by hand once,
// save it, let it be reused for every user/room of that type" (an
// alternative to depending on the AI planner's per-run variance for
// well-known room types). Deliberately lives here (a plain Next.js API
// route) rather than in either Python backend (api_server.py /
// api_server_mrcnn.py): those two are interchangeable via the UI's own
// "Backend selector" and editor.html can't know which one is active for
// an unrelated feature, but this file is served by the SAME Next.js
// process regardless of that selection — same reasoning /api/agent and
// /api/materials already rely on.
//
// Storage is a single JSON file OUTSIDE public/ (server-side only, never
// directly web-servable) — no database in this project yet, and a flat
// file is enough for what's realistically a short, hand-curated list.
const DATA_DIR = path.join(process.cwd(), 'data');
const PRESETS_FILE = path.join(DATA_DIR, 'furnish-presets.json');

interface PresetItem {
  model: string;        // el.assetUrl — already a locally-servable path for BOTH My Library items and previously-downloaded Sketchfab items, so applying a preset never needs to re-search anything
  sourceLabel: string;  // for display only (e.g. "Sofa Straight 1 (My Library)")
  widthM: number;
  depthM: number;
  heightM: number;
  dxM: number;           // offset from the room's own centroid, in real metres, image-space X
  dyM: number;           // same, image-space Y
  angle: number;         // solver-convention angle (radians) — NOT rotationDeg; see editor.html's _aiFurnRotationDegToSolverAngle for why
}

interface Preset {
  id: string;
  name: string;
  roomType: string;      // matched loosely against a target room's own label, e.g. "reception"
  createdAt: string;
  items: PresetItem[];
}

function readPresets(): Preset[] {
  try {
    const raw = fs.readFileSync(PRESETS_FILE, 'utf-8');
    const data = JSON.parse(raw);
    return Array.isArray(data.presets) ? data.presets : [];
  } catch {
    return [];
  }
}

function writePresets(presets: Preset[]) {
  fs.mkdirSync(DATA_DIR, { recursive: true });
  fs.writeFileSync(PRESETS_FILE, JSON.stringify({ presets }, null, 2), 'utf-8');
}

export async function GET() {
  return NextResponse.json({ presets: readPresets() });
}

export async function POST(request: Request) {
  let body: { name?: string; roomType?: string; items?: PresetItem[] };
  try {
    body = await request.json();
  } catch {
    return NextResponse.json({ error: 'Invalid JSON body' }, { status: 400 });
  }

  const name = (body.name || '').trim();
  const roomType = (body.roomType || '').trim().toLowerCase();
  const items = Array.isArray(body.items) ? body.items : [];
  if (!name) return NextResponse.json({ error: 'name is required' }, { status: 400 });
  if (!items.length) return NextResponse.json({ error: 'No furniture found in that room to save — place some first' }, { status: 400 });

  const preset: Preset = {
    id: crypto.randomUUID(),
    name,
    roomType,
    createdAt: new Date().toISOString(),
    items,
  };

  const presets = readPresets();
  presets.push(preset);
  writePresets(presets);

  return NextResponse.json({ preset });
}

export async function DELETE(request: Request) {
  const { searchParams } = new URL(request.url);
  const id = searchParams.get('id');
  if (!id) return NextResponse.json({ error: 'id is required' }, { status: 400 });

  const presets = readPresets();
  const next = presets.filter(p => p.id !== id);
  if (next.length === presets.length) {
    return NextResponse.json({ error: 'No preset with that id' }, { status: 404 });
  }
  writePresets(next);
  return NextResponse.json({ ok: true });
}
