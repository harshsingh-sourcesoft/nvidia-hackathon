import { NextRequest } from 'next/server';
import { proxyToBackend } from '@/utils/backendProxy';

// Mask R-CNN / floorplan backend (api_server_mrcnn.py) — see
// utils/backendProxy.ts for why this exists at all.
export const runtime = 'nodejs';

const TARGET = 'http://localhost:8001';

function handle(req: NextRequest, { params }: { params: { path: string[] } }) {
  return proxyToBackend(req, params.path, TARGET);
}

export const GET = handle;
export const POST = handle;
export const PUT = handle;
export const DELETE = handle;
export const PATCH = handle;
