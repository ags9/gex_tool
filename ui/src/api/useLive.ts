import { useEffect, useState } from "react";

import { api } from "./client";
import type {
  Alert, Health, LatestPoll, LiveMessage, ShadowTrade, TapePrint, TapeStats,
} from "./types";

export type ConnState = "connecting" | "live" | "reconnecting" | "off";

export interface LiveState {
  conn: ConnState;
  poll: LatestPoll | null;
  position: ShadowTrade | null;
  health: Health | null;
  alerts: Alert[];
  pollsReceived: number;
  prints: TapePrint[];
  tapeStats: TapeStats | null;
  lastMessageAt: Date | null;
  error: string | null;
}

const MAX_BACKOFF_MS = 15_000;
const ALERT_BUFFER = 50;
const TAPE_LIMIT = 500;

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
export function useLive(underlying?: string): LiveState {
  const [state, setState] = useState<LiveState>({
    conn: "connecting",
    poll: null,
    position: null,
    health: null,
    alerts: [],
    pollsReceived: 0,
    prints: [],
    tapeStats: null,
    lastMessageAt: null,
    error: null,
  });

  useEffect(() => {
    // Everything below is effect-LOCAL, not refs. React StrictMode mounts
    // effects twice in dev, and with shared refs the first invocation's
    // teardown raced the second's setup: the dying socket's onclose saw a
    // freshly-reset "not closed" flag and scheduled its own reconnect. That
    // left orphaned sockets accumulating — 4 for one tab, measured — each
    // polling DuckDB once a second against the engine's write lock, and
    // each incrementing the same counters, so one poll registered as two.
    // Effect-local closures make every invocation independently cancellable.
    let cancelled = false;
    let socket: WebSocket | null = null;
    let timer: number | null = null;
    let attempt = 0;

    // Seed from REST so the first paint is truthful before the socket opens,
    // and so a socket that never opens still shows real numbers.
    const ac = new AbortController();
    void (async () => {
      try {
        const [latest, health] = await Promise.all([
          api.latest(underlying, ac.signal).catch(() => null),
          api.health(ac.signal).catch(() => null),
        ]);
        if (cancelled) return;
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
      if (cancelled) return;
      const proto = window.location.protocol === "https:" ? "wss" : "ws";
      const qs = underlying ? `?underlying=${encodeURIComponent(underlying)}` : "";
      const sock = new WebSocket(`${proto}://${window.location.host}/ws/live${qs}`);
      socket = sock;

      // `socket !== sock` means this handler belongs to a superseded
      // connection; it must not touch state or schedule anything.
      const stale = () => cancelled || socket !== sock;

      sock.onopen = () => {
        if (stale()) return;
        attempt = 0;
        setState((s) => ({ ...s, conn: "live", error: null }));
      };

      sock.onmessage = (ev) => {
        if (stale()) return;
        let msg: LiveMessage;
        try {
          msg = JSON.parse(ev.data as string) as LiveMessage;
        } catch {
          return;
        }
        setState((s) => apply(s, msg));
      };

      sock.onerror = () => {
        if (stale()) return;
        setState((s) => ({ ...s, error: "websocket error" }));
      };

      sock.onclose = () => {
        if (stale()) return;
        setState((s) => ({ ...s, conn: "reconnecting" }));
        const delay = Math.min(500 * 2 ** attempt, MAX_BACKOFF_MS);
        attempt += 1;
        timer = window.setTimeout(connect, delay);
      };
    };

    connect();

    return () => {
      cancelled = true;
      ac.abort();
      if (timer !== null) window.clearTimeout(timer);
      const dying = socket;
      socket = null;            // any in-flight handler now reads as stale
      dying?.close();
    };
    // Switching instrument tears the socket down and reconnects: the server
    // filters the stream, so a stale socket would keep pushing the old map.
  }, [underlying]);

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
      // The push carries its own context, position and as_of — every number
      // on screen then describes the same instant.
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
    case "prints":
      // Newest first, and bounded on the client too: a session's prints must
      // not accumulate in the tab any more than they do on the engine.
      return {
        ...s,
        lastMessageAt: now,
        prints: [...[...msg.data.prints].sort((a, b) => b.seq - a.seq),
                 ...s.prints].slice(0, TAPE_LIMIT),
        tapeStats: msg.data.stats,
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
