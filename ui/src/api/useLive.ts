import { useEffect, useRef, useState } from "react";

import { api } from "./client";
import type { Alert, Health, LatestPoll, LiveMessage, Poll, ShadowTrade, Strike } from "./types";

export type ConnState = "connecting" | "live" | "reconnecting" | "off";

export interface LiveState {
  conn: ConnState;
  poll: (Poll & { strikes: Strike[] }) | null;
  position: ShadowTrade | null;
  health: Health | null;
  alerts: Alert[];
  pollsReceived: number;
  lastMessageAt: Date | null;
  error: string | null;
}

const MAX_BACKOFF_MS = 15_000;
const ALERT_BUFFER = 50;

/**
 * One WebSocket to /ws/live, reconnecting forever.
 *
 * The engine never pushes into the API and the API never pushes into the
 * engine; this socket is the last link in a chain that is deliberately
 * one-directional (Phase 2 §4.2). A wedged browser can therefore do nothing
 * worse than miss updates.
 *
 * On every reconnect the snapshot arrives first, so a dropped connection
 * cannot leave the screen showing a half-updated map. If the socket cannot be
 * established at all we fall back to a single REST read so the strip can say
 * something truthful instead of nothing.
 */
export function useLive(): LiveState {
  const [state, setState] = useState<LiveState>({
    conn: "connecting",
    poll: null,
    position: null,
    health: null,
    alerts: [],
    pollsReceived: 0,
    lastMessageAt: null,
    error: null,
  });

  const wsRef = useRef<WebSocket | null>(null);
  const attemptRef = useRef(0);
  const timerRef = useRef<number | null>(null);
  const closedRef = useRef(false);

  useEffect(() => {
    closedRef.current = false;

    // seed from REST so the first paint is truthful even before the socket
    // opens — and so a socket that never opens still shows real numbers
    const ac = new AbortController();
    void (async () => {
      try {
        const [latest, health] = await Promise.all([
          api.latest(ac.signal).catch(() => null),
          api.health(ac.signal).catch(() => null),
        ]);
        if (ac.signal.aborted) return;
        setState((s) =>
          s.poll
            ? s
            : {
                ...s,
                poll: latest ? { ...latest, strikes: latest.strikes } : s.poll,
                position: latest?.position ?? s.position,
                health: health ?? s.health,
              },
        );
      } catch {
        /* the socket is the primary path; this is only a head start */
      }
    })();

    const connect = () => {
      if (closedRef.current) return;
      const proto = window.location.protocol === "https:" ? "wss" : "ws";
      const ws = new WebSocket(`${proto}://${window.location.host}/ws/live`);
      wsRef.current = ws;

      ws.onopen = () => {
        attemptRef.current = 0;
        setState((s) => ({ ...s, conn: "live", error: null }));
      };

      ws.onmessage = (ev) => {
        let msg: LiveMessage;
        try {
          msg = JSON.parse(ev.data as string) as LiveMessage;
        } catch {
          return;
        }
        setState((s) => apply(s, msg));
      };

      ws.onerror = () => {
        setState((s) => ({ ...s, error: "websocket error" }));
      };

      ws.onclose = () => {
        if (closedRef.current) return;
        setState((s) => ({ ...s, conn: "reconnecting" }));
        const delay = Math.min(500 * 2 ** attemptRef.current, MAX_BACKOFF_MS);
        attemptRef.current += 1;
        timerRef.current = window.setTimeout(connect, delay);
      };
    };

    connect();

    return () => {
      closedRef.current = true;
      ac.abort();
      if (timerRef.current !== null) window.clearTimeout(timerRef.current);
      wsRef.current?.close();
    };
  }, []);

  return state;
}

function apply(s: LiveState, msg: LiveMessage): LiveState {
  const now = new Date();
  switch (msg.type) {
    case "snapshot": {
      const snap = msg.data as LatestPoll | null;
      return {
        ...s,
        lastMessageAt: now,
        poll: snap ? { ...snap, strikes: snap.strikes } : s.poll,
        position: snap ? snap.position : s.position,
      };
    }
    case "poll":
      return {
        ...s,
        lastMessageAt: now,
        poll: msg.data,
        pollsReceived: s.pollsReceived + 1,
      };
    case "alert":
      return {
        ...s,
        lastMessageAt: now,
        alerts: [msg.data, ...s.alerts].slice(0, ALERT_BUFFER),
      };
    case "trade":
      // an exit updates the row in place; a closed trade means we are flat
      return {
        ...s,
        lastMessageAt: now,
        position: msg.data.exit_ts ? null : msg.data,
      };
    case "health":
      return { ...s, lastMessageAt: now, health: msg.data };
    default:
      return s;
  }
}

/** Ticks once a second so ages on screen count up between messages. */
export function useNow(intervalMs = 1000): Date {
  const [now, setNow] = useState(() => new Date());
  useEffect(() => {
    const id = window.setInterval(() => setNow(new Date()), intervalMs);
    return () => window.clearInterval(id);
  }, [intervalMs]);
  return now;
}
