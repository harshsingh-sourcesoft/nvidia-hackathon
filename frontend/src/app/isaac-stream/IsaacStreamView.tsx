'use client';

import { useEffect, useRef, useState } from 'react';
import { useSearchParams } from 'next/navigation';
import {
  AppStreamer,
  DirectConfig,
  EventAction,
  EventStatus,
  IceServerConfiguration,
  LogLevel,
  StreamEvent,
  StreamProps,
  StreamType,
} from '@nvidia/ov-web-rtc';

// crypto.randomUUID() is only exposed in "secure contexts" (HTTPS, or
// localhost) per the Web Crypto spec — it's simply undefined on `window.crypto`
// anywhere else. The stream library calls it internally when a connection
// opens, which is fine over the HTTPS tunnel but throws for anyone reaching
// this page via a plain http://<lan-ip>:3000 link (confirmed live: "crypto.
// randomUUID is not a function" thrown from the library's onStreamStarted/
// sendConfig path, right as the WebSocket's onopen fires). crypto.getRandomValues
// IS available in every context, so this polyfill still gets real randomness
// rather than falling back to Math.random.
if (typeof window !== 'undefined' && window.crypto && typeof window.crypto.randomUUID !== 'function') {
  (window.crypto as Crypto & { randomUUID: () => string }).randomUUID = () => {
    const bytes = new Uint8Array(16);
    window.crypto.getRandomValues(bytes);
    bytes[6] = (bytes[6] & 0x0f) | 0x40; // version 4
    bytes[8] = (bytes[8] & 0x3f) | 0x80; // variant 10
    const hex = Array.from(bytes, (b) => b.toString(16).padStart(2, '0')).join('');
    return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
  };
}

export default function IsaacStreamView() {
  const params = useSearchParams();
  const host = params.get('host') || '127.0.0.1';
  const port = Number(params.get('port') || 49100);

  const [state, setState] = useState<'connecting' | 'connected' | 'failed'>('connecting');
  const [errorMessage, setErrorMessage] = useState('');
  const connectStarted = useRef(false);

  useEffect(() => {
    // Effects can fire twice in dev (React Strict Mode) — AppStreamer.connect
    // isn't idempotent, so guard against actually starting a second session.
    if (connectStarted.current) return;
    connectStarted.current = true;

    (async () => {
      // Without a TURN relay, this only ever worked for a visitor on the
      // same network as the host — direct connection attempts from outside
      // fail outright, since nothing forwards the raw signaling/media ports
      // through. Fetched fresh per page load (not cached) since these are
      // short-lived credentials from Cloudflare Realtime's TURN service —
      // see /api/turn-credentials for why the request goes through our own
      // server instead of calling Cloudflare directly (keeps the API token
      // that authorizes it off the browser entirely).
      let iceServerConfiguration: IceServerConfiguration | undefined;
      try {
        const res = await fetch('/api/turn-credentials');
        const data = await res.json();
        if (Array.isArray(data.iceServers) && data.iceServers.length) {
          // "all" tries a direct connection first (best for a viewer on the
          // same LAN as this machine — lower latency, no relay hop) and
          // only falls through to the TURN relay when direct fails, which
          // is exactly the remote-visitor case this exists for. Forcing
          // "relay" unconditionally would work too, but would burn
          // Cloudflare's relay bandwidth quota even for local viewers who
          // never needed it.
          iceServerConfiguration = { iceServers: data.iceServers, iceTransportPolicy: 'all' };
        }
      } catch {
        // No TURN credentials available — fall through and attempt a
        // direct-only connection, same as before this existed.
      }

      const streamConfig: DirectConfig = {
        videoElementId: 'remote-video',
        signalingServer: host,
        signalingPort: port,
        // editor.html only ever sends port 443 when it's fronted by a
        // reverse proxy (Apache, a tunnel) rather than this being Kit's own
        // raw signaling port — forcing WSS here is what makes the browser
        // actually speak wss:// to it instead of defaulting to plain ws://
        // (confirmed live: Apache's WebSocket proxy on 443 only answers
        // correctly over the upgraded TLS connection).
        forceWSS: port === 443,
        // width/height here are an upper bound on what the server will ever
        // send, not what actually gets rendered/encoded — that's controlled
        // server-side by planlift_timelapse.kit's own [settings.app.renderer]
        // resolution, which is what actually needs to match. fps matches
        // the server's own loop-rate default (60) — see the server-side
        // comment in api_server_mrcnn.py for why a lowered, mismatched fps
        // caused a growing frame backlog instead of helping.
        width: 1920,
        height: 1080,
        fps: 60,
        iceServerConfiguration,
        onStart: (message: StreamEvent) => {
          if (message.action === EventAction.START) {
            if (message.status === EventStatus.SUCCESS) {
              setState('connected');
            } else if (message.status === EventStatus.ERROR) {
              setState('failed');
              setErrorMessage(String(message.info || 'Unknown error'));
            }
          }
        },
        onStop: () => {
          setState('failed');
          setErrorMessage('Stream disconnected.');
        },
      };
      const streamProps: StreamProps = {
        streamSource: StreamType.DIRECT,
        logLevel: LogLevel.INFO,
        streamConfig,
      };

      AppStreamer.connect(streamProps).catch((err) => {
        setState('failed');
        setErrorMessage(String(err || 'Failed to connect to stream'));
      });
    })();
  }, [host, port]);

  return (
    <div style={{ position: 'fixed', inset: 0, background: '#000' }}>
      <video
        id="remote-video"
        autoPlay
        playsInline
        style={{
          width: '100%',
          height: '100%',
          objectFit: 'contain',
          display: state === 'connected' ? 'block' : 'none',
        }}
      />
      {state !== 'connected' && (
        <div
          style={{
            position: 'absolute',
            inset: 0,
            display: 'flex',
            flexDirection: 'column',
            alignItems: 'center',
            justifyContent: 'center',
            color: '#fff',
            fontFamily: 'system-ui, sans-serif',
            gap: 12,
            textAlign: 'center',
            padding: 24,
          }}
        >
          {state === 'connecting' ? (
            <>
              <div>Connecting to Isaac Sim…</div>
              <div style={{ fontSize: 13, color: '#888' }}>
                {host}:{port}
              </div>
            </>
          ) : (
            <>
              <div style={{ color: '#f87171' }}>Couldn&apos;t connect to the Isaac Sim stream.</div>
              <div style={{ fontSize: 13, color: '#888', maxWidth: 480 }}>{errorMessage}</div>
            </>
          )}
        </div>
      )}
    </div>
  );
}
