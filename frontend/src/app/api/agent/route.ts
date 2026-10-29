import { NextResponse } from 'next/server';
import fs from 'fs';
import path from 'path';

// Switched from a local Ollama model to NVIDIA Nemotron via Nebius Token
// Factory — same model family already powering the web AI Furniture Agent
// (api_server.py's /agent/arrange-furniture). The old Ollama call is kept
// below, commented out, for rollback. Nothing else in this file changed:
// buildSystemPrompt (the actual intelligence — add vs furnish, room
// matching, __ALL_ROOMS__) and the response post-processing are untouched,
// so the Kit-side AI Agent panel (extension.py's _call_agent_api, the
// ONLY caller of this route — confirmed editor.html never calls it) needs
// zero changes; it gets the exact same {action, entity_kind/room_type,
// room, attributes} shape back, just reasoned by a different model.
const NEBIUS_BASE_URL = 'https://api.tokenfactory.nebius.com/v1';
const NEMOTRON_MODEL = 'nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B';

// const OLLAMA_URL = 'http://localhost:11434/api/chat';
// const MODEL = 'qwen2.5:7b';

let _nebiusApiKeyCache: string | null = null;
function readNebiusApiKey(): string {
  if (_nebiusApiKeyCache) return _nebiusApiKeyCache;
  if (process.env.NEBIUS_API_KEY) {
    _nebiusApiKeyCache = process.env.NEBIUS_API_KEY;
    return _nebiusApiKeyCache;
  }
  // Same PlanLift-Backend/.env api_server.py's _read_env_var already reads
  // NEBIUS_API_KEY from — single source of truth for the secret, never
  // duplicated into app/'s own env files. Frontend and backend are now
  // sibling folders (not nested) — this was broken by that move until
  // fixed here, since process.cwd()/../PlanLift/.env no longer resolved
  // to anything real.
  try {
    const envPath = path.join(process.cwd(), '..', 'PlanLift-Backend', '.env');
    const text = fs.readFileSync(envPath, 'utf-8');
    for (const line of text.split('\n')) {
      const trimmed = line.trim();
      if (!trimmed || trimmed.startsWith('#') || !trimmed.includes('=')) continue;
      const eq = trimmed.indexOf('=');
      const k = trimmed.slice(0, eq).trim();
      if (k === 'NEBIUS_API_KEY') {
        _nebiusApiKeyCache = trimmed.slice(eq + 1).trim().replace(/^["']|["']$/g, '');
        return _nebiusApiKeyCache;
      }
    }
  } catch {
    // fall through — empty key handled by the caller
  }
  return '';
}

// 'light' and 'camera' are built-in procedural skills implemented directly
// in the Kit-side AI Agent panel (extension.py). Any other entity_kind is
// resolved there against the local furniture library
// (public/models/library/manifest.json) by fuzzy name match — this route
// mirrors that same fuzzy match purely so the "supported" flag it returns
// is informative; Kit is the actual authority on whether an item can be
// placed (it also handles catalog entries this list might miss).
const BUILTIN_SUPPORTED_KINDS = ['light', 'camera'];

let _furnitureNamesCache: string[] | null = null;
function furnitureNames(): string[] {
  if (_furnitureNamesCache) return _furnitureNamesCache;
  const names: string[] = [];
  try {
    const manifestPath = path.join(process.cwd(), 'public', 'models', 'library', 'manifest.json');
    const manifest = JSON.parse(fs.readFileSync(manifestPath, 'utf-8'));
    const walk = (node: any) => {
      if (node?.asset?.elementType === 'furniture' && node?.name) {
        names.push(String(node.name).toLowerCase());
      }
      for (const child of node?.children || []) walk(child);
    };
    walk(manifest);
  } catch {
    // manifest missing/unreadable — furniture "supported" detection just
    // degrades to false; Kit-side resolution is unaffected either way.
  }
  _furnitureNamesCache = names;
  return names;
}

function isSupportedKind(kind: string): boolean {
  if (BUILTIN_SUPPORTED_KINDS.includes(kind)) return true;
  const needle = kind.toLowerCase();
  return furnitureNames().some(n => n.includes(needle) || needle.includes(n));
}

interface RoomInfo {
  name: string;
}

function buildSystemPrompt(rooms: RoomInfo[]): string {
  const roomList = rooms.length
    ? rooms.map(r => `"${r.name}"`).join(', ')
    : '(no rooms have been named yet)';

  return `You are a floor-plan assistant. Convert the user's one-sentence request into a single JSON object describing what to add and where.

Rooms currently labeled in this floor plan: ${roomList}.

There are TWO possible actions:

1. "add" — the user names ONE specific object to place.
{"action":"add","entity_kind":"<lowercase noun, e.g. light, sofa, camera, lamp>","room":"<the room name from the list above that best matches what the user said, or your best guess if none match>","attributes":{}}

2. "furnish" — the user describes a ROOM'S PURPOSE/TYPE and wants it populated with a whole matching set of furniture, not one named object (e.g. "furnish this as a reception", "add reception furniture", "set this room up as a waiting area", "make this look like an office", "add the things a reception area uses").
{"action":"furnish","room_type":"<lowercase noun for the room's purpose, e.g. reception, office, living room, bedroom, waiting area>","room":"<the room name from the list above that best matches what the user said>","attributes":{}}

Respond with ONLY ONE JSON object matching one of the two shapes above, no other text.

Rules:
- Use "furnish" whenever the request describes a room TYPE/PURPOSE and implies multiple/unspecified items, not one named object. Use "add" whenever a specific object is named (sofa, light, camera, desk, etc.), even if it's for a themed room (e.g. "add a desk in the reception" is still "add", not "furnish").
- entity_kind/room_type should be the simplest lowercase noun (e.g. "light" not "ceiling light fixture"; "reception" not "reception desk area").
- room must be your best match to one of the labeled rooms above, copied exactly as listed, even if the user's wording differs (e.g. user says "bedroom", list has "Master Bedroom" -> use "Master Bedroom").
- If an "add" request applies to EVERY labeled room at once (e.g. "add a camera in all rooms", "put a light in every room", "add a light in each room") instead of one specific room, set room to exactly "__ALL_ROOMS__" (that literal string) — do not pick just one room from the list in that case. "furnish" never uses "__ALL_ROOMS__" — it always targets one room.
- attributes may include a "spec" key with any short descriptive detail from the request that isn't the object/room-type or room itself (resolution, color, size, brand) — omit the key entirely if there's nothing like that.
- If the request doesn't clearly mention adding/furnishing anything, still return your best-guess "add" JSON — never return prose.

Examples:
User: "add a light in the bedroom"
{"action":"add","entity_kind":"light","room":"Bedroom","attributes":{}}

User: "put a lamp in the kitchen"
{"action":"add","entity_kind":"lamp","room":"Kitchen","attributes":{}}

User: "add a sofa in the living room"
{"action":"add","entity_kind":"sofa","room":"Living Room","attributes":{}}

User: "add the 4 mp camera in garage"
{"action":"add","entity_kind":"camera","room":"Garage","attributes":{"spec":"4 mp"}}

User: "add a camera in all rooms"
{"action":"add","entity_kind":"camera","room":"__ALL_ROOMS__","attributes":{}}

User: "put a light in every room"
{"action":"add","entity_kind":"light","room":"__ALL_ROOMS__","attributes":{}}

User: "furnish the reception area"
{"action":"furnish","room_type":"reception","room":"Reception","attributes":{}}

User: "add the objects a reception uses in the front room"
{"action":"furnish","room_type":"reception","room":"Front Room","attributes":{}}

User: "make the lounge look like a proper living room"
{"action":"furnish","room_type":"living room","room":"Lounge","attributes":{}}

User: "set up the study as an office"
{"action":"furnish","room_type":"office","room":"Study","attributes":{}}

User: "furnish the warehouse"
{"action":"furnish","room_type":"warehouse","room":"Warehouse","attributes":{}}`;
}

export async function POST(request: Request) {
  let body: { prompt?: string; rooms?: RoomInfo[] };
  try {
    body = await request.json();
  } catch {
    return NextResponse.json({ error: 'Invalid JSON body' }, { status: 400 });
  }

  const prompt = (body.prompt || '').trim();
  const rooms = Array.isArray(body.rooms) ? body.rooms : [];
  if (!prompt) {
    return NextResponse.json({ error: 'prompt is required' }, { status: 400 });
  }

  const apiKey = readNebiusApiKey();
  if (!apiKey) {
    return NextResponse.json(
      { error: 'NEBIUS_API_KEY not set in PlanLift/.env — get one at https://tokenfactory.nebius.com/' },
      { status: 502 },
    );
  }

  let nebiusRes: Response;
  try {
    nebiusRes = await fetch(`${NEBIUS_BASE_URL}/chat/completions`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        Authorization: `Bearer ${apiKey}`,
      },
      body: JSON.stringify({
        model: NEMOTRON_MODEL,
        messages: [
          { role: 'system', content: buildSystemPrompt(rooms) },
          { role: 'user', content: prompt },
        ],
        response_format: { type: 'json_object' },
        temperature: 0.3,
      }),
      signal: AbortSignal.timeout(60000),
    });
  } catch (err) {
    return NextResponse.json(
      { error: `Could not reach Nebius Token Factory (${err instanceof Error ? err.message : String(err)})` },
      { status: 502 },
    );
  }

  if (!nebiusRes.ok) {
    const text = await nebiusRes.text().catch(() => '');
    return NextResponse.json({ error: `Nebius Token Factory returned HTTP ${nebiusRes.status}: ${text.slice(0, 300)}` }, { status: 502 });
  }

  const data = await nebiusRes.json();
  const raw: string = data?.choices?.[0]?.message?.content || '';

  /* ── Old Ollama-based call (commented out, kept for rollback) ──────────
  let ollamaRes: Response;
  try {
    ollamaRes = await fetch(OLLAMA_URL, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        model: MODEL,
        messages: [
          { role: 'system', content: buildSystemPrompt(rooms) },
          { role: 'user', content: prompt },
        ],
        format: 'json',
        stream: false,
        keep_alive: '30m',
      }),
      signal: AbortSignal.timeout(120000),
    });
  } catch (err) {
    return NextResponse.json(
      { error: `Could not reach Ollama at ${OLLAMA_URL} — is "ollama serve" running? (${err instanceof Error ? err.message : String(err)})` },
      { status: 502 },
    );
  }
  if (!ollamaRes.ok) {
    const text = await ollamaRes.text().catch(() => '');
    return NextResponse.json({ error: `Ollama returned HTTP ${ollamaRes.status}: ${text.slice(0, 300)}` }, { status: 502 });
  }
  const data = await ollamaRes.json();
  const raw: string = data?.message?.content || '';
  ────────────────────────────────────────────────────────────────────── */

  let intent: { action?: string; entity_kind?: string; room_type?: string; room?: string; attributes?: Record<string, unknown> };
  try {
    intent = JSON.parse(raw);
  } catch {
    return NextResponse.json({ error: `Model returned non-JSON output: ${raw.slice(0, 300)}` }, { status: 502 });
  }

  const action = intent.action === 'furnish' ? 'furnish' : 'add';

  // `room` is allowed to be missing/empty and defaults to a placeholder
  // rather than failing the request — the Kit side already handles an
  // unmatched room name gracefully (a clear "couldn't find that room"
  // status), and a "...in the current camera view" prompt overrides
  // whatever room comes back anyway (see _resolve_camera_view_target),
  // so it should never hard-fail here just because there are zero (or
  // zero relevant) labeled rooms for the model to echo back. Only the
  // actual THING to place (entity_kind / room_type) is non-negotiable.
  const room = (intent.room && String(intent.room).trim()) || 'Unspecified';

  if (action === 'furnish') {
    if (!intent.room_type) {
      return NextResponse.json({ error: `Model response missing required fields: ${JSON.stringify(intent)}` }, { status: 502 });
    }
    return NextResponse.json({
      action,
      room_type: String(intent.room_type).toLowerCase(),
      room,
      attributes: intent.attributes || {},
    });
  }

  if (!intent.entity_kind) {
    return NextResponse.json({ error: `Model response missing required fields: ${JSON.stringify(intent)}` }, { status: 502 });
  }

  return NextResponse.json({
    action,
    entity_kind: String(intent.entity_kind).toLowerCase(),
    room,
    attributes: intent.attributes || {},
    supported: isSupportedKind(String(intent.entity_kind).toLowerCase()),
  });
}
