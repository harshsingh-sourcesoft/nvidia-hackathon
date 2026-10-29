import { NextResponse } from 'next/server';
import fs from 'fs';
import path from 'path';

// Generates short-lived Cloudflare Realtime TURN credentials server-side —
// the API token that authorizes this call is a real secret and must never
// reach the browser, so this route exists purely to hide it: the client
// calls this same-origin endpoint and gets back only the resulting
// iceServers array (short-lived username/credential pair + STUN/TURN
// URLs), which is safe to hand to AppStreamer.connect()'s
// iceServerConfiguration. See IsaacStreamView.tsx for why this exists at
// all — without a TURN relay, Isaac Sim streaming only works for visitors
// on the same network as this machine (confirmed live: direct connection
// attempts from outside fail, since nothing forwards the raw signaling/
// media ports through to this host).
let _cfKeyIdCache: string | null = null;
let _cfApiTokenCache: string | null = null;

function readCloudflareCreds(): { keyId: string; apiToken: string } | null {
  if (_cfKeyIdCache && _cfApiTokenCache) {
    return { keyId: _cfKeyIdCache, apiToken: _cfApiTokenCache };
  }
  if (process.env.CLOUDFLARE_TURN_KEY_ID && process.env.CLOUDFLARE_TURN_API_TOKEN) {
    _cfKeyIdCache = process.env.CLOUDFLARE_TURN_KEY_ID;
    _cfApiTokenCache = process.env.CLOUDFLARE_TURN_API_TOKEN;
    return { keyId: _cfKeyIdCache, apiToken: _cfApiTokenCache };
  }
  // Same PlanLift-Backend/.env every other secret in this project already
  // reads from — single source of truth, never duplicated into app/'s own
  // env files. Mirrors /api/agent/route.ts's own readNebiusApiKey.
  try {
    const envPath = path.join(process.cwd(), '..', 'PlanLift-Backend', '.env');
    const text = fs.readFileSync(envPath, 'utf-8');
    let keyId = '';
    let apiToken = '';
    for (const line of text.split('\n')) {
      const trimmed = line.trim();
      if (!trimmed || trimmed.startsWith('#') || !trimmed.includes('=')) continue;
      const eq = trimmed.indexOf('=');
      const k = trimmed.slice(0, eq).trim();
      const v = trimmed.slice(eq + 1).trim().replace(/^["']|["']$/g, '');
      if (k === 'CLOUDFLARE_TURN_KEY_ID') keyId = v;
      if (k === 'CLOUDFLARE_TURN_API_TOKEN') apiToken = v;
    }
    if (keyId && apiToken) {
      _cfKeyIdCache = keyId;
      _cfApiTokenCache = apiToken;
      return { keyId, apiToken };
    }
  } catch {
    // fall through — empty creds handled by the caller
  }
  return null;
}

export async function GET() {
  const creds = readCloudflareCreds();
  if (!creds) {
    return NextResponse.json(
      { error: 'CLOUDFLARE_TURN_KEY_ID/CLOUDFLARE_TURN_API_TOKEN not set in PlanLift-Backend/.env' },
      { status: 502 },
    );
  }

  let cfRes: Response;
  try {
    cfRes = await fetch(
      `https://rtc.live.cloudflare.com/v1/turn/keys/${creds.keyId}/credentials/generate-ice-servers`,
      {
        method: 'POST',
        headers: {
          Authorization: `Bearer ${creds.apiToken}`,
          'Content-Type': 'application/json',
        },
        // Comfortably longer than any realistic single viewing session —
        // short-lived by Cloudflare's own design, not meant to be reused
        // across page loads, so this route is called fresh every time
        // IsaacStreamView mounts rather than cached.
        body: JSON.stringify({ ttl: 3600 }),
        signal: AbortSignal.timeout(10000),
      },
    );
  } catch (err) {
    return NextResponse.json(
      { error: `Could not reach Cloudflare Realtime (${err instanceof Error ? err.message : String(err)})` },
      { status: 502 },
    );
  }

  if (!cfRes.ok) {
    const text = await cfRes.text().catch(() => '');
    return NextResponse.json({ error: `Cloudflare returned HTTP ${cfRes.status}: ${text.slice(0, 300)}` }, { status: 502 });
  }

  const data = await cfRes.json();
  return NextResponse.json({ iceServers: data.iceServers || [] });
}
