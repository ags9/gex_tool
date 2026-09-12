import type { Alert, Health, LatestPoll, Poll, ShadowTrade } from "./types";

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
  latest: (signal?: AbortSignal) =>
    get<LatestPoll>("/api/session/latest", signal),
  polls: (date: string, signal?: AbortSignal) =>
    get<Poll[]>(`/api/session/${date}/polls`, signal),
  profileAt: (date: string, minute: number, signal?: AbortSignal) =>
    get<LatestPoll>(`/api/session/${date}/profile?minute=${minute}`, signal),
  alerts: (date: string, signal?: AbortSignal) =>
    get<Alert[]>(`/api/session/${date}/alerts`, signal),
  trades: (date: string, signal?: AbortSignal) =>
    get<ShadowTrade[]>(`/api/session/${date}/trades`, signal),
};

export { ApiError };
