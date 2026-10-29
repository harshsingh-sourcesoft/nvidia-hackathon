import { NextRequest } from 'next/server';

// Generic same-origin reverse proxy for the Python backends (Mask R-CNN on
// :8001, Hunyuan3D-2.1/TRELLIS on :8002). editor.html's dozens of
// API_BASE/TRELLIS_API call sites build their own paths by string
// concatenation (e.g. `${API_BASE}/projects`, `${TRELLIS_API}/stream/${id}`)
// — rather than hand-writing a route per endpoint, this one handler covers
// all of them: whatever path a client hits under /api/proxy/<target>/*
// is forwarded verbatim to http://localhost:<port>/* and the response is
// streamed straight back, unbuffered. That's what makes this work for a
// remote visitor through a public tunnel at all: `http://localhost:8001`
// means a DIFFERENT machine to every visitor's own browser, but a relative
// `/api/proxy/mrcnn/...` always resolves to this same origin, and this
// route (running server-side, on the actual host machine) is the one that
// still gets to say "localhost" and mean it.
//
// Streaming both ways matters for two real cases here, not just large
// files: /predict and /generate take a multipart FormData file upload
// (forwarding req.body unbuffered avoids holding a whole image/upload in
// memory), and /stream/<job_id> is a long-lived EventSource (SSE) — piping
// res.body straight through without buffering is what lets that keep
// working unchanged.
export async function proxyToBackend(
  req: NextRequest,
  pathSegments: string[],
  target: string,
): Promise<Response> {
  const url = new URL(req.url);
  const targetUrl = `${target}/${pathSegments.join('/')}${url.search}`;

  const headers = new Headers(req.headers);
  headers.delete('host');
  // Content-length IS kept (not deleted) — the body below is req.body piped
  // through completely unchanged, byte for byte, so the original value is
  // still accurate.
  //
  // Expect IS deleted. A client that adds "Expect: 100-continue" for a
  // large upload (curl does this automatically past a size threshold; some
  // browsers/XHR paths can too) gets that header copied onto the outbound
  // fetch() call below, and Node's fetch (undici) throws
  // `NotSupportedError: expect header not supported` the instant it sees
  // it — caught below as a generic "fetch failed", which this route then
  // reports as a 502. Confirmed directly via a 30MB multipart upload:
  // failed through this proxy with that exact undici cause (visible via
  // the catch block below), while the identical bytes posted straight to
  // the backend, bypassing this proxy, succeeded immediately. Still
  // unconfirmed whether this is the same failure the browser hit via the
  // public domain (reported there as a 413, a different status, possibly
  // from another layer in front of this) — fixing it here regardless,
  // since it's a real bug on its own.
  headers.delete('expect');

  const hasBody = !(req.method === 'GET' || req.method === 'HEAD');

  let upstream: Response;
  try {
    upstream = await fetch(targetUrl, {
      method: req.method,
      headers,
      body: hasBody ? req.body : undefined,
      // Required by Node's fetch (undici) whenever body is a ReadableStream.
      ...(hasBody ? { duplex: 'half' } : {}),
    } as RequestInit);
  } catch (err) {
    // err.cause carries undici's actual reason (e.g. UND_ERR_*, ECONNRESET)
    // under Node's generic "fetch failed" wrapper message — surfaced here
    // temporarily while diagnosing the large-upload failure.
    const cause = err instanceof Error && 'cause' in err ? (err as any).cause : undefined;
    const causeStr = cause ? ` cause=${cause instanceof Error ? `${cause.name}: ${cause.message}` : String(cause)}` : '';
    return Response.json(
      { error: `Could not reach backend at ${target} (${err instanceof Error ? err.message : String(err)}${causeStr}) — is it running?` },
      { status: 502 },
    );
  }

  const outHeaders = new Headers(upstream.headers);
  // fetch() transparently decompresses the body for us, so the original
  // content-encoding/content-length headers no longer describe the bytes
  // we're about to send — forwarding them verbatim would make the browser
  // try to gunzip an already-decompressed body and either corrupt or
  // truncate every image/JSON response that passed through this proxy.
  outHeaders.delete('content-encoding');
  outHeaders.delete('content-length');

  return new Response(upstream.body, {
    status: upstream.status,
    headers: outHeaders,
  });
}
