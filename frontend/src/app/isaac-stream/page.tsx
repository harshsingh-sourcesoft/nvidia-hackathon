'use client';

// Opens as its own browser tab from editor.html's "Open in Isaac Sim" flow
// once a launch finishes — see _isaacPoll()'s isDone branch in editor.html,
// and api_server_mrcnn.py's _isaac_worker_mrcnn, which launches the Kit app
// with omni.kit.livestream.app enabled instead of (or in addition to) its
// local window, and reports the signaling port back once it's confirmed
// listening. host/port here are exactly what that backend reported, passed
// through as query params — this page never guesses them.
//
// host/port identify WHICH already-running Kit process to connect to, not a
// new one to launch — that launch already happened server-side before this
// tab ever opens.
//
// The actual streaming component is loaded with ssr:false — @nvidia/ov-web-rtc
// touches browser globals (WebRTC APIs) at import time, which crashes Next's
// server-side pre-render pass of this page (confirmed live: a plain top-level
// import here 500'd every request). Dynamic-importing it client-only avoids
// that entirely; nothing in this file needs to render on the server anyway.

import dynamic from 'next/dynamic';
import { Suspense } from 'react';

const IsaacStreamView = dynamic(() => import('./IsaacStreamView'), { ssr: false });

export default function IsaacStreamPage() {
  return (
    <Suspense fallback={null}>
      <IsaacStreamView />
    </Suspense>
  );
}
