/** Display helpers. Every one of these is presentation only — no figure on
 *  screen is computed here that is not already stored or derived by the API. */

export function money(v: number | null | undefined): string {
  if (v === null || v === undefined) return "—";
  const m = v / 1e6;
  if (Math.abs(m) >= 1000) return `${m >= 0 ? "+" : ""}${(m / 1000).toFixed(2)}B`;
  return `${m >= 0 ? "+" : ""}${m.toFixed(1)}M`;
}

export function price(v: number | null | undefined, digits = 0): string {
  if (v === null || v === undefined) return "—";
  return v.toLocaleString("en-US", {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  });
}

/** Seconds -> "11m 04s" / "2h 13m". Ages are read at a glance; be terse. */
export function age(seconds: number): string {
  const s = Math.max(0, Math.floor(seconds));
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m ${String(s % 60).padStart(2, "0")}s`;
  const h = Math.floor(m / 60);
  return `${h}h ${String(m % 60).padStart(2, "0")}m`;
}

export function clockUtcToEt(d: Date): string {
  return d.toLocaleTimeString("en-US", {
    hour12: false,
    timeZone: "America/New_York",
  });
}
