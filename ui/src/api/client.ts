import type {
  Alert,
  Bundle,
  BundleSummary,
  Health,
  Heatmap,
  LatestPoll,
  Poll,
  Premium,
  SessionSummary,
  ShadowTrade,
} from "./types";

/**
 * Parse a timestamp from the API as UTC.
 *
 * Load-bearing. `poll_snapshot.ts` is stored as a naive TIMESTAMP and comes
 * back without an offset ("2026-09-12T00:41:13.586881"). `new Date()` reads
 * an offset-less string as LOCAL time, which would silently shift every
 * timestamp by the machine's UTC offset — four hours here — and make a fresh
 * poll look hours stale, or a stale one look fresh. The same class of bug
 * cost us the replay/parity timezone mess (CLAUDE.md §9); it is not repeating
 * on the display side.
 *
 * `health.as_of` already carries "+00:00" and is left alone.
 */
export function parseUtc(iso: string): Date {
  const hasZone = /(?:Z|[+-]\d{2}:?\d{2})$/.test(iso);
  return new Date(hasZone ? iso : `${iso}Z`);
}

function q(params: Record<string, string | number | undefined>): string {
  const s = Object.entries(params)
    .filter(([, v]) => v !== undefined && v !== null && v !== "")
    .map(([k, v]) => `${k}=${encodeURIComponent(String(v))}`)
    .join("&");
  return s ? `?${s}` : "";
}

class ApiError extends Error {
  constructor(readonly status: number, readonly path: string, body: string) {
    super(`${status} ${path}${body ? ` — ${body}` : ""}`);
    this.name = "ApiError";
  }
}

async function get<T>(path: string, signal?: AbortSignal): Promise<T> {
  const res = await fetch(path, { signal });
  if (!res.ok) {
    throw new ApiError(res.status, path, await res.text().catch(() => ""));
  }
  return (await res.json()) as T;
}

export const api = {
  health: (signal?: AbortSignal) => get<Health>("/api/health", signal),
  latest: (underlying?: string, signal?: AbortSignal) =>
    get<LatestPoll>(`/api/session/latest${q({ underlying })}`, signal),
  polls: (date: string, underlying?: string, signal?: AbortSignal) =>
    get<Poll[]>(`/api/session/${date}/polls${q({ underlying })}`, signal),
  profileAt: (date: string, minute: number, signal?: AbortSignal) =>
    get<LatestPoll>(`/api/session/${date}/profile?minute=${minute}`, signal),
  alerts: (date: string, underlying?: string, signal?: AbortSignal) =>
    get<Alert[]>(`/api/session/${date}/alerts${q({ underlying })}`, signal),
  trades: (date: string, underlying?: string, signal?: AbortSignal) =>
    get<ShadowTrade[]>(`/api/session/${date}/trades${q({ underlying })}`, signal),
  premium: (date: string, signal?: AbortSignal) =>
    get<Premium[]>(`/api/session/${date}/premium`, signal),
  heatmap: (date: string, underlying?: string, signal?: AbortSignal) =>
    get<Heatmap>(`/api/session/${date}/heatmap${q({ underlying })}`, signal),
  expiries: (pollId: number, signal?: AbortSignal) =>
    get<import("./types").Expiry[]>(`/api/session/expiry?poll_id=${pollId}`, signal),
  narrate: (underlying?: string, signal?: AbortSignal) =>
    get<{ narration: string | null; model: string; as_of: string }>(
      `/api/session/narrate${q({ underlying })}`, signal),
  tape: (
    underlying?: string,
    opts: { limit?: number; min_size?: number; strike?: number;
            right?: string; side?: number } = {},
    signal?: AbortSignal,
  ) =>
    get<{ prints: import("./types").TapePrint[];
          stats: import("./types").TapeStats }>(
      `/api/tape${q({ underlying, ...opts })}`, signal),
  chain: (symbol = "SPX", dte = 3, strikes = 5, signal?: AbortSignal) =>
    get<import("./types").ChainView>(
      `/api/chain${q({ symbol, dte, strikes })}`, signal),
  underlyings: (signal?: AbortSignal) =>
    get<string[]>("/api/underlyings", signal),
  sessions: (limit = 30, signal?: AbortSignal) =>
    get<SessionSummary[]>(`/api/sessions?limit=${limit}`, signal),
  bundles: (signal?: AbortSignal) =>
    get<BundleSummary[]>("/api/backtests", signal),
  bundle: (name: string, signal?: AbortSignal) =>
    get<Bundle>(`/api/backtests/${encodeURIComponent(name)}`, signal),
};

/** A cancelled request is not a failure. StrictMode aborts the first pass of
 *  every effect in dev, and surfacing that as an error banner reports the
 *  framework's own behaviour as a server fault. */
export function isAbort(e: unknown): boolean {
  // Not `instanceof DOMException`: the abort reason crosses realms in dev and
  // the name is the only thing reliably present.
  return (
    typeof e === "object" &&
    e !== null &&
    ((e as { name?: string }).name === "AbortError" ||
      String((e as { message?: string }).message ?? "").includes("aborted"))
  );
}

export { ApiError };
